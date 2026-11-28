"""The throwaway FreeBSD host: boot the pinned image under QEMU, run something as root in it, bring the results back.

For Linux with KVM (a GitHub-hosted ubuntu-24.04 runner or a workstation). Needs qemu-system-x86_64, qemu-img, OVMF,
xorriso (or cloud-localds, genisoimage) and tar. No root and no host configuration: user-mode networking, files only.
Nothing travels over a listening socket: what goes in and comes out is a tar stream on a raw virtual disk.

The pattern follows scripts/runner/run-vm.sh of FreeSense 1.x.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import lzma
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time

import source

GIB = 1024 ** 3
FIRMWARE = (("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
            ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
            ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
            ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"))
BOOT_TIMEOUT = 600


def firmware() -> tuple[str, str]:
    for code, variables in FIRMWARE:
        if Path(code).is_file() and Path(variables).is_file():
            return code, variables
    raise ValueError("UEFI firmware (OVMF) was not found; install the ovmf package")


# A standard GitHub-hosted runner for a public repository, to try its sizing on a larger machine.
PROFILES = {"runner": (4, 12288)}


def size() -> tuple[int, int]:
    """CPUs and MiB of memory for the VM: the whole machine but 3 GiB, unless FREESENSE_VM_PROFILE or
    FREESENSE_VM_CPUS and FREESENSE_VM_MEMORY_MB say otherwise."""
    name = os.environ.get("FREESENSE_VM_PROFILE")
    if name and name not in PROFILES:
        raise ValueError(f"unknown FREESENSE_VM_PROFILE {name}; known: {', '.join(PROFILES)}")
    profile = PROFILES.get(name)
    cpus = int(os.environ.get("FREESENSE_VM_CPUS") or (profile[0] if profile else min(os.cpu_count() or 1, 32)))
    memory = os.environ.get("FREESENSE_VM_MEMORY_MB") or (profile[1] if profile else None)
    if not memory:
        total = re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text(encoding="ascii"))
        memory = min(int(total[1]) // 1024 - 3072, 32768) // 256 * 256
    if int(memory) < 6144:
        raise ValueError(f"the build VM needs at least 6 GiB of memory, {memory} MiB are available for it")
    return cpus, int(memory)


def preflight(directory: Path, need_gib: int) -> None:
    for tool in ("qemu-system-x86_64", "qemu-img", "tar", "zstd"):
        if not shutil.which(tool):
            raise ValueError(f"missing host program: {tool}")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise ValueError("/dev/kvm is not readable and writable for this user; the build needs hardware virtualisation")
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free / GIB
    need = int(os.environ.get("FREESENSE_MIN_FREE_GIB") or need_gib)
    if free < need:
        raise ValueError(f"{free:.0f} GiB are free under {directory}; these stages need about {need} GiB")
    firmware()


def image(lock: dict, cache: Path) -> Path:
    """The pinned builder image, unpacked. FREESENSE_BUILDER_IMAGE names a local copy of the .qcow2.xz."""
    entry = lock["builder_image"]
    cache.mkdir(parents=True, exist_ok=True)
    unpacked = cache / f"{entry['sha256']}.qcow2"
    if unpacked.is_file():
        return unpacked
    local = os.environ.get("FREESENSE_BUILDER_IMAGE")
    if local:
        packed = Path(local)
        if source.sha256(packed) != entry["sha256"]:
            raise ValueError(f"{packed} does not have the sha256 of the pinned builder image")
    else:
        print(f"  downloading the pinned builder image {entry['file']}", flush=True)
        packed = source.fetch_file(cache / entry["file"], [entry["url"]], entry["sha256"], None)
    partial = unpacked.with_suffix(".part")
    with lzma.open(packed) as stream, partial.open("wb") as target:
        shutil.copyfileobj(stream, target, 1 << 20)
    # The download stays: a workflow can cache it, and the snapshot server drops old images.
    partial.replace(unpacked)
    return unpacked


def seed(directory: Path, user_data: str, name: str) -> Path:
    (directory / "user-data").write_text(user_data, encoding="utf-8", newline="\n")
    (directory / "meta-data").write_text(f"instance-id: {name}\nlocal-hostname: freesense-builder\n", encoding="utf-8", newline="\n")
    target = directory / "seed.img"
    if shutil.which("cloud-localds"):
        command = ["cloud-localds", str(target), str(directory / "user-data"), str(directory / "meta-data")]
    else:
        maker = ["xorriso", "-as", "mkisofs"] if shutil.which("xorriso") else [shutil.which("genisoimage") or "mkisofs"]
        command = [*maker, "-quiet", "-output", str(target), "-volid", "cidata", "-joliet", "-rock",
                   str(directory / "user-data"), str(directory / "meta-data")]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return target


def archive(target: Path, root: Path, names: list[str], exclude: tuple = ()) -> Path:
    """A tar file used as a raw disk. GNU tar pads to 10 KiB records, a whole number of sectors."""
    present = [name for name in names if (root / name).exists()]
    command = ["tar", "-cf", str(target), "-C", str(root), *[f"--exclude={pattern}" for pattern in exclude]]
    subprocess.run(command + (present if present else ["--files-from=/dev/null"]), check=True)
    return target


def guest_script(nonce: str, repository: str, packages: list[str], work_disk: bool, body: str) -> str:
    """user-data: runs once as root, before the image's first-boot upgrade, which is switched off first."""
    setup = [
        "#!/bin/sh",
        "exec </dev/null >/dev/ttyu0 2>&1",
        f"echo BEGIN-{nonce}",
        # rc runs this script before firstboot_pkg_upgrade; an upgraded host is not the pinned one.
        "sysrc -q firstboot_pkg_upgrade_enable=NO >/dev/null",
        "rm -f /usr/local/etc/rc.d/firstboot_pkg_upgrade",
        "disk() { for d in $(sysctl -n kern.disks); do [ \"$(diskinfo -s /dev/$d 2>/dev/null)\" = \"$1\" ] && { echo /dev/$d; return; }; done;"
        " echo \"no disk $1\" >&2; return 1; }",
        "main() {",
        "set -eu",
        # rc's own PATH has no /usr/local.
        "export PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin HOME=/root",
    ]
    # The CURRENT image runs lock-order checks and a debugging malloc; neither belongs in a build, and both cost time.
    setup += ["sysctl debug.witness.watch=0 >/dev/null 2>&1 || :", "export MALLOC_CONF=junk:false"]
    if work_disk:
        setup += ["kldload -n zfs",
                  "sysctl vfs.zfs.arc.max=2147483648 >/dev/null",
                  # A kept disk (FREESENSE_VM_DISK) has the pool of the last run; a new one is empty.
                  "zpool import -f -d \"$(disk fswork)\" work 2>/dev/null ||"
                  " zpool create -f -O compression=lz4 -O atime=off -O mountpoint=/work work \"$(disk fswork)\"",
                  "zpool set autotrim=on work",
                  "mkdir -p /work/tmp",
                  "export TMPDIR=/work/tmp"]
    else:
        setup += ["mkdir -p /work"]
    if packages:
        setup += ["mkdir -p /root/repos",
                  f"printf '%s\\n' {shlex.quote(repository)} > /root/repos/Locked.conf",
                  f"REPOS_DIR=/root/repos IGNORE_OSVERSION=yes ASSUME_ALWAYS_YES=yes pkg install -y {' '.join(packages)} >/root/pkg.log 2>&1"
                  " || { tail -20 /root/pkg.log; return 70; }",
                  f"pkg query '%n-%v' {' '.join(packages)} > /work/builder-packages.txt"]
    return "\n".join([*setup, body, "}", "status=0", "( main ) || status=$?", f"echo END-{nonce} $status", "sync",
                      "shutdown -p now", ""])


LIVE = "live.log"
LIVE_PORT = "freesense.live"
started_live: set = set()


def boot(directory: Path, base: Path, disks: dict, user_data_for, work_gib: int, timeout: int, live: Path | None = None) -> None:
    """Run one VM. `disks` maps an identifier to (file, read-only). Streams the guest's output; fails on its status.
    `live` receives, through a virtio port, the whole output of the stages; it is not printed."""
    cpus, memory = size()
    nonce = secrets.token_hex(8)
    overlay = directory / "system.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base.resolve()), str(overlay), "20G"], check=True)
    code, variables = firmware()
    shutil.copyfile(variables, directory / "vars.fd")
    arguments = ["qemu-system-x86_64", "-name", f"freesense-{nonce}", "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", str(cpus),
                 "-m", str(memory), "-display", "none", "-monitor", "none", "-no-reboot",
                 "-drive", f"if=pflash,format=raw,readonly=on,file={code}", "-drive", f"if=pflash,format=raw,file={directory / 'vars.fd'}",
                 "-drive", f"if=virtio,format=qcow2,discard=unmap,file={overlay}",
                 "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0,romfile="]
    if work_gib:
        # A persistent machine can keep the work disk, so the obj tree of a failed or repeated base stage is used again.
        work = Path(os.environ.get("FREESENSE_VM_DISK") or directory / "work.qcow2").resolve()
        if work.suffix != ".qcow2":
            raise ValueError("FREESENSE_VM_DISK names a .qcow2 file")
        if not work.is_file():
            work.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(work), f"{work_gib}G"], check=True)
        disks = {"fswork": (work, False), **disks}
    for index, (ident, (path, readonly)) in enumerate(disks.items()):
        form = "qcow2" if path.suffix == ".qcow2" else "raw"
        arguments += ["-drive", f"if=none,id=d{index},format={form},discard=unmap,file={path}" + (",readonly=on" if readonly else ""),
                      "-device", f"virtio-blk-pci,drive=d{index},serial={ident}"]
    cidata = seed(directory, user_data_for(nonce), f"freesense-{nonce}")
    arguments += ["-drive", f"if=ide,media=cdrom,format=raw,readonly=on,file={cidata}"]
    serial = directory / "serial.log"
    serial.write_bytes(b"")
    arguments += ["-serial", f"file:{serial}"]
    if live:
        # One file for all the VMs of one run; a new run starts it again.
        if live not in started_live:
            live.write_bytes(b"")
            started_live.add(live)
        arguments += ["-device", "virtio-serial-pci", "-chardev", f"file,id=live,path={live},append=on",
                      "-device", f"virtserialport,chardev=live,name={LIVE_PORT}"]
        print(f"  full stage output: {live}", flush=True)
    print(f"  starting the build VM: {cpus} CPUs, {memory} MiB", flush=True)
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL)
    peak = [0, 0.0]

    def sample() -> None:
        if time.monotonic() - peak[1] >= 30:
            peak[0], peak[1] = max(peak[0], allocated(directory)), time.monotonic()

    try:
        stream_serial(process, serial, nonce, timeout, sample)
    finally:
        print(f"  build VM files used up to {max(peak[0], allocated(directory)) / GIB:.1f} GiB of host disk", flush=True)
        if process.poll() is None:
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def clean(line: str) -> str:
    return re.sub(r"\x1b\[[0-9;?=]*[A-Za-z]|[\x00-\x08\x0b-\x1f]", "", line)


def allocated(directory: Path) -> int:
    """Bytes the files under a directory really occupy (sparse disks count what was written)."""
    total = 0
    for path in directory.rglob("*"):
        try:
            status = path.lstat()
        except OSError:
            continue
        total += getattr(status, "st_blocks", 0) * 512
    return total


def stream_serial(process: subprocess.Popen, serial: Path, nonce: str, timeout: int, sample=lambda: None) -> None:
    started, position, pending, begun = time.monotonic(), 0, "", False
    while True:
        sample()
        ended = process.poll() is not None
        with serial.open("rb") as stream:
            stream.seek(position)
            data = stream.read()
        position += len(data)
        pending += data.decode("utf-8", "replace")
        *lines, pending = pending.split("\n")
        for line in map(clean, lines):
            if f"BEGIN-{nonce}" in line:
                begun = True
            elif (found := re.search(rf"END-{nonce} (\d+)", line)):
                if found[1] != "0":
                    raise ValueError(f"the build VM reported exit code {found[1]} (see the lines above)")
                return
            elif begun:
                print(line, flush=True)
        elapsed = time.monotonic() - started
        if ended or elapsed >= timeout or (not begun and elapsed >= BOOT_TIMEOUT):
            tail = [clean(line) for line in serial.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]]
            if not begun:
                print("\n".join(tail), flush=True)
            reason = "stopped without a result" if ended else f"exceeded {timeout} s" if elapsed >= timeout else "did not start the job"
            raise ValueError(f"the build VM {reason}")
        time.sleep(2)


def interrupted(*_):
    raise KeyboardInterrupt


def empty(directory: Path) -> None:
    """Remove everything but the live log, which outlives the VM."""
    for path in directory.iterdir() if directory.is_dir() else []:
        if path.name == LIVE:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


class Scratch:
    """The VM's files, removed whatever happens."""

    def __init__(self, directory: Path):
        self.directory = directory

    def __enter__(self) -> Path:
        empty(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.previous = signal.signal(signal.SIGTERM, interrupted)
        return self.directory

    def __exit__(self, *_) -> None:
        signal.signal(signal.SIGTERM, self.previous)
        empty(self.directory)
        if self.directory.is_dir() and not any(self.directory.iterdir()):
            self.directory.rmdir()


def sparse(path: Path, size_gib: int) -> Path:
    with path.open("wb") as stream:
        stream.truncate(size_gib * GIB)
    return path


# What goes into the build VM and comes back. The patched source, obj, ports tree and git caches stay inside it.
RESULTS = ["state", "out", "cache/catalogue", "cache/distfiles"]
TREE = ["freesense", "freesense-native", "freesense-system-ports", "freesense-os-base"]
# Free space on the host, by the stages that run: the image, the VM's disks and the results (twice, in transit).
NEEDED_GIB = {"base": 35, "other": 20}
PASSED_ON = ("FREESENSE_BUILD_ID", "FREESENSE_JOBS", "FREESENSE_REQUIRE_SIGNED")


def run_stages(context, last: str, forced_from: str | None, builds_base: bool) -> None:
    """Run the driver up to `last` inside the pinned image, with this work directory's results."""
    preflight(context.work, NEEDED_GIB["base" if builds_base else "other"])
    lock = context.lock
    base = image(lock, context.cache / "images")
    environment = {name: os.environ[name] for name in PASSED_ON if os.environ.get(name)}
    if os.environ.get("GITHUB_ACTIONS"):
        environment["FREESENSE_REQUIRE_SIGNED"] = "1"
    options = ["--work", "/work", "--offline"] + (["--from", forced_from] if forced_from else [])
    if context.public_key_given:
        options += ["--public-key", "/work/public-key"]
    with Scratch(context.work / "vm") as directory:
        tree = archive(directory / "tree.raw", context.workspace, TREE, (".git", "__pycache__", "local-output", "node_modules"))
        given = directory / "given"
        given.mkdir()
        if context.public_key_given:
            shutil.copyfile(context.public_key, given / "public-key")
        if context.mirror:
            # The package files and distfiles of a local mirror; git repositories are fetched by the VM itself.
            (given / "mirror").mkdir()
            for path in context.mirror.iterdir():
                if path.is_file():
                    shutil.copyfile(path, given / "mirror" / path.name)
            options += ["--mirror", "/work/mirror"]
        incoming = archive(directory / "in.raw", context.work, [name for name in RESULTS if (context.work / name).exists()])
        extra = archive(directory / "given.raw", given, [p.name for p in given.iterdir()])
        outgoing = sparse(directory / "out.raw", 24)
        exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in environment.items())
        body = "\n".join([
            # The port with the full stage output; the build goes on without it.
            "kldload -n virtio_console 2>/dev/null || :",
            f"sleep 1; [ ! -c /dev/vtcon/{LIVE_PORT} ] || export FREESENSE_LIVE_LOG=/dev/vtcon/{LIVE_PORT}",
            # On a kept work disk this run's tree and results replace the earlier ones.
            "rm -rf /work/tree /work/state /work/out /work/cache/catalogue /work/builder-packages.txt",
            # --from base or earlier builds world and kernel again.
            "rm -f /work/obj/.*-done" if forced_from in ("check", "source", "base") else "true",
            "mkdir -p /work/tree",
            "tar -xf \"$(disk fstree)\" -C /work/tree",
            "tar -xf \"$(disk fsin)\" -C /work",
            "tar -xf \"$(disk fsgiven)\" -C /work",
            "chown -R root:wheel /work",
            exports + "status=0",
            f"python3 /work/tree/freesense-os-base/build/build {shlex.join(options)} {last} || status=$?",
            # Finished stages come back also after a failure, so the next run continues from them.
            "cd /work",
            "have=",
            f"for name in {' '.join(RESULTS)}; do [ ! -e \"$name\" ] || have=\"$have $name\"; done",
            "tar -cf \"$(disk fsout)\" $have",
            "echo \"  build VM work disk: $(zpool list -H -o allocated work) allocated (lz4)\"",
            "return $status",
        ])
        disks = {"fstree": (tree, True), "fsin": (incoming, True), "fsgiven": (extra, True), "fsout": (outgoing, False)}
        repository = f'Locked: {{ url: "{lock["packages"]["repository"]}", signature_type: "fingerprints", ' \
                     'fingerprints: "/usr/share/keys/pkg", enabled: yes }'
        try:
            boot(directory, base, disks,
                 lambda nonce: guest_script(nonce, repository, lock["build_host"]["packages"], True, body),
                 80, int(os.environ.get("FREESENSE_VM_TIMEOUT") or 5.5 * 3600), live=directory / LIVE)
        finally:
            collect(outgoing, context.work)


def collect(outgoing: Path, work: Path) -> None:
    """Replace the results with the ones the VM wrote; keep the old ones when it wrote none."""
    with outgoing.open("rb") as stream:
        header = stream.read(512)
    if header[257:262] != b"ustar":
        return
    staging = outgoing.parent / "results"
    staging.mkdir()
    subprocess.run(["tar", "-xf", str(outgoing), "-C", str(staging), "--no-same-owner"], check=True)
    for name in RESULTS:
        if (staging / name).exists():
            shutil.rmtree(work / name, ignore_errors=True)
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (staging / name).rename(work / name)
