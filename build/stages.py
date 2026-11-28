"""The FreeBSD stages: base (world, kernel, pkgbase), deps, freesense, repo and iso.

Each writes its results under out/<stage>/ and out/<stage>.json, the record with checksums that later stages read and are
keyed by. They run as root on the pinned FreeBSD host; on Linux build/vm.py provides that host.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time

import packages
import source

BUILD = Path(__file__).resolve().parent
GUEST = BUILD / "guest"
PUBLIC_KEY = BUILD.parent / "keys/freesense-2.0-pkg.pub"
KERNEL = "FREESENSE"
LABEL = "FREESENSE_INSTALL"
HEARTBEAT = 300
MARKER = {"check": "packages"}


class Sink:
    """The whole output, as it is written, for someone following along. A failing sink never fails the build."""

    def __init__(self, path: str | None):
        self.stream = None
        if path:
            try:
                self.stream = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
            except OSError as error:
                print(f"    no live log: {error}", flush=True)

    def write(self, text: str) -> None:
        if self.stream:
            try:
                self.stream.write(text)
            except OSError:
                self.close()

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream:
            try:
                stream.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.close()


def live_log() -> Sink:
    return Sink(os.environ.get("FREESENSE_LIVE_LOG"))


def run_logged(argv: list, log: Path, what: str, show: str, environment: dict | None = None) -> None:
    """Run with the whole output in `log`. Print the lines matching `show`, a heartbeat, and on failure the real cause."""
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {what} (log: {log})", flush=True)
    pattern, started, last = re.compile(show), time.monotonic(), ""
    lines: queue.Queue = queue.Queue()
    with log.open("w", encoding="utf-8", errors="replace") as stream, live_log() as live:
        live.write(f"\n===== {what} ({log.name}) {time.strftime('%H:%M:%S')} UTC =====\n")
        process = subprocess.Popen([str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", env={**os.environ, **(environment or {})})

        def read() -> None:
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        quiet = time.monotonic()
        while True:
            try:
                line = lines.get(timeout=10)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                stream.write(line)
                live.write(line)
                last = line.rstrip()
                if pattern.search(line):
                    print(f"    {line.rstrip()[:200]}", flush=True)
                    quiet = time.monotonic()
            if time.monotonic() - quiet >= HEARTBEAT:
                stream.flush()
                print(f"    ... {what}: {(time.monotonic() - started) / 60:.0f} min, now: ...{last[-150:]}", flush=True)
                quiet = time.monotonic()
        status = process.wait()
    if status:
        text = log.read_text(encoding="utf-8", errors="replace").splitlines()
        errors = [line for line in text if re.search(r"(^|\s)(error:|ERROR:|\*\*\* (Error|\[))|^FAIL|Stop in ", line)]
        print(f"---- {what}: exit code {status}. First error lines:", flush=True)
        print("\n".join(errors[:30]), flush=True)
        print(f"---- last 80 lines of {log}:", flush=True)
        print("\n".join(text[-80:]), flush=True)
        print("----", flush=True)
        raise ValueError(f"{what} failed with exit code {status} (log: {log})")


def require_builder(context) -> None:
    if not sys.platform.startswith("freebsd") or os.geteuid() != 0:
        raise ValueError("this stage runs as root on the pinned FreeBSD host; run build/build on Linux with KVM to get one")
    release = int(subprocess.run(["sysctl", "-n", "kern.osreldate"], capture_output=True, text=True, check=True).stdout)
    want = context.lock["freebsd_source"]["osversion"]
    if release != want:
        raise ValueError(f"the build host is __FreeBSD_version {release}, the lock pins {want}: use the pinned image, not upgraded")


def jobs() -> int:
    """One job per CPU, but at least 1.5 GiB of memory each: the heavy C++ files need it."""
    configured = os.environ.get("FREESENSE_JOBS")
    if configured:
        return max(1, int(configured))
    cpus = os.cpu_count() or 1
    memory = int(subprocess.run(["sysctl", "-n", "hw.physmem"], capture_output=True, text=True, check=True).stdout)
    return max(1, min(cpus, int(memory / (1.5 * 1024 ** 3))))


def record(context, stage: str) -> dict:
    path = context.out / f"{MARKER.get(stage, stage)}.json"
    if not path.is_file():
        raise ValueError(f"the {stage} stage has no result ({path}); run it first")
    return json.loads(path.read_text(encoding="utf-8"))


def write_record(context, stage: str, value: dict) -> None:
    (context.out / f"{stage}.json").write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8", newline="\n")


def remove_tree(directory: Path) -> None:
    # Installed roots hold files with the system immutable flag.
    if sys.platform.startswith("freebsd"):
        subprocess.run(["chflags", "-R", "noschg", str(directory)], check=False)
    shutil.rmtree(directory)


def fresh(directory: Path) -> Path:
    if directory.exists():
        remove_tree(directory)
    directory.mkdir(parents=True)
    return directory


def listing(directory: Path) -> list[dict]:
    return [{"file": path.name, "sha256": source.sha256(path), "size": path.stat().st_size} for path in sorted(directory.glob("*.pkg"))]


def verified(directory: Path, files: list[dict]) -> list[Path]:
    """The files of an earlier stage, as that stage recorded them."""
    paths = []
    for entry in files:
        path = directory / entry["file"]
        if not path.is_file() or source.sha256(path) != entry["sha256"]:
            raise ValueError(f"{path} is missing or differs from the stage record; run the stage that made it again")
        paths.append(path)
    return paths


def link(path: Path, target: Path) -> None:
    try:
        os.link(path, target)
    except OSError:
        shutil.copyfile(path, target)


def query(package: Path, form: str, pkg: str = "pkg") -> str:
    return subprocess.run([pkg, "query", "-F", str(package), form], check=True, capture_output=True, text=True).stdout.strip()


def base(context) -> None:
    require_builder(context)
    context.ensure_source()
    repository = context.work / "base-repo"
    fresh(repository)
    run_logged(["sh", GUEST / "base.sh", context.work / "src", context.work / "obj", repository, jobs(), KERNEL, BUILD / "nasm.sh"],
               context.logs / "base.log", "world, kernel and base packages", r"^(>>> |build/base:)")
    built = sorted((p for p in repository.glob("*/*/*.pkg") if not p.is_symlink() and not p.parent.is_symlink()), key=lambda p: p.name)
    versions = {path.parent.name for path in built}
    if len(versions) != 1:
        raise ValueError(f"expected one package set under {repository}, found {sorted(versions)}")
    target = fresh(context.out / "base")
    for path in built:
        # pkg repo writes its own catalogue files next to the packages.
        if path.name.startswith("FreeSense-"):
            link(path, target / path.name)
    version = versions.pop()
    names = {query(path, "%n") for path in sorted(target.glob("*.pkg"))}
    provided = json.loads((BUILD / "base-provides.json").read_text(encoding="utf-8"))
    missing = sorted(set(provided["packages"]) - names)
    if missing:
        raise ValueError(f"base-provides.json records packages this build did not make: {', '.join(missing)}")
    write_record(context, "base", {"version": version, "packages": listing(target)})
    print(f"    {len(names)} base packages, version {version}", flush=True)


def catalogue_files(context) -> list[tuple[str, Path]]:
    """Download exactly what check resolved. The catalogue's sum is the SHA-256 of the package file."""
    resolved = record(context, "check")
    cache = context.cache / "packages"
    files = []
    for entry in resolved["packages"]:
        name = f"{entry['name']}-{entry['version']}.pkg"
        url = f"{resolved['repository']}/{entry['repopath']}"
        files.append((name, source.fetch_file(cache / name, [url], entry["sum"], context.mirror)))
    return files


def ports_make_conf(custom: dict) -> str:
    lines = ["# Written by build/stages.py from lock.json.", "ALLOW_UNSUPPORTED_SYSTEM=yes", "IGNORE_OSVERSION=yes",
             "DEFAULT_VERSIONS=\tssl=base", "OPTIONS_UNSET+=\tDOCS EXAMPLES"]
    for origin, port in sorted(custom.items()):
        name = origin.replace("/", "_")
        for key, variable in (("set", "SET_FORCE"), ("unset", "UNSET_FORCE")):
            if port.get(key):
                lines.append(f"{name}_{variable}=\t{' '.join(port[key])}")
    return "\n".join(lines) + "\n"


def option_problems(origin: str, port: dict, built: dict) -> list[str]:
    """`built` is what the package says it was built with; every wanted option must be there."""
    wrong = [f"{origin}: option {name} is not on" for name in port.get("set", []) if built.get(name) != "on"]
    return wrong + [f"{origin}: option {name} is on" for name in port.get("unset", []) if built.get(name) == "on"]


def deps(context) -> None:
    require_builder(context)
    target = fresh(context.out / "deps")
    print(f"  downloading {len(record(context, 'check')['packages'])} packages", flush=True)
    for name, path in catalogue_files(context):
        link(path, target / name)
    custom = context.lock["custom_ports"]
    built = []
    if custom:
        ports = context.ensure_ports()
        work = fresh(context.work / "custom-ports")
        (work / "make.conf").write_text(ports_make_conf(custom), encoding="utf-8", newline="\n")
        output = fresh(work / "out")
        run_logged(["sh", GUEST / "ports.sh", ports, work / "make.conf", work, output, context.cache / "distfiles", *sorted(custom)],
                   context.logs / "custom-ports.log", "our own builds of third-party ports",
                   r"^(build/ports:|===>  (Building|Installing|Registering) |\*\*\*)", context.pkg_environment())
        known = {entry["name"] for entry in record(context, "check")["packages"]} | {port["package"] for port in custom.values()}
        problems = []
        for origin, port in sorted(custom.items()):
            found = [p for p in output.glob("*.pkg") if query(p, "%n") == port["package"]]
            if len(found) != 1:
                raise ValueError(f"{origin}: expected one package {port['package']}, found {len(found)}")
            options = dict(line.split(" ", 1) for line in query(found[0], "%Ok %Ov").splitlines())
            problems += option_problems(origin, port, options)
            needs = {line for line in query(found[0], "%dn").splitlines() if line}
            problems += [f"{origin}: runs with {name}, which is not in the set (add it to its runtime list in lock.json)"
                         for name in sorted(needs - known)]
            shutil.copyfile(found[0], target / found[0].name)
            built.append({"origin": origin, "file": found[0].name, "options_on": sorted(k for k, v in options.items() if v == "on")})
        if problems:
            raise ValueError("\n".join(problems))
    write_record(context, "deps", {"packages": listing(target), "built_from_ports": built,
                                   "ports_commit": context.lock["ports_source"]["commit"]})


def package_version(build_id: str | None) -> str | None:
    """2.0.0-dev.20261128.7 -> 2.0.0.a.20261128.7, which pkg orders below the 2.0.0 release."""
    if not build_id:
        return None
    found = re.fullmatch(r"(\d+\.\d+\.\d+)-dev\.(\d{8})\.(\d+)", build_id)
    if not found:
        raise ValueError(f"FREESENSE_BUILD_ID is not X.Y.Z-dev.YYYYMMDD.N: {build_id}")
    return f"{found[1]}.a.{found[2]}.{found[3]}"


def freesense(context) -> None:
    require_builder(context)
    dependencies = context.out / "deps"
    verified(dependencies, record(context, "deps")["packages"])
    ports = context.ensure_ports()
    target = fresh(context.out / "freesense")
    version = package_version(os.environ.get("FREESENSE_BUILD_ID"))
    command = ["sh", context.workspace / packages.PORTS / "build-packages.sh", "-p", ports, "-d", dependencies, "-o", target,
               "-w", context.work / "ports-work", "-s", context.workspace]
    if version:
        command += ["-v", version]
    run_logged(command, context.logs / "freesense.log", "FreeSense packages", r"^(build-packages:|PACKAGES-READY|\*\*\*)")
    write_record(context, "freesense", {"packages": listing(target)})


def pkg_static(context, directory: Path) -> Path:
    """pkg-static of the package set itself, not whatever the host has."""
    found = sorted((context.out / "deps").glob("pkg-[0-9]*.pkg"))
    if len(found) != 1:
        raise ValueError("the package set must hold exactly one pkg package")
    return extract_pkg_static(found[0], directory)


def extract_pkg_static(package: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-xf", str(package), "-C", str(directory), "-s", ",.*/,,", "/usr/local/sbin/pkg-static"], check=True)
    return directory / "pkg-static"


def installer_list(context) -> list[str]:
    return packages.installer_packages((context.workspace / packages.INSTALLER).read_text(encoding="utf-8"))


def abi(context) -> str:
    return context.lock["targets"][context.lock["target"]]["abi"]


def repo(context) -> None:
    require_builder(context)
    scratch = fresh(context.work / "repo-work")
    everything = fresh(scratch / "all" / "All")
    for stage in ("base", "deps", "freesense"):
        for path in verified(context.out / stage, record(context, stage)["packages"]):
            if (everything / path.name).exists():
                raise ValueError(f"two stages made {path.name}")
            link(path, everything / path.name)
    media = fresh(context.out / "repo")
    tool = pkg_static(context, scratch / "tool")
    run_logged(["sh", GUEST / "repo.sh", tool, everything.parent, media, scratch / "resolve", abi(context), *installer_list(context)],
               context.logs / "repo.log", "repository and closure check", r"^(build/repo:|REPOSITORY-READY)")
    write_record(context, "repo", {"packages": listing(media / "All"), "available": len(list(everything.glob("*.pkg")))})
    remove_tree(scratch)


def signed(repository: Path) -> bool:
    """pkg puts the signature next to the catalogue, inside packagesite.pkg."""
    archive = repository / "packagesite.pkg"
    listed = subprocess.run(["tar", "-tf", str(archive)], check=True, capture_output=True, text=True).stdout.split()
    return any(name.endswith((".sig", ".pub")) or name in ("signature", "pubkey") for name in listed)


def iso_name(system_package: str, target: str, build_id: str | None) -> str:
    found = re.fullmatch(r"FreeSense-system-(?:php\d+-)?([0-9][0-9a-z._]*)\.pkg", system_package)
    if not found:
        raise ValueError(f"cannot read the System version from {system_package}")
    return f"FreeSense-{build_id or found[1]}-{target}.iso"


def iso(context) -> None:
    require_builder(context)
    repository = context.out / "repo"
    verified(repository / "All", record(context, "repo")["packages"])
    system = [e["file"] for e in record(context, "freesense")["packages"] if e["file"].startswith("FreeSense-system-")]
    if len(system) != 1:
        raise ValueError("expected exactly one FreeSense-system package")
    is_signed = signed(repository)
    if not is_signed:
        if os.environ.get("FREESENSE_REQUIRE_SIGNED"):
            raise ValueError("the repository is not signed; sign it (build/sign-repo) before the iso stage")
        print("    WARNING: the repository is not signed; this ISO installs without verifying it (test builds only)", flush=True)
    build_id = os.environ.get("FREESENSE_BUILD_ID")
    name = iso_name(system[0], context.lock["target"], build_id)
    target = fresh(context.out / "iso")
    scratch = fresh(context.work / "iso-work")
    tool = pkg_static(context, scratch / "tool")
    run_logged(["sh", GUEST / "iso.sh", repository, target / name, scratch, tool, abi(context), LABEL,
                context.public_key if is_signed else "-", *installer_list(context)],
               context.logs / "iso.log", "installer ISO", r"^(build/iso:|ISO-READY|ERROR)")
    remove_tree(scratch)
    digest = source.sha256(target / name)
    (target / "SHA256SUMS").write_text(f"{digest}  {name}\n", encoding="utf-8", newline="\n")
    manifest = {"schema": "freesense.build-manifest/v1", "build_id": build_id, "iso": name, "sha256": digest,
                "size": (target / name).stat().st_size, "label": LABEL, "repository_signed": is_signed,
                "repository_key_sha256": source.sha256(context.public_key) if is_signed else None,
                "inputs": {"freebsd_commit": context.lock["freebsd_source"]["commit"],
                           "ports_commit": context.lock["ports_source"]["commit"],
                           "builder_image_sha256": context.lock["builder_image"]["sha256"]},
                "source": record(context, "source"), "base_version": record(context, "base")["version"],
                "third_party": record(context, "check")["packages"], "built_from_ports": record(context, "deps")["built_from_ports"],
                "freesense": record(context, "freesense")["packages"], "media_packages": record(context, "repo")["packages"],
                "builder_packages": context.builder_packages()}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    write_record(context, "iso", {"iso": name, "sha256": digest, "size": manifest["size"], "repository_signed": is_signed})
    print(f"    {name}: {manifest['size'] / 1024 ** 2:.0f} MiB, sha256 {digest}", flush=True)


def smoke(context) -> None:
    entry = record(context, "iso")
    image = context.out / "iso" / entry["iso"]
    if source.sha256(image) != entry["sha256"]:
        raise ValueError(f"{image} differs from the iso record")
    command = [sys.executable, str(BUILD / "smoke.py"), str(image), "--logs", str(context.logs), "--install"]
    if entry["repository_signed"]:
        command.append("--signed")
    status = subprocess.run(command).returncode
    if status:
        raise ValueError(f"smoke test failed with exit code {status} (serial logs in {context.logs})")
    write_record(context, "smoke", {"iso": entry["iso"], "sha256": entry["sha256"], "result": "passed"})
