"""Check the local estate ledger, optionally comparing it to the pinned sources."""

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "planning/inventory"
COLUMNS = ("source", "path", "mode", "object", "surface")
DECISIONS = ("source", "path", "status", "replacement", "evidence")
SCRIPT_SUFFIXES = {".bash", ".sh", ".ps1", ".py", ".rb", ".awk", ".pl", ".lua"}


def surface(source: str, path: str, mode: str) -> str:
    """Describe a review entry point. Execution and retirement require caller tracing."""
    item = PurePosixPath(path)
    if mode == "160000":
        return "git-submodule"
    if mode == "120000":
        return "symlink"
    if source in {"freesense-site-designs", "branding"}:
        return "design-material"
    if path.startswith(".github/workflows/"):
        return "workflow"
    if item.name.startswith(("pkg-install", "pkg-deinstall", "pkg-pre-", "pkg-post-")):
        return "package-hook"
    if item.name == "pkg-plist":
        return "package-manifest"
    if item.name == "Makefile" or item.suffix == ".mk":
        return "build-definition"
    if source == "freesense" and path.startswith("src/"):
        if path.startswith("src/etc/rc.") and mode == "100755":
            return "system-boot-or-console"
        if path.startswith(("src/usr/local/bin/", "src/usr/local/sbin/")):
            return "system-command"
        if "/widgets/" in path:
            return "system-widget"
        if path.startswith("src/usr/local/www/") and item.suffix == ".php":
            return "system-web-page"
        if "/restapi/" in path:
            return "system-api"
        if mode == "100755" or item.suffix in SCRIPT_SUFFIXES:
            return "system-script-candidate"
        return "system-payload"
    if mode == "100755" or item.suffix in SCRIPT_SUFFIXES or "scripts" in item.parts:
        return "script-candidate"
    if source in {"freesense-packages", "freesense-system-ports"} and "files" in item.parts:
        return "port-input"
    return "repository-file"


def git_rows(repo: Path, name: str, commit: str):
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError(f"invalid commit pin for {name}")
    data = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-rz", commit])
    for record in data.split(b"\0"):
        if record:
            meta, path = record.decode("utf-8").split("\t", 1)
            mode, _, oid = meta.split()
            yield name, path, mode, oid, surface(name, path, mode)


def asset_rows(folder: Path, name: str):
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        raise ValueError(f"expected local asset directory {folder}")
    for item in folder.rglob("*"):
        if item.is_symlink() or item.is_junction():
            raise ValueError(f"review linked asset before pinning {item}")
        if item.is_file():
            with item.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            path = item.relative_to(folder).as_posix()
            yield name, path, "file", digest, surface(name, path, "file")


def snapshot(workspace: Path, sources: dict) -> bytes:
    rows = []
    for name, commit in sources["git"].items():
        rows.extend(git_rows(workspace / "repositories/active" / name, name, commit))
    for name, folder in sources["assets"].items():
        rows.extend(asset_rows(workspace / folder, name))
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(sorted(rows))
    return buffer.getvalue().encode("utf-8")


def read_table(name: str, columns: tuple[str, ...]) -> list[dict]:
    with (DATA / name).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != list(columns):
            raise ValueError(f"invalid header in {name}")
        return list(reader)


def local_artifact(relative: str) -> bool:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
        return False
    candidate = (ROOT / path).resolve()
    return candidate.is_relative_to(ROOT) and candidate.is_file()


# Website components that a workspace may lack (the security repository is private, so CI cannot check it out).
OPTIONAL_COMPONENTS = {"freesense-security", "freesense.org"}


def replacement_exists(relative: str) -> bool:
    component = relative.split("/", 1)[0]
    return (component in OPTIONAL_COMPONENTS and not (ROOT / component).exists()) or local_artifact(relative)


def check_ledger(expected_digest: str, complete: bool) -> None:
    if hashlib.sha256((DATA / "paths.tsv").read_bytes()).hexdigest() != expected_digest:
        raise ValueError("inventory digest differs from the recorded source snapshot")
    items = read_table("paths.tsv", COLUMNS)
    keys = {(row["source"], row["path"]) for row in items}
    if len(keys) != len(items):
        raise ValueError("duplicate source path in inventory")
    seen = set()
    totals = Counter()
    for row in read_table("decisions.tsv", DECISIONS):
        key = row["source"], row["path"]
        status = row["status"]
        if key not in keys or key in seen or status not in {"in-progress", "replaced", "consolidated", "retired"}:
            raise ValueError(f"invalid decision for {key!r}")
        if not local_artifact(row["evidence"]):
            raise ValueError(f"missing local evidence for {key!r}")
        replacement = row["replacement"]
        if status in {"replaced", "consolidated"} or replacement:
            if status == "retired" or not replacement_exists(replacement):
                raise ValueError(f"invalid replacement for {key!r}")
        totals[status] += 1
        seen.add(key)
    open_count = len(keys) - len(seen) + totals["in-progress"]
    print(f"Inventory: {len(keys)} paths, {open_count} awaiting final disposition.")
    if complete and open_count:
        raise ValueError("estate reconciliation is incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--complete", action="store_true", help="require every path to have a final disposition")
    parser.add_argument("--workspace", type=Path, help="FreeSense workspace containing the reference trees")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--verify-sources", action="store_true", help="regenerate in memory and compare all paths and hashes")
    actions.add_argument("--emit", type=Path, help="write a proposed fresh snapshot to a new file for review")
    args = parser.parse_args()
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    if args.verify_sources or args.emit:
        if not args.workspace:
            parser.error("--workspace is required for comparison or regeneration")
        image = snapshot(args.workspace, sources)
        if args.emit:
            with args.emit.open("xb") as handle:
                handle.write(image)
            print(f"Proposed inventory SHA-256 {hashlib.sha256(image).hexdigest()}")
            return
        if image != (DATA / "paths.tsv").read_bytes():
            raise ValueError("reference trees or local asset contents differ from the inventory")
    check_ledger(sources["paths_sha256"], args.complete)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Inventory check failed: {error}")
