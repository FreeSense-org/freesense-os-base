"""Unattended lab acceptance on the Proxmox lab (planning/proxmox-lab.md).

Build the FreeSense packages from the working tree with the real ports on the builder VM, update VM 150 (fs2-a) or install it fresh,
reboot it, run tests/appliance on it and basic traffic checks from VM 152 (fs2-client), then report and stop the VMs.
Only VMs 150 and 152 are started, changed or stopped. Passwords are never printed.
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
LAB = ROOT / "tools/lab"
FIREWALL, CLIENT = 150, 152
NAMES = {FIREWALL: "fs2-a", CLIENT: "fs2-client"}
BRIDGES = {FIREWALL: {"net0": "vmbr0", "net1": "vmbr2", "net2": "vmbr2"}, CLIENT: {"net0": "vmbr0", "net1": "vmbr2", "net2": "vmbr2"}}
LAN = "172.29.50.1"
CLIENT_LAN = "172.29.50.10"
# A public resolver used only as a routed destination through the firewall.
OUTSIDE = "9.9.9.9"


class LabError(Exception):
    """A step that the rest of the run depends on failed."""


def key_file(value: Path) -> str:
    """Git's ssh wants /c/... for the identity files that the lab ssh_config names."""
    text = value.resolve().as_posix()
    return re.sub(r"^([A-Za-z]):", lambda m: "/" + m[1].lower(), text)


def find_ssh() -> str:
    git = shutil.which("git")
    if os.name == "nt" and git:
        for parent in Path(git).resolve().parents:
            candidate = parent / "usr/bin/ssh.exe"
            if candidate.is_file():
                return str(candidate)
    ssh = shutil.which("ssh")
    if not ssh:
        raise LabError("ssh is required")
    return ssh


SETTINGS = ROOT / "local-output" / "lab-settings.json"


def setting(name: str, default: str | None = None) -> str:
    """A lab address or key path: environment first, then the ignored settings file. Neither is in the repository."""
    value = os.environ.get(name)
    if not value and SETTINGS.is_file():
        value = json.loads(SETTINGS.read_text(encoding="utf-8")).get(name)
    if not value and default is None:
        raise LabError(f"set {name} in the environment or in local-output/lab-settings.json (planning/proxmox-lab.md)")
    return value or default


class Lab:
    def __init__(self, arguments):
        self.ssh_path = find_ssh()
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        self.pve = [self.ssh_path, "-i", key_file(Path(setting("FREESENSE_PVE_KEY", str(home / ".ssh/id_ed25519")))),
                    "-o", f"UserKnownHostsFile={key_file(home / '.ssh/known_hosts')}", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    setting("FREESENSE_PVE")]
        self.builder_config = Path(setting("FREESENSE_BUILDER_SSH_CONFIG"))
        self.builder_host = setting("FREESENSE_BUILDER")
        self.keys = Path(setting("FREESENSE_LAB_KEYS"))
        # ssh starts the jump through the client in a shell with this same argument: it must not be a backslash path.
        self.lab_config = key_file(self.keys / "ssh_config")
        self.last_error = ""
        self.arguments = arguments
        self.results: list[tuple[str, bool, str]] = []
        self.log_dir = WORKSPACE / "local-output/lab-accept" / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()

    # ---- processes ----------------------------------------------------------------------------------------------
    def run(self, command: list[str], *, data: bytes | None = None, timeout: int = 120, check: bool = True) -> tuple[int, str]:
        options = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **options)
        except subprocess.TimeoutExpired:
            if check:
                raise LabError(f"timed out after {timeout} s: {command[-1][:80]}") from None
            return 124, "timed out"
        output = result.stdout.decode("utf-8", "replace")
        if check and result.returncode:
            raise LabError(f"exit {result.returncode}: {command[-1][:80]}\n{output[-2000:]}")
        return result.returncode, output

    def relay(self, source_command: list[str], target_command: list[str], timeout: int) -> tuple[bool, str]:
        """Stream one command's output into another's input, through this process: handing the source's pipe to the target
        directly stalls between two of Git's ssh processes on Windows."""
        with tempfile.TemporaryFile() as errors:
            source = subprocess.Popen(source_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
            target = subprocess.Popen(target_command, stdin=subprocess.PIPE, stdout=errors, stderr=errors)
            watchdog = threading.Timer(timeout, lambda: (source.kill(), target.kill()))
            watchdog.start()
            try:
                try:
                    shutil.copyfileobj(source.stdout, target.stdin, 1 << 20)
                    target.stdin.close()
                except OSError:
                    source.kill()
                ok = target.wait() == 0 and source.wait() == 0
            finally:
                watchdog.cancel()
                source.stdout.close()
            errors.seek(0)
            return ok, errors.read().decode("utf-8", "replace")[-500:]

    def host(self, command: str, **options) -> tuple[int, str]:
        return self.run([*self.pve, command], **options)

    def builder(self, command: str, **options) -> tuple[int, str]:
        return self.run([self.ssh_path, "-F", str(self.builder_config), self.builder_host, command], **options)

    def to_builder(self, data: bytes, path: str) -> None:
        self.builder(f"umask 077; cat > {path}", data=data, timeout=300)

    def guest_command(self, name: str, command: str) -> list[str]:
        return [self.ssh_path, "-F", self.lab_config, name, command]

    def guest(self, name: str, command: str, **options) -> tuple[int, str]:
        return self.run(self.guest_command(name, command), **options)

    # ---- reporting ----------------------------------------------------------------------------------------------
    def step(self, text: str) -> None:
        print(f"[{time.monotonic() - self.started:6.0f}s] {text}", flush=True)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append((name, passed, detail))
        print(f"  {'ok  ' if passed else 'FAIL'} {name}", flush=True)
        if not passed and detail:
            print("       " + detail.strip().replace("\n", "\n       ")[-1500:], flush=True)
        return passed

    def check(self, name: str, machine: str, command: str, expect: str | None = None, timeout: int = 90) -> bool:
        code, output = self.guest(machine, command, check=False, timeout=timeout)
        passed = code == 0 and (expect is None or re.search(expect, output) is not None)
        return self.record(name, passed, f"exit {code}: {output.strip()}")

    def wait(self, what: str, probe, seconds: int) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if probe():
                return
            time.sleep(3)
        raise LabError(f"{what}: not reached within {seconds} s {self.last_error}".rstrip())

    # ---- virtual machines ---------------------------------------------------------------------------------------
    def verify_machines(self) -> None:
        for vmid, bridges in BRIDGES.items():
            _, config = self.host(f"qm config {vmid}")
            if f"name: {NAMES[vmid]}" not in config.splitlines():
                raise LabError(f"VM {vmid} is not {NAMES[vmid]}; refusing to touch it")
            for nic, bridge in bridges.items():
                if not re.search(rf"^{nic}: [^\n]*bridge={bridge}\b", config, re.M):
                    raise LabError(f"VM {vmid} {nic} is not on {bridge}; refusing to start a firewall on the wrong network")

    def running(self, vmid: int) -> bool:
        return "running" in self.host(f"qm status {vmid}")[1]

    def start(self, vmid: int) -> None:
        if not self.running(vmid):
            self.host(f"qm start {vmid}")

    def stop(self, vmid: int) -> None:
        if self.running(vmid):
            self.host(f"qm shutdown {vmid} --timeout 120 --forceStop 1", timeout=180, check=False)

    def connect_client(self) -> None:
        """The client's management address comes from the home router's DHCP; follow it if it moved."""
        def reachable() -> bool:
            return self.guest("fs2-client", "true", check=False, timeout=20)[0] == 0

        end = time.monotonic() + 150
        while time.monotonic() < end:
            if reachable():
                return
            _, config = self.host(f"qm config {CLIENT}")
            mac = re.search(r"^net0: virtio=([0-9A-F:]+),", config, re.M)[1].lower()
            _, neighbours = self.host(f"ip neigh show | grep -i {mac} || true")
            found = re.findall(r"^(\d+\.\d+\.\d+\.\d+) ", neighbours, re.M)
            if found:
                if self.run([self.ssh_path, "-F", self.lab_config, "-o", f"HostName={found[0]}", "fs2-client", "true"], check=False,
                            timeout=20)[0] == 0:
                    raise LabError(f"fs2-client moved to {found[0]}: update HostName in {self.lab_config} (the jump hosts use it)")
            time.sleep(5)
        raise LabError(f"fs2-client is not reachable over SSH; check its address on the serial console and {self.lab_config}")

    def boot_time(self) -> str | None:
        code, output = self.guest("fs2-a", "sysctl -n kern.boottime", check=False, timeout=20)
        self.last_error = output.strip()[-300:] if code else ""
        match = re.search(r"sec = (\d+)", output)
        return match[1] if code == 0 and match else None

    # ---- build and deploy ---------------------------------------------------------------------------------------
    def tarball(self, directory: Path, names: list[str]) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as bundle:
            for name in names:
                bundle.add(directory / name, arcname=name,
                           filter=lambda info: None if "__pycache__" in info.name else info)
        return stream.getvalue()

    def installer_packages(self) -> list[str]:
        source = (ROOT / "freesense/src/usr/local/FreeSense/lib/Installer/PackageInstaller.php").read_text(encoding="utf-8")
        return re.findall(r"'([A-Za-z0-9_.+-]+)'", re.search(r"DEFAULT_PACKAGES = \[(.*?)\];", source, re.S)[1])

    def build(self) -> None:
        self.step("Building the packages from the real ports on the builder")
        self.builder("mkdir -p /build/v2-pkg /build/px && chmod 700 /build/px")
        names = ["freesense-system-ports", "freesense/src", "freesense-native", "tools/lab/make-repo.sh"]
        self.to_builder(self.tarball(ROOT, names), "/build/v2-pkg/src.tar")
        code, output = self.builder("sh -s " + " ".join(self.installer_packages()), data=(LAB / "build-packages.sh").read_bytes(),
                                    timeout=900, check=False)
        (self.log_dir / "build.log").write_text(output, encoding="utf-8")
        if code or "PACKAGES-READY" not in output:
            raise LabError("package build failed (missing third-party packages: python tools/lab/fetch_stock.py):\n" + output[-3000:])
        self.record("packages built from the real ports; the installer's list resolves", True)

    def update(self) -> None:
        self.step("Updating VM 150 with the new packages")
        self.start(FIREWALL)
        self.wait("fs2-a SSH", lambda: self.boot_time() is not None, 300)
        installs = []
        # Dependencies first: pkg add resolves nothing from a repository.
        for name in ["FreeSense-platform-abi", "ssh_tunnel_shell", "FreeSense-native", "FreeSense-system"]:
            _, remote = self.builder(f"ls /build/v2-pkg/out/{name}-[0-9]*.pkg")
            local = f"/root/{name}.pkg"
            ok, detail = self.relay([self.ssh_path, "-F", str(self.builder_config), self.builder_host, f"cat {remote.strip()}"],
                                    self.guest_command("fs2-a", f"cat > {local}"), 600)
            if not ok:
                raise LabError(f"could not copy {name} to fs2-a: {detail}")
            installs.append(f"env ASSUME_ALWAYS_YES=yes /usr/local/sbin/pkg-static add -f {local}")
        code, output = self.guest("fs2-a", " && ".join(installs) + "; rc=$?; rm -f /root/*.pkg; exit $rc", timeout=600, check=False)
        (self.log_dir / "update.log").write_text(output, encoding="utf-8")
        if code:
            raise LabError("pkg add failed (a new dependency needs --fresh):\n" + output[-2000:])
        self.record("update installed on fs2-a", True)

    def install_fresh(self) -> None:
        self.step("Building a fresh install image (VM 150 disks are replaced)")
        source = (ROOT / "freesense/tests/InstallerPipelineTest.php").read_text(encoding="utf-8")
        secret = re.search(r"'admin_password' => '([^']+)'", source)[1]
        config = {"storage": {"disks": ["@D1@"], "firmware": "uefi", "filesystem": "zfs", "layout": "stripe", "swap": 1024, "pool": "FreeSense"},
                  "settings": {"version": 1, "hostname": "fs2-a", "wan": {"device": "vtnet0"},
                               "lan": {"device": "vtnet1", "address": LAN, "prefix": 24, "dhcp": True},
                               "webui": {"mode": "enabled", "port": 443}},
                  "repository": "file:///build/v2-pkg/repo", "console": "both", "admin_password": secret}
        self.to_builder(json.dumps(config).encode(), "/build/px/cfg-fs2-a.json")
        self.to_builder((self.keys / "id_ed25519.pub").read_bytes(), "/build/px/lab.pub")
        code, output = self.builder("sh -s fs2-a", data=(LAB / "build-install-image.sh").read_bytes(), timeout=1800, check=False)
        (self.log_dir / "image.log").write_text(output, encoding="utf-8")
        if code or "IMAGE-READY" not in output:
            raise LabError("install image failed:\n" + output[-3000:])
        self.record("fresh install image", True)
        self.stop(FIREWALL)
        _, vm = self.host(f"qm config {FIREWALL}")
        for remote, slot in [("/build/px/fs2-a.raw", "scsi0"), ("/build/px/repo.raw", "scsi1")]:
            volume = re.search(rf"^{slot}: (local-lvm:vm-{FIREWALL}-disk-\d+),", vm, re.M)
            if not volume:
                raise LabError(f"VM {FIREWALL} has no own disk at {slot}")
            self.step(f"Writing {slot} of VM {FIREWALL}")
            ok, detail = self.relay([self.ssh_path, "-F", str(self.builder_config), self.builder_host, f"gzip -1 -c {remote}"],
                                    [*self.pve, f"set -e; dev=$(pvesm path {volume[1]}); lvchange -ay $dev; blkdiscard -f $dev; "
                                     "gunzip | dd of=$dev bs=1M iflag=fullblock conv=sparse status=none"], 1800)
            if not ok:
                raise LabError(f"could not write {slot}: {detail}")
        self.builder("rm -f /build/px/fs2-a.raw")
        # A new disk means a new host key.
        self.forget_host_key(LAN)
        # First boot with the WAN link down until the NIC mapping is confirmed.
        mac = re.search(r"^net0: virtio=([0-9A-F:]+),", vm, re.M)[1]
        self.host(f"qm set {FIREWALL} --net0 virtio={mac},bridge=vmbr0,link_down=1")
        try:
            self.start(FIREWALL)
            self.wait("fs2-a SSH after the fresh install", lambda: self.boot_time() is not None, 420)
            _, nics = self.guest("fs2-a", "ifconfig vtnet0; echo ==; ifconfig vtnet1")
            wan, lan = nics.split("==")
            lan_mac = re.search(r"^net1: virtio=([0-9A-F:]+),", vm, re.M)[1].lower()
            if mac.lower() not in wan or lan_mac not in lan or f"inet {LAN} " not in lan:
                raise LabError("the installed NIC mapping is not WAN=net0/LAN=net1; WAN link left down")
            self.record("first boot with the WAN link down; LAN on vmbr2", True)
        except LabError:
            self.stop(FIREWALL)
            raise
        self.host(f"qm set {FIREWALL} --net0 virtio={mac},bridge=vmbr0")

    def forget_host_key(self, address: str) -> None:
        known = self.keys / "known_hosts"
        if known.is_file():
            self.run([str(Path(self.ssh_path).with_name("ssh-keygen" + Path(self.ssh_path).suffix)), "-R", address, "-f", key_file(known)],
                     check=False)

    def verify_tree(self) -> None:
        """Every System file on the appliance equals the working tree."""
        source = ROOT / "freesense/src"
        lines = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()} /{path.relative_to(source).as_posix()}\n"
                        for path in sorted(source.rglob("*")) if path.is_file() and "__pycache__" not in path.parts)
        script = ('rc=0; while read -r want path; do [ "$(sha256 -q "$path" 2>/dev/null)" = "$want" ] || '
                  '{ echo "differs: $path"; rc=1; }; done; exit $rc')
        code, output = self.guest("fs2-a", script, data=lines.encode(), check=False, timeout=300)
        self.record(f"installed files equal the working tree ({lines.count(chr(10))} files)", code == 0, output[-1500:])

    def log_errors(self) -> set[str]:
        _, output = self.guest("fs2-a", "cat /var/log/messages /var/log/system.log 2>/dev/null | "
                               "grep -E 'PHP (Fatal|Parse|Warning)|Uncaught |panic:' | tail -200; true", check=False)
        return set(output.splitlines())

    def reboot(self) -> None:
        self.step("Rebooting fs2-a")
        before = self.boot_time()
        if before is None:
            raise LabError("fs2-a is not reachable before the reboot")
        self.guest("fs2-a", "shutdown -r now", check=False, timeout=30)
        self.wait("fs2-a back after the reboot", lambda: self.boot_time() not in (None, before), 420)
        self.record("fs2-a rebooted", True)

    # ---- checks -------------------------------------------------------------------------------------------------
    def firewall_checks(self) -> None:
        self.step("Checking the appliance after the reboot")
        try:
            self.wait("pf enabled and WebUI listening", lambda: self.guest(
                "fs2-a", "pfctl -si | grep -q '^Status: Enabled' && sockstat -4 -l -p 443 | grep -q ':443'", check=False, timeout=30)[0] == 0, 180)
        except LabError:
            pass
        self.check("pf is enabled", "fs2-a", "pfctl -si", r"Status: Enabled")
        self.check("pf rules are loaded", "fs2-a", "pfctl -sr | wc -l", r"^\s*([2-9]\d|\d{3,})\s*$")
        self.check("forwarding is on", "fs2-a", "sysctl -n net.inet.ip.forwarding", r"^1")
        self.check("WebUI listens on 443", "fs2-a", "sockstat -4 -l -p 443", r":443")
        self.check("WAN has an address and a default route", "fs2-a", "ifconfig vtnet0 | grep 'inet ' && route -n get default | grep gateway")
        self.check("the configuration reads", "fs2-a", "/usr/local/sbin/freesense-config backup >/dev/null")

    def appliance_tests(self) -> None:
        self.step("Running tests/appliance on fs2-a")
        tests = sorted(p.relative_to(ROOT / "freesense").as_posix() for p in (ROOT / "freesense/tests/appliance").glob("*Test.php"))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            for name in ["src", "tests"]:
                bundle.add(ROOT / "freesense" / name, arcname=name, filter=lambda info: None if "__pycache__" in info.name else info)
            bundle.add(ROOT / "tools/php-check.php", arcname="php-check.php")
        code, output = self.guest(
            "fs2-a", "set -e; export TMPDIR=/tmp FREESENSE_PHP_JOBS=1; d=$(mktemp -d /tmp/labaccept.XXXXXX); trap 'rm -rf \"$d\"' EXIT; "
            f"cd \"$d\"; tar -xzf -; /usr/local/bin/php php-check.php selected {' '.join(tests)}",
            data=stream.getvalue(), check=False, timeout=900)
        (self.log_dir / "appliance.log").write_text(output, encoding="utf-8")
        self.record(f"tests/appliance ({len(tests)} tests)", code == 0, output[-3000:])

    def traffic_checks(self) -> None:
        self.step("Checking traffic from fs2-client")
        self.guest("fs2-client", f"ifconfig vtnet1 | grep -q 'inet {CLIENT_LAN} ' || ifconfig vtnet1 inet {CLIENT_LAN}/24 up", check=False)
        self.check("client pings the LAN address", "fs2-client", f"ping -S {CLIENT_LAN} -c 3 -t 10 {LAN}")
        self.check("DNS Resolver answers the client", "fs2-client", f"drill -I {CLIENT_LAN} @{LAN} freebsd.org A", r"rcode: NOERROR")
        # The client's own default route uses the management network; one host route sends this destination through the firewall.
        route = f"route -q delete -host {OUTSIDE} >/dev/null 2>&1; route -q add -host {OUTSIDE} {LAN} && "
        cleanup = f"; rc=$?; route -q delete -host {OUTSIDE} >/dev/null 2>&1; exit $rc"
        self.check("routed and translated traffic through the firewall (ICMP)", "fs2-client",
                   f"{route}ping -S {CLIENT_LAN} -c 3 -t 10 {OUTSIDE}{cleanup}")
        self.check("routed TCP through the firewall (443)", "fs2-client",
                   f"{route}nc -z -w 10 -s {CLIENT_LAN} {OUTSIDE} 443{cleanup}")
        code, states = self.guest("fs2-a", "pfctl -ss", check=False)
        self.record("the firewall holds a translated state for the client",
                    re.search(rf"\({re.escape(CLIENT_LAN)}:\d+\) -> {re.escape(OUTSIDE)}", states) is not None, states[-800:])
        # A client that only asks: its script brings the test NIC up and configures nothing, so the client's routes stay as they are.
        script = '#!/bin/sh\n[ "$reason" = PREINIT ] && ifconfig $interface up\nexit 0\n'
        code, output = self.guest(
            "fs2-client", "cat > /tmp/fs-lease.sh; chmod 700 /tmp/fs-lease.sh; echo 'script \"/tmp/fs-lease.sh\";' > /tmp/fs-lease.conf; "
            "pkill -f 'dhclient.*vtnet2' >/dev/null 2>&1; rm -f /tmp/fs-lease.db; "
            "timeout 20 dhclient -d -c /tmp/fs-lease.conf -l /tmp/fs-lease.db -p /tmp/fs-lease.pid vtnet2 2>&1; "
            "ifconfig vtnet2 down; rm -f /tmp/fs-lease.sh /tmp/fs-lease.conf /tmp/fs-lease.db /tmp/fs-lease.pid; true",
            data=script.encode(), check=False, timeout=60)
        self.record("the LAN DHCP server leases an address",
                    re.search(r"DHCPACK from 172\.29\.50\.1[^0-9][\s\S]*bound to 172\.29\.50\.\d+", output) is not None, output[-800:])
        _, wan = self.guest("fs2-a", "ifconfig vtnet0 | awk '$1 == \"inet\" {print $2; exit}'", check=False)
        wan = wan.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", wan):
            code, output = self.guest("fs2-client", f"nc -z -w 5 {wan} 443", check=False, timeout=30)
            self.record("the WAN side refuses the WebUI (default deny)", code != 0, "TCP 443 on the WAN address answered")
        else:
            self.record("the WAN side refuses the WebUI (default deny)", False, "fs2-a has no WAN address")
        self.webui_checks()
        self.login_protection_check()

    def login_protection_check(self) -> None:
        """Failed WebUI sign-ins must put the address in the sshguard table. A spare address of the client takes the block,
        so the address that carries the SSH jump stays free."""
        spare = "172.29.50.11"
        body = json.dumps({"username": "admin", "password": "not-the-password"})
        request = "\r\n".join(["POST /api/v1/auth/login HTTP/1.1", f"Host: {LAN}", f"Origin: https://{LAN}",
                               "Content-Type: application/json", "X-FreeSense-Command: login",
                               f"Content-Length: {len(body)}", "Connection: close", "", body])
        attempt = f"openssl s_client -quiet -ign_eof -bind {spare} -connect {LAN}:443 2>/dev/null | head -1"

        def blocked() -> bool:
            return spare in self.guest("fs2-a", "pfctl -t sshguard -T show", check=False)[1]

        self.guest("fs2-client", f"ifconfig vtnet1 inet {spare}/32 alias")
        try:
            answers = [self.guest("fs2-client", attempt, data=request.encode(), check=False, timeout=60)[1] for _ in range(4)]
            self.record("a wrong password is refused", " 401 " in answers[0], answers[0][-200:])
            try:
                self.wait("the sshguard block", blocked, 60)
            except LabError:
                pass
            self.record("repeated failed sign-ins are blocked at the firewall", blocked(), "".join(answers)[-400:])
            code, _ = self.guest("fs2-client", f"nc -z -w 5 -s {spare} {LAN} 443", check=False, timeout=30)
            self.record("the blocked address cannot reach the WebUI", code != 0, "TCP 443 still answered")
        finally:
            self.guest("fs2-a", f"pfctl -t sshguard -T delete {spare}", check=False)
            self.guest("fs2-client", f"ifconfig vtnet1 inet {spare} -alias", check=False)

    def webui_checks(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            local = probe.getsockname()[1]
        tunnel = subprocess.Popen([self.ssh_path, "-F", self.lab_config, "-N", "-o", "ExitOnForwardFailure=yes",
                                   "-L", f"{local}:{LAN}:443", "fs2-client"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # lab VM with a self-signed certificate

        def call(path: str, body=None, headers=None) -> tuple[int, bytes]:
            request = urllib.request.Request(f"https://127.0.0.1:{local}{path}", data=None if body is None else json.dumps(body).encode())
            for name, value in {"Host": LAN, "Origin": f"https://{LAN}", **(headers or {})}.items():
                request.add_header(name, value)
            try:
                with urllib.request.urlopen(request, context=context, timeout=60) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
            except OSError as error:
                return 0, str(error).encode()

        try:
            try:
                self.wait("WebUI tunnel", lambda: call("/")[0] != 0, 60)
            except LabError:
                pass
            status, page = call("/")
            self.record("the WebUI serves its page", status == 200 and b"/assets/shell/app.js" in page, f"HTTP {status}")
            source = (ROOT / "freesense/tests/InstallerPipelineTest.php").read_text(encoding="utf-8")
            secret = re.search(r"'admin_password' => '([^']+)'", source)[1]
            status, body = call("/api/v1/auth/login", {"username": "admin", "password": secret},
                                {"Content-Type": "application/json", "X-FreeSense-Command": "login"})
            if not self.record("sign-in with the lab password", status == 200, f"HTTP {status}"):
                return
            data = json.loads(body)["data"]
            auth = {"Authorization": "Bearer " + (data.get("token") or data.get("key"))}
            for path in ["/api/v1/session", "/api/v1/status/nics", "/api/v1/firewall/rules"]:
                status, body = call(path, headers=auth)
                self.record(f"authenticated read {path}", status == 200, f"HTTP {status} {body[:200]!r}")
        finally:
            tunnel.terminate()
            tunnel.wait(timeout=30)

    # ---- run ----------------------------------------------------------------------------------------------------
    def execute(self) -> int:
        self.verify_machines()
        failure = None
        try:
            if not self.arguments.checks_only:
                self.build()
            self.start(CLIENT)
            self.connect_client()
            if self.arguments.fresh:
                self.install_fresh()
            elif not self.arguments.checks_only:
                self.update()
            else:
                self.start(FIREWALL)
            self.wait("fs2-a SSH", lambda: self.boot_time() is not None, 300)
            if not self.arguments.checks_only:
                self.verify_tree()
            known = self.log_errors()
            self.reboot()
            self.firewall_checks()
            self.appliance_tests()
            self.traffic_checks()
            new = self.log_errors() - known
            self.record("no new PHP errors or panics in the system log", not new, "\n".join(sorted(new)[:20]))
        except (LabError, OSError, subprocess.TimeoutExpired) as error:
            failure = str(error)
            self.record("run completed", False, failure)
        finally:
            if self.arguments.keep:
                self.step("Leaving VMs 150 and 152 running (--keep)")
            else:
                self.step("Stopping VMs 150 and 152")
                for vmid in (FIREWALL, CLIENT):
                    try:
                        self.stop(vmid)
                        self.record(f"VM {vmid} stopped", not self.running(vmid))
                    except LabError as error:
                        self.record(f"VM {vmid} stopped", False, str(error))
        return self.report()

    def report(self) -> int:
        failed = [name for name, passed, _ in self.results if not passed]
        mode = "checks only" if self.arguments.checks_only else "fresh install" if self.arguments.fresh else "update"
        lines = [f"Lab acceptance ({mode}): {len(self.results) - len(failed)} of {len(self.results)} checks passed "
                 f"in {time.monotonic() - self.started:.0f} s"]
        lines += [f"{'ok  ' if passed else 'FAIL'} {name}" for name, passed, _ in self.results]
        lines += [f"\n--- {name}\n{detail}" for name, passed, detail in self.results if not passed]
        (self.log_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n" + lines[0])
        if failed:
            print("Failed: " + ", ".join(failed))
        print(f"Report and logs: {self.log_dir}")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fresh", action="store_true", help="replace VM 150's disks with a fresh ZFS install instead of updating it")
    mode.add_argument("--checks-only", action="store_true", help="no build or deploy: reboot VM 150 and run the checks")
    parser.add_argument("--keep", action="store_true", help="leave VMs 150 and 152 running afterwards")
    try:
        return Lab(parser.parse_args()).execute()
    except LabError as error:
        print(f"FAIL: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
