#!/usr/bin/env python3
"""Smoke test of the installer ISO under QEMU/KVM, over the serial line: smoke.py ISO [--logs DIR] [--install] [--signed]

Boots the ISO four ways (UEFI and BIOS, as a CD and as a USB stick) and waits for the installer menu. With --install it
also installs from the menu's rescue shell with an unattended file (UFS and ZFS, under UEFI and BIOS), then boots the
installed disk and waits for its console. --signed expects the installation to verify the repository's signature.
Needs qemu-system-x86_64, qemu-img, OVMF and /dev/kvm. No network reaches the guest.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vm  # noqa: E402

MENU = r"Welcome to FreeSense"
INSTALLED = r"freesense>|Enter an option|FreeSense[^\n]{0,60}console"
FAILURES = r"panic:|Fatal trap|mountroot>|Mounting from [^\n]* failed|Enter full pathname of shell"
BOOTS = (("uefi-cd", True, "cd"), ("bios-cd", False, "cd"), ("uefi-usb", True, "usb"), ("bios-usb", False, "usb"))
INSTALLS = (("uefi-zfs", True, "zfs"), ("bios-ufs", False, "ufs"), ("uefi-ufs", True, "ufs"), ("bios-zfs", False, "zfs"))
BOOT_TIMEOUT, INSTALL_TIMEOUT = 420, 900


class Machine:
    def __init__(self, directory: Path, log: Path, uefi: bool, iso: Path | None, medium: str, disk: Path | None):
        self.log, self.text, self.position = log, "", 0
        arguments = ["qemu-system-x86_64", "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", "2", "-m", "3072",
                     "-display", "none", "-monitor", "none", "-no-reboot", "-serial", "stdio"]
        if uefi:
            code, variables = vm.firmware()
            store = directory / "vars.fd"
            if not store.is_file():
                shutil.copyfile(variables, store)
            arguments += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}", "-drive", f"if=pflash,format=raw,file={store}"]
        if iso is not None and medium == "cd":
            arguments += ["-drive", f"if=none,id=medium,media=cdrom,format=raw,readonly=on,file={iso}",
                          "-device", "ide-cd,drive=medium,bootindex=1"]
        elif iso is not None:
            # The same file written to a stick: a disk with the ISO's partition table. Writes are thrown away.
            arguments += ["-device", "qemu-xhci", "-drive", f"if=none,id=medium,format=raw,snapshot=on,file={iso}",
                          "-device", "usb-storage,drive=medium,bootindex=1"]
        if disk is not None:
            arguments += ["-drive", f"if=none,id=disk,format=qcow2,file={disk}", "-device", "virtio-blk-pci,drive=disk,bootindex=2"]
        for number in range(2):
            arguments += ["-netdev", f"user,id=n{number},restrict=on", "-device", f"virtio-net-pci,netdev=n{number},romfile="]
        self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        with self.log.open("ab") as stream:
            while block := self.process.stdout.read1(65536):
                stream.write(block)
                stream.flush()
                self.text += block.decode("utf-8", "replace")

    def expect(self, pattern: str, timeout: int, what: str) -> str:
        """Wait for `pattern` in the output not yet consumed; terminal control sequences are ignored."""
        import re
        deadline = time.monotonic() + timeout
        while True:
            alive = self.process.poll() is None
            if not alive:
                self.reader.join(timeout=10)
            seen = vm.clean(self.text[self.position:])
            if found := re.search(pattern, seen):
                self.position = len(self.text)
                return found[0]
            if bad := re.search(FAILURES, seen):
                raise ValueError(f"{what}: the guest reported '{bad[0]}'")
            if not alive:
                raise ValueError(f"{what}: the machine stopped before it appeared")
            if time.monotonic() >= deadline:
                raise ValueError(f"{what}: not seen within {timeout} s")
            time.sleep(1)

    def send(self, text: str, pause: float = 0.3) -> None:
        self.process.stdin.write(text.encode())
        self.process.stdin.flush()
        time.sleep(pause)

    def stop(self, wait: int = 0) -> None:
        try:
            self.process.wait(timeout=wait or 0.1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=10)


def boot_case(iso: Path, directory: Path, log: Path, uefi: bool, medium: str) -> None:
    machine = Machine(directory, log, uefi, iso, medium, None)
    try:
        machine.expect(MENU, BOOT_TIMEOUT, "the installer menu")
    finally:
        machine.stop()


def configuration(filesystem: str) -> str:
    return json.dumps({"storage": {"disks": ["vtbd0"], "filesystem": filesystem, "swap": 512},
                       "settings": {"version": 1, "wan": {"device": "vtnet0"},
                                    "lan": {"device": "vtnet1", "address": "192.168.77.1", "prefix": 24}},
                       "repository": "file:///media/repo", "admin_password": f"smoke-{secrets.token_hex(8)}"}, indent=1)


def install_case(iso: Path, directory: Path, log: Path, uefi: bool, filesystem: str, signed: bool) -> None:
    disk = directory / "disk.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), "16G"], check=True)
    machine = Machine(directory, log, uefi, iso, "cd", disk)
    try:
        machine.expect(MENU, BOOT_TIMEOUT, "the installer menu")
        time.sleep(3)
        machine.send("4", 1)  # Rescue Shell
        machine.send("\r", 3)
        machine.send("echo READY-$((40+2))\n")
        machine.expect(r"READY-42", 60, "the rescue shell")
        machine.send("cat > /tmp/c.json <<'END'\n")
        for line in configuration(filesystem).splitlines():
            machine.send(line + "\n", 0.1)
        machine.send("END\n", 1)
        machine.send("freesense-install install --config /tmp/c.json --yes; echo RESULT-$?\n")
        result = machine.expect(r"RESULT-[0-9]+", INSTALL_TIMEOUT, "the end of the installation")
        if result != "RESULT-0":
            raise ValueError(f"the installation ended with {result}")
        machine.send("echo VERIFY-$(grep -o 'signature_type: \"[a-z]*\"' /tmp/fs-repos/FreeSense.conf | cut -d'\"' -f2)\n")
        verified = machine.expect(r"VERIFY-[a-z]+", 60, "the repository configuration of the installation")
        wanted = "VERIFY-pubkey" if signed else "VERIFY-none"
        if verified != wanted:
            raise ValueError(f"the installer used {verified}, expected {wanted}")
        machine.send("sync; /sbin/poweroff\n")
        machine.stop(wait=180)
    finally:
        machine.stop()
    machine = Machine(directory, log, uefi, None, "", disk)
    try:
        machine.expect(INSTALLED, BOOT_TIMEOUT, "the console of the installed system")
    finally:
        machine.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("iso", type=Path)
    parser.add_argument("--logs", type=Path, default=Path("smoke-logs"))
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--signed", action="store_true")
    parser.add_argument("--only", action="append", help="run only these cases, e.g. uefi-cd or install-bios-ufs")
    args = parser.parse_args()
    iso, logs = args.iso.resolve(), args.logs.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    cases = [(name, lambda d, log, u=uefi, m=medium: boot_case(iso, d, log, u, m)) for name, uefi, medium in BOOTS]
    if args.install:
        cases += [(f"install-{name}", lambda d, log, u=uefi, f=fs: install_case(iso, d, log, u, f, args.signed))
                  for name, uefi, fs in INSTALLS]
    if args.only:
        cases = [case for case in cases if case[0] in args.only]
        if not cases:
            parser.error("no case has that name")
    failed = []
    for name, case in cases:
        started = time.monotonic()
        log = logs / f"smoke-{name}.log"
        log.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="freesense-smoke.", dir=logs) as directory:
            try:
                case(Path(directory), log)
                print(f"  smoke {name}: ok in {time.monotonic() - started:.0f} s", flush=True)
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                failed.append(name)
                print(f"  smoke {name}: FAILED: {error}", flush=True)
                if log.is_file():
                    tail = vm.clean(log.read_text(encoding="utf-8", errors="replace")).splitlines()[-30:]
                    print("\n".join(f"    | {line[:200]}" for line in tail), flush=True)
    if failed:
        sys.exit(f"FAIL: {len(failed)} of {len(cases)} smoke cases: {', '.join(failed)}")


if __name__ == "__main__":
    main()
