"""Choose the tests that touch the files changed since the last full check (python tools/check.py --affected).

A full `tools/check.py` run stays the gate for integration milestones. During feature work only the changed files, the
tests that name them and, for a shared file, the set its group in check-affected.json declares run, so a new page or daemon does
not retest the whole firewall.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import fnmatch
import json
from pathlib import Path
import re

SKIP = {".git", "__pycache__", "node_modules", "vendor", ".astro", ".cache", "dist"}
NOT_COMPONENTS = {"tools", "planning", ".git", "__pycache__", "local-output"}
MAP_FILE = "tools/check-affected.json"
TOKEN_MIN = 4


@dataclass
class Selection:
    changed: list[str] = field(default_factory=list)
    php_tests: dict[str, str] = field(default_factory=dict)  # path -> reason
    web_tests: dict[str, str] = field(default_factory=dict)
    py_tests: dict[str, str] = field(default_factory=dict)
    native_tests: dict[str, str] = field(default_factory=dict)  # listed, run with --appliance on the lab
    lint_php: list[str] = field(default_factory=list)
    lint_shell: list[str] = field(default_factory=list)
    lint_python: list[str] = field(default_factory=list)
    core_hits: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.php_tests or self.web_tests or self.py_tests or self.lint_php or self.lint_shell or self.lint_python)


def stamp_path(root: Path) -> Path:
    # Outside the workspace components, so the stamp is never packaged or scanned.
    return root.parents[1] / "local-output" / "check-state" / f"{root.name}-last-full.json"


def write_stamp(root: Path, started: datetime) -> None:
    path = stamp_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"started": started.isoformat(timespec="seconds")}) + "\n", encoding="utf-8")


def parse_since(root: Path, value: str | None) -> datetime:
    """`None` means the last full pass; otherwise an ISO time or a span such as 90m, 6h, 2d."""
    if value:
        span = re.fullmatch(r"(\d+)([mhd])", value)
        if span:
            unit = {"m": "minutes", "h": "hours", "d": "days"}[span[2]]
            return datetime.now() - timedelta(**{unit: int(span[1])})
        return datetime.fromisoformat(value)
    path = stamp_path(root)
    if not path.is_file():
        raise ValueError("no full check has been recorded yet; run the full check once or pass --since (for example --since 6h)")
    return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["started"])


def walk(directory: Path):
    for parent, dirs, names in directory.walk():
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(names):
            yield parent / name


def components(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name not in NOT_COMPONENTS and not p.name.startswith("."))


def changed_files(root: Path, since: datetime) -> list[Path]:
    limit = since.timestamp()
    found = []
    for directory in [*components(root), root / "tools"]:
        for path in walk(directory):
            if path.suffix in {".pyc", ".log"}:
                continue
            if path.stat().st_mtime > limit:
                found.append(path)
    return found


def tokens(path: Path) -> set[str]:
    """Names a test would use for this file. One-word names (Catalog, Notice, assignments) are too common on their own,
    so they count only qualified: `Vpn\\Ipsec\\Catalog` for a class, `interfaces/assignments` for a page asset."""
    stem = path.stem
    parts = path.as_posix().split("/")
    found = set()
    if path.suffix == ".php" and re.fullmatch(r"[A-Z][A-Za-z0-9]+", stem):
        words = re.findall(r"[A-Z][a-z0-9]*", stem)
        if "lib" in parts:
            namespace = parts[len(parts) - 1 - parts[::-1].index("lib") + 1:-1]
            found.add("\\".join([*namespace[-1:], stem]))
        if len(words) >= 2:
            kebab = "-".join(w.lower() for w in words)
            found |= {stem, kebab, kebab.replace("-", "_")}
    elif path.suffix in {".js", ".css", ".json", ".html"}:
        if len(parts) >= 2:
            found.add(f"{parts[-2]}/{stem}")
        if "-" in stem:
            found |= {stem, stem.replace("-", "_")}
    elif stem not in {"pkg-plist", "pkg-descr", "pkg-message", "check-stage", "stage-system"}:
        if "-" in stem or "_" in stem:
            found.add(stem)
    return {t for t in found if len(t) >= TOKEN_MIN}


def test_files(root: Path) -> dict[str, list[Path]]:
    groups = {"php": [], "web": [], "py": [], "native": []}
    for component in components(root):
        tests = component / "tests"
        if not tests.is_dir():
            continue
        for path in walk(tests):
            relative = path.relative_to(tests).as_posix()
            if path.name.endswith("Test.php"):
                first = relative.split("/", 1)[0].casefold()
                groups["native" if first in {"appliance", "lab"} else "php"].append(path)
            elif path.suffix == ".py" and path.name.startswith("test_"):
                groups["web" if relative.startswith("web/") else "py"].append(path)
    groups["py"].extend(sorted((root / "tools/tests").glob("test_*.py")))
    return groups


def load_map(root: Path) -> dict:
    path = root / MAP_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"groups": []}


def matches(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def select(root: Path, changed: list[Path]) -> Selection:
    rules = load_map(root)
    groups = test_files(root)
    texts = {path: path.read_text(encoding="utf-8", errors="replace") for group in groups.values() for path in group}
    selection = Selection(changed=[p.relative_to(root).as_posix() for p in changed])
    bucket = {"php": selection.php_tests, "web": selection.web_tests, "py": selection.py_tests, "native": selection.native_tests}
    kind_of = {path: name for name, group in groups.items() for path in group}

    def add(path: Path, reason: str) -> None:
        target = bucket[kind_of[path]]
        target.setdefault(path.relative_to(root).as_posix(), reason)

    shared: dict[str, dict] = {}  # group name -> group, for the groups a changed file belongs to
    for path in changed:
        relative = path.relative_to(root).as_posix()
        head = path.read_bytes()[:128].split(b"\n", 1)[0] if path.suffix == "" else b""
        if path.suffix == ".php" or (head.startswith(b"#!") and b"php" in head):
            selection.lint_php.append(relative)
        elif path.suffix in {".sh", ".bash"}:
            selection.lint_shell.append(relative)
        elif path.suffix == ".py":
            selection.lint_python.append(relative)
        if path in kind_of:
            add(path, "changed test")
            continue
        found = [group for group in rules.get("groups", []) if matches(relative, group["files"])]
        if found:
            # Shared files are named by many tests; their group declares what covers them instead.
            shared.update((group["name"], group) for group in found)
            selection.core_hits.append(relative)
            if not all(group.get("names", False) for group in found):
                continue
        names = tokens(path)
        if not names:
            continue
        pattern = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(re.escape(n) for n in sorted(names)) + r")(?![A-Za-z0-9_])")
        for test, text in texts.items():
            if pattern.search(text):
                add(test, f"names {relative}")
    extra = [(entry, "always") for entry in rules.get("always", [])] if changed else []
    for name, group in shared.items():
        extra += [(entry, f"{name} set") for entry in group.get("tests", [])]
        if "mentions" in group:
            extra += [(test.relative_to(root).as_posix(), f"{name} set") for test, text in texts.items() if group["mentions"] in text]
    for entry, reason in extra:
        target = root / entry
        if target in kind_of:
            add(target, reason)
    return selection


def report(selection: Selection, since: datetime) -> str:
    lines = [f"Files changed since {since.isoformat(timespec='minutes')}: {len(selection.changed)}"]
    if selection.core_hits:
        lines.append("Shared files changed (their group's tests added): " + ", ".join(selection.core_hits[:8])
                     + (" ..." if len(selection.core_hits) > 8 else ""))
    for title, group in [("PHP tests", selection.php_tests), ("Browser suites", selection.web_tests),
                         ("Tool and Python tests", selection.py_tests), ("Native tests (run on the lab with --appliance)", selection.native_tests)]:
        if group:
            lines.append(f"{title}: {len(group)}")
            lines.extend(f"  {path}  ({reason})" for path, reason in sorted(group.items()))
    lines.append(f"Lint: {len(selection.lint_php)} PHP, {len(selection.lint_shell)} shell, {len(selection.lint_python)} Python files")
    return "\n".join(lines)
