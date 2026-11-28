"""Run the refactor workspace's local checks. Requires Python 3.12 or later."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

# The local workspace admits source files only.
sys.dont_write_bytecode = True
import affected
import parallel
import port_ownership
import public_php
import snapshot

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", "node_modules", "vendor", ".astro", ".cache", "dist"}
SYSTEM = "freesense"
PORTS = "freesense-system-ports"
PLIST = Path("security/FreeSense-system/pkg-plist")
PHP_IMAGE = "php:8.6.0-cli"
SHELLCHECK_IMAGE = "koalaman/shellcheck:stable"
REF_PATH = re.compile(
    r"(?:^|[/\\\"'`])(?:repositories[/\\])?(?:active|stable-1\.1|refactor12-archive-[\w.-]+|local-output)[/\\]",
    re.IGNORECASE,
)


class CheckError(Exception):
    """A local prerequisite or acceptance check failed."""


def run(args: list[str], **options) -> None:
    parallel.run(args, **options)


def source_files(directory: Path, *, shipped: bool = False):
    def report_error(error: OSError) -> None:
        raise error

    if directory.is_junction() or directory.is_symlink():
        raise CheckError(f"review link before admission: {directory}")
    for parent, dirs, names in directory.walk(on_error=report_error):
        dirs[:] = sorted(dirs if shipped else (d for d in dirs if d not in SKIP))
        for name in [*dirs, *names]:
            path = parent / name
            if path.is_symlink() or path.is_junction():
                raise CheckError(f"review link before admission: {path}")
        for name in sorted(names):
            yield parent / name


def kind(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(1024)
    line = head.split(b"\n", 1)[0]
    if path.suffix in {".php", ".inc", ".phtml"} or re.search(rb"^#!.*\bphp\b|^<\?php\b", line):
        return "php"
    if path.suffix in {".sh", ".bash"} or re.search(rb"^#!.*\b(?:ba)?sh\b", line):
        return "bash" if path.suffix == ".bash" or b"bash" in line else "sh"
    return path.suffix


def plist_check() -> dict[str, int]:
    files = ROOT / SYSTEM / "src"
    manifest = ROOT / PORTS / PLIST
    if not files.is_dir() or not manifest.is_file():
        raise CheckError("System packaging requires freesense/src and the sibling System pkg-plist")
    public_php.check_stage(files, optional=False)
    expected = {"/" + path.relative_to(files).as_posix(): path for path in source_files(files, shipped=True)}
    listed = {}
    for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        entry = raw.strip()
        if not entry or entry.startswith("@comment "):
            continue
        attrs = re.match(r"^@\((?:root,wheel,|,,)(0?(?:444|644|555|755|600))\) (.+)$", entry)
        mode = 0o644
        if attrs:
            mode = int(attrs[1], 8)
            entry = attrs[2]
        if entry.startswith("@"):
            raise CheckError(f"{manifest}:{number}: add validation for plist directive {entry.split()[0]}")
        entry = entry.replace("%%DATADIR%%/", "/usr/local/share/FreeSense/")
        if not entry.startswith("/"):
            entry = "/usr/local/" + entry
        if entry in listed:
            raise CheckError(f"duplicate pkg-plist entry: {entry}")
        if entry in expected:
            with expected[entry].open("rb") as stream:
                if stream.read(2) == b"#!" and not mode & 0o100:
                    raise CheckError(f"executable entry point needs an explicit executable mode: {entry}")
        listed[entry] = mode
    if listed.keys() != expected.keys():
        raise CheckError(f"pkg-plist missing {sorted(expected.keys() - listed.keys())}; stale {sorted(listed.keys() - expected.keys())}")
    parallel.say(f"System pkg-plist matches {len(listed)} staged paths")
    return listed


def workspace_folders() -> list[Path]:
    return sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name not in {".git", "__pycache__", "local-output"})


def layout_check(folders: list[Path]) -> None:
    """LF line endings everywhere, and library folders that match namespaces (one type per file, named like it)."""
    count = 0
    # The root documents too; NUL is a Windows device name, so only the named kinds are opened there.
    top = [p for p in ROOT.iterdir() if p.is_file() and (p.suffix == ".md" or p.name.startswith(".git"))]
    for source in [top, *(source_files(folder) for folder in folders)]:
        for path in source:
            relative = path.relative_to(ROOT).as_posix()
            if path.suffix == ".patch" or "/third-party/" in relative:
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue
            count += 1
            if b"\r\n" in data:
                raise CheckError(f"CRLF line endings: {relative}")
            if path.suffix != ".php" or "/FreeSense/lib/" not in relative:
                continue
            namespace = "\\".join(["FreeSense", *relative.rsplit("/FreeSense/lib/", 1)[1].split("/")[:-1]])
            text = data.decode("utf-8")
            declared = re.search(r"^namespace ([A-Za-z0-9_\\]+)\s*;", text, re.M)
            if not declared or declared[1] != namespace:
                raise CheckError(f"namespace does not match the folder: {relative} should declare {namespace}")
            if not re.search(rf"^(?:(?:final|abstract|readonly) )*(?:class|interface|trait|enum) {re.escape(path.stem)}\b", text, re.M):
                raise CheckError(f"file name does not match its type: {relative}")
    parallel.say(f"Layout: {count} files use LF and library folders match namespaces")


def select_tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise CheckError(f"required executable missing: {name}")
    return executable


def snapshot_names() -> list[str]:
    return [p.name for p in ROOT.iterdir() if p.name not in {".git", "__pycache__", "local-output"}]


@contextmanager
def workspace_volume():
    """The shared snapshot of this run, or a new one when none is held."""
    held = os.environ.get(snapshot.ENVIRONMENT)
    if held:
        yield held
        return
    with snapshot.volume(ROOT, snapshot_names(), select_tool("docker")) as name:
        yield name


@contextmanager
def held_snapshot():
    """Take one workspace snapshot and share it with every job, including the browser suites."""
    if not shutil.which("docker"):
        yield
        return
    with workspace_volume() as name:
        previous = os.environ.get(snapshot.ENVIRONMENT)
        os.environ[snapshot.ENVIRONMENT] = name
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(snapshot.ENVIRONMENT, None)


def shell_check(paths: list[Path], language: str) -> None:
    """Syntax and ShellCheck for many scripts of one language. Every script is checked before the failure is raised."""
    interpreter = shutil.which(language)
    linter = shutil.which("shellcheck")
    failed = []

    def attempt(args: list[str], what: str) -> None:
        try:
            run(args)
        except subprocess.CalledProcessError:
            failed.append(what)

    if interpreter:
        for path in paths:
            attempt([interpreter, "-n", str(path)], f"syntax of {path.name}")
    if linter:
        attempt([linter, "-s", language, *map(str, paths)], "ShellCheck")
    if not interpreter or not linter:
        # One container per tool, not one per script.
        docker = select_tool("docker")
        names = [path.relative_to(ROOT).as_posix() for path in paths]
        with workspace_volume() as volume:
            base = [docker, "run", "--rm", "--pull=never", "--network", "none", "-v", f"{volume}:/check:ro", "-w", "/check"]
            if not interpreter:
                attempt([*base, "--entrypoint", "sh", PHP_IMAGE, "-c",
                         f'rc=0; for f; do {language} -n "$f" || rc=1; done; exit $rc', "syntax", *names], "syntax")
            if not linter:
                attempt([*base, "--entrypoint", "shellcheck", SHELLCHECK_IMAGE, "-s", language, *names], "ShellCheck")
    if failed:
        raise CheckError(f"{language} checks failed: {', '.join(failed)}")


def portable_php(directory: Path, command: list[str]) -> None:
    executable = shutil.which(command[0])
    if executable and shutil.which("php"):
        environment = os.environ.copy()
        environment.pop("FREESENSE_TEST_PHP", None)
        run([executable, *command[1:]], cwd=directory, env=environment)
        return
    docker = select_tool("docker")
    settings = ["-e", "FREESENSE_PHP_JOBS"] if os.environ.get("FREESENSE_PHP_JOBS") else []
    with (workspace_volume() if directory == ROOT else snapshot.volume(directory, docker=docker)) as volume:
        run([docker, "run", "--rm", "--pull=never", "--network", "none", *settings,
             "-v", f"{volume}:/check:ro", "-w", "/check", PHP_IMAGE, *command])


def portable_test_paths(names: list[str], components: set[str]) -> list[str]:
    selected = []
    for name in names:
        parts = name.replace("\\", "/").split("/")
        if (len(parts) < 3 or parts[0] not in components or parts[1] != "tests"
                or not parts[-1].endswith("Test.php") or parts[2].casefold() in {"lab", "appliance"}
                or any(not p or p.endswith((".", " ")) or p.casefold() in SKIP or ":" in p for p in parts)):
            raise CheckError(f"expected a portable COMPONENT/tests/...Test.php path: {name}")
        target = ROOT
        for part in parts:
            target = target / part
            if target.is_symlink() or target.is_junction():
                raise CheckError(f"linked test paths are not supported: {name}")
        if not target.is_file():
            raise CheckError(f"portable PHP test does not exist: {name}")
        if not target.resolve(strict=True).is_relative_to(ROOT / parts[0] / "tests"):
            raise CheckError(f"test path escapes the component's tests directory: {name}")
        path = target.relative_to(ROOT).as_posix()
        if path not in selected:
            selected.append(path)
    return selected


def php_checks(component: Path, paths: list[Path], appliance: bool, modes: dict[str, int]) -> None:
    # A sibling tests/ and src/ reproduce install-relative entry points in isolation.
    with tempfile.TemporaryDirectory(prefix="freesense-check-") as tmp:
        stage = Path(tmp)
        for path in source_files(component):
            dest = stage / "component" / path.relative_to(component)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        if component.name == SYSTEM:
            port = ROOT / PORTS / PLIST.parent
            shutil.copy2(port / "files/stage-system.sh", stage)
            shutil.copy2(port / PLIST.name, stage)
            (stage / "modes.json").write_text(json.dumps(modes), encoding="utf-8")
        elif component.name == port_ownership.OPTIONAL:
            # Package tests consume the real installed System API from a sibling checkout.
            library = Path("src/usr/local/FreeSense")
            shutil.copytree(ROOT / SYSTEM / library, stage / SYSTEM / library)
            # ... and the shared fakes of the typed system facts that those tests inject.
            fakes = stage / SYSTEM / "tests"
            fakes.mkdir(parents=True, exist_ok=True)
            for fake in [*(ROOT / SYSTEM / "tests").glob("Fake*Facts.php"), ROOT / SYSTEM / "tests/IfconfigFixture.php"]:
                shutil.copy2(fake, fakes / fake.name)
            if not (component / "tests/appliance").is_dir():
                (stage / "native-portable-only").touch()
        shutil.copyfile(ROOT / "tools/php-check.php", stage / "php-check.php")
        shutil.copyfile(ROOT / "tools/check-stage.sh", stage / "check-stage.sh")
        names = [p.relative_to(component).as_posix() for p in paths]
        (stage / "paths.json").write_text(json.dumps(names), encoding="utf-8")
        if appliance and platform.system() != "FreeBSD":
            run_guest(stage)
            return
        if not appliance:
            portable_php(stage, ["sh", "check-stage.sh", "portable"])
            return
        shell = shutil.which("sh")
        if shutil.which("php") and shell:
            run([shell, "check-stage.sh", "appliance"], cwd=stage, env=os.environ.copy())
        else:
            raise CheckError("PHP 8.6 CLI is required on the FreeBSD guest")


def run_guest(stage: Path) -> None:
    guest = os.environ.get("FREESENSE_TEST_GUEST", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", guest):
        raise CheckError("--appliance needs native FreeBSD or FREESENSE_TEST_GUEST set to the local lab guest's SSH alias")
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        for path in stage.iterdir():
            archive.add(path, arcname=path.name)
    # The guest owns this temporary directory. No installed appliance files are replaced.
    # TMPDIR without a trailing slash: PHP's sys_get_temp_dir() keeps it, and '/tmp//x' fails the canonical-path checks.
    command = """set -eu
export TMPDIR=/tmp
test "$(uname -s)" = FreeBSD || { echo 'FreeBSD guest required' >&2; exit 1; }
command -v php >/dev/null || { echo 'PHP 8.6 CLI required on guest' >&2; exit 1; }
d=$(mktemp -d /tmp/freesense-check.XXXXXXXX)
trap 'rm -rf "$d"' EXIT
cd "$d"
tar -xzf -
sh check-stage.sh appliance"""
    guest_php = os.environ.get("FREESENSE_TEST_PHP")
    if guest_php:
        if not guest_php.startswith("/"):
            raise CheckError("FREESENSE_TEST_PHP must be the absolute guest CLI path")
        command = "export FREESENSE_TEST_PHP=" + shlex.quote(guest_php) + "\n" + command
    ssh_options = []
    config = os.environ.get("FREESENSE_TEST_SSH_CONFIG")
    if config:
        if not Path(config).is_file():
            raise CheckError("FREESENSE_TEST_SSH_CONFIG must name a readable SSH config file")
        ssh_options.extend(["-F", config])
    run([select_tool("ssh"), *ssh_options, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=yes", guest, "/bin/sh -c " + shlex.quote(command)], input=bundle.getvalue())


def browser_job(path: Path) -> parallel.Job:
    return parallel.Job(path.relative_to(ROOT).as_posix(), lambda: run([sys.executable, "-B", str(path)]))


def require_playwright() -> None:
    if importlib.util.find_spec("playwright") is None:
        raise CheckError("System browser checks require Python Playwright and a local Chromium browser.")


def failing_job(name: str, error: Exception) -> parallel.Job:
    def fail() -> None:
        raise error

    return parallel.Job(name, fail)


def check_component(component: Path, appliance: bool, modes: dict[str, int] | None) -> list[parallel.Job]:
    """Scan the component's sources now and return its portable jobs. Native tests run apart (appliance_job)."""
    php_files, scripts, referenced = [], {}, []
    for path in source_files(component):
        language = kind(path)
        if path.name == "Makefile" or language in {
            "php", "sh", "bash", ".py", ".js", ".ts", ".mjs", ".json", ".yml", ".yaml", ".xml", ".mk", ".conf",
        }:
            if REF_PATH.search(path.read_text(encoding="utf-8")):
                referenced.append(path.relative_to(ROOT).as_posix())
        if language == "php":
            php_files.append(path)
        elif language in {"sh", "bash"}:
            scripts.setdefault(language, []).append(path)
        elif language == ".py":
            compile(path.read_bytes(), str(path), "exec")
    if referenced:
        raise CheckError(f"external reference tree used in {', '.join(referenced)}")
    name = component.name
    jobs = [parallel.Job(f"{name}: {language} scripts", lambda paths=paths, language=language: shell_check(paths, language))
            for language, paths in sorted(scripts.items())]
    if php_files:
        if modes is None:
            jobs.append(failing_job(f"{name}: portable PHP", CheckError("not run: the System packaging gate failed")))
        else:
            jobs.append(parallel.Job(f"{name}: portable PHP", lambda: php_checks(component, php_files, False, modes)))
    browser_suites = sorted((component / "tests/web").glob("test_*.py"))
    if name == SYSTEM and browser_suites:
        require_playwright()
        jobs.extend(browser_job(suite) for suite in browser_suites)
        tftp = ROOT / port_ownership.OPTIONAL / "ftp/FreeSense-pkg-tftpd/files/usr/local/share/FreeSense/web-packages/tftp.json"
        if tftp.is_file():
            jobs.append(browser_job(ROOT / "tools/browser_management.py"))
    if any(path not in browser_suites for path in (component / "tests").glob("**/test_*.py")):
        jobs.append(parallel.Job(f"{name}: Python tests", lambda: run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=component)))
    package = component / "package.json"
    if package.is_file():
        declared = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        checks = [s for s in ("validate", "check", "test", "check-links", "check-versions") if s in declared]
        if not checks:
            raise CheckError(f"{name}: declare a native validation script in package.json")

        def validate() -> None:
            for script in checks:
                run([select_tool("npm"), "run", script], cwd=component)

        jobs.append(parallel.Job(f"{name}: npm {' '.join(checks)}", validate))
    return jobs


def appliance_job(component: Path, modes: dict[str, int]) -> parallel.Job | None:
    php_files = [path for path in source_files(component) if kind(path) == "php"]
    if component.name == SYSTEM or (component / "tests/appliance").exists() or (component.name == port_ownership.OPTIONAL and php_files):
        return parallel.Job(f"{component.name}: appliance PHP", lambda: php_checks(component, php_files, True, modes))
    return None


def history_path() -> Path:
    return affected.stamp_path(ROOT).with_name(f"{ROOT.name}-job-seconds.json")


def packaging_gates(packaging: bool, state: dict) -> list[parallel.Job]:
    """The quick workspace rules, run first. `state["modes"]` receives the System file modes for the PHP staging."""
    def package_rules() -> None:
        modes = plist_check()
        port_ownership.audit(ROOT, modes)
        state["modes"] = modes

    gates = [parallel.Job("layout", lambda: layout_check(workspace_folders()))]
    # The estate ledger is part of the local planning notes, which a published checkout does not have.
    if (ROOT / "planning/inventory").is_dir():
        gates.append(parallel.Job("inventory", lambda: run([sys.executable, "-B", str(ROOT / "tools/inventory.py")])))
    if packaging:
        gates.append(parallel.Job("packaging", package_rules))
    parallel.execute(gates, 1)
    return gates


def import_job() -> parallel.Job:
    return parallel.Job("missing imports", lambda: portable_php(ROOT, ["php", "tools/php-imports.php", "."]))


def run_jobs(jobs: list[parallel.Job], gates: list[parallel.Job], workers: int, started: float) -> int:
    print(f"Running {len(jobs)} jobs with {workers} workers (--jobs or FREESENSE_CHECK_JOBS)", flush=True)
    parallel.execute(jobs, workers, history_path())
    return parallel.summary([*gates, *jobs], time.monotonic() - started, workers)


def affected_jobs(selection) -> list[parallel.Job]:
    jobs = []
    if selection.lint_php:
        jobs.append(import_job())
        jobs.append(parallel.Job("lint changed PHP",
                                 lambda: portable_php(ROOT, ["php", "tools/php-check.php", "lint", *selection.lint_php])))
    scripts = {}
    for name in selection.lint_shell:
        scripts.setdefault(kind(ROOT / name), []).append(ROOT / name)
    jobs.extend(parallel.Job(f"changed {language} scripts", lambda paths=paths, language=language: shell_check(paths, language))
                for language, paths in sorted(scripts.items()))
    jobs.extend(parallel.Job(f"compile {name}", lambda name=name: compile((ROOT / name).read_bytes(), name, "exec"))
                for name in selection.lint_python)
    if selection.php_tests:
        paths = portable_test_paths(sorted(selection.php_tests), {name.split("/", 1)[0] for name in selection.php_tests})
        jobs.append(parallel.Job("selected PHP tests",
                                 lambda: portable_php(ROOT, ["php", "tools/php-check.php", "selected", *paths])))
    for name in sorted(selection.py_tests):
        path = ROOT / name
        top = ROOT / "tools/tests" if name.startswith("tools/") else ROOT / name.split("/", 1)[0] / "tests"
        jobs.append(parallel.Job(name, lambda path=path, top=top: run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", str(top), "-p", path.name, "-q"], cwd=top.parent)))
    if selection.web_tests:
        require_playwright()
        jobs.extend(browser_job(ROOT / name) for name in sorted(selection.web_tests))
    return jobs


def check_affected(since_value: str | None, list_only: bool, requested_jobs: int | None = None) -> None:
    """Feature-work run: packaging gates, lint of the changed files and only the tests that name them (tools/affected.py)."""
    since = affected.parse_since(ROOT, since_value)
    selection = affected.select(ROOT, affected.changed_files(ROOT, since))
    print(affected.report(selection, since), flush=True)
    if list_only:
        return
    if not selection.changed:
        print("Nothing changed since then; no tests to run.")
        return
    started = time.monotonic()
    workers = parallel.default_jobs(requested_jobs)
    gates = packaging_gates(True, {})
    with held_snapshot():
        status = run_jobs(affected_jobs(selection), gates, workers, started)
    if selection.native_tests:
        print("Native tests named above were not run; run them on the lab or builder (see AGENTS.md).")
    if status:
        raise CheckError("affected checks failed")
    print("Affected checks passed. Run the full check at integration milestones.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    components = {p.name: p for p in ROOT.iterdir()
                  if p.is_dir() and p.name not in {"tools", "planning", ".git", "__pycache__", "local-output"}}
    parser.add_argument("--component", choices=sorted(components))
    parser.add_argument("--appliance", action="store_true", help="also run FreeBSD guest acceptance")
    parser.add_argument("--php-test", action="append", metavar="COMPONENT/tests/NameTest.php",
                        help="run a named portable PHP test; repeat to check a batch in one invocation")
    parser.add_argument("--affected", action="store_true",
                        help="run only the tests that touch files changed since the last full check (or --since)")
    parser.add_argument("--since", metavar="ISO|90m|6h|2d", help="with --affected: compare against this time instead")
    parser.add_argument("--list", action="store_true", help="with --affected: show the selection and stop")
    parser.add_argument("--jobs", type=int, metavar="N",
                        help="jobs side by side (default: from processors and free memory, or FREESENSE_CHECK_JOBS)")
    args = parser.parse_args()
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be 1 or more")
    if args.affected:
        if args.component or args.appliance or args.php_test:
            parser.error("--affected chooses its own tests; do not combine it with --component, --appliance or --php-test")
        check_affected(args.since, args.list, args.jobs)
        return
    if args.since or args.list:
        parser.error("--since and --list need --affected")
    stamp = datetime.now()
    started = time.monotonic()
    selected = {args.component} if args.component else set(components)
    if args.php_test:
        if args.appliance:
            parser.error("--php-test runs portable tests and cannot be combined with --appliance")
        paths = portable_test_paths(args.php_test, selected)
        portable_php(ROOT, ["php", "tools/php-check.php", "selected", *paths])
        return
    if not selected:
        raise CheckError("no admitted components present")
    if args.appliance:
        if SYSTEM not in components:
            raise CheckError("--appliance requires the local freesense component")
        selected.add(SYSTEM)
    workers = parallel.default_jobs(args.jobs)
    packaging = bool(selected.intersection({SYSTEM, PORTS, port_ownership.OPTIONAL}))
    state = {"modes": None}
    gates = packaging_gates(packaging, state)
    modes = state["modes"] if packaging else {}
    with held_snapshot():
        jobs = [parallel.Job("tool tests", lambda: run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", str(ROOT / "tools/tests"), "-q"]))]
        if selected.intersection({SYSTEM, port_ownership.OPTIONAL}):
            jobs.append(import_job())
        for name in sorted(selected):
            try:
                jobs.extend(check_component(components[name], args.appliance, modes))
            except (CheckError, SyntaxError, ValueError, OSError) as error:
                jobs.append(failing_job(f"{name}: sources", error))
        status = run_jobs(jobs, gates, workers, started)
    if args.appliance and modes is not None:
        # Native tests share the guest's kernel: one at a time, after the portable jobs.
        native = [job for name in sorted(selected) if (job := appliance_job(components[name], modes))]
        parallel.execute(native, 1)
        status |= parallel.summary(native, time.monotonic() - started, 1)
    if status:
        raise CheckError("selected checks failed")
    if SYSTEM in selected:
        # --affected compares against the start of the last full System pass.
        affected.write_stamp(ROOT, stamp)
    print("Selected checks passed.")


if __name__ == "__main__":
    try:
        main()
    except (CheckError, ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"FAIL: {error}")
