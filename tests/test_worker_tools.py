from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "resolve_worker_tools", ROOT / "scripts/resolve_worker_tools.py"
)
assert SPEC and SPEC.loader
worker_tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker_tools)


ROOT_ORIGINS = {
    "gtar": "archivers/gtar",
    "git-tiny": "devel/git",
    "rclone": "net/rclone",
    "jq": "textproc/jq",
    "poudriere-devel": "ports-mgmt/poudriere-devel",
    "xmlstarlet": "textproc/xmlstarlet",
}
SHA256 = "1$" + "0" * 64
PORTS_SHA = "7" * 40


def package(
    name: str,
    *,
    version: str = "1.0",
    origin: str | None = None,
    dependencies: dict[str, dict[str, str]] | None = None,
    repopath: str | None = None,
    checksum: str = SHA256,
    size: int = 4,
    timestamp: str = "2026-07-19T02:00:00+0000",
    ports_commit: str = PORTS_SHA,
) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "origin": origin or ROOT_ORIGINS.get(name, f"devel/{name}"),
        "repopath": repopath or f"All/{name}-{version}.pkg",
        "sum": checksum,
        "pkgsize": size,
        "deps": dependencies or {},
        "annotations": {
            "build_timestamp": timestamp,
            "ports_top_git_hash": ports_commit,
        },
    }


def catalog(*extra: dict[str, object], omit: str | None = None) -> list[str]:
    records = [package(name) for name in reversed(worker_tools.ROOT_NAMES) if name != omit]
    records.extend(extra)
    return [json.dumps(record) for record in records]


class WorkerToolResolutionTests(unittest.TestCase):
    def test_streams_catalog_and_emits_dependency_first_deterministic_closure(self):
        dependency = package("libworker", version="1.2_3,1")
        records = [json.loads(line) for line in catalog(dependency)]
        for record in records:
            if record["name"] == "gtar":
                record["deps"] = {
                    "libworker": {"version": "1.2_3,1", "origin": "devel/libworker"}
                }
        first = worker_tools.resolve_worker_tools(json.dumps(record) for record in records)
        second = worker_tools.resolve_worker_tools(
            json.dumps(record) for record in reversed(records)
        )
        self.assertEqual(first, second)
        self.assertEqual(first["roots"], list(worker_tools.ROOT_NAMES))
        self.assertEqual(first["commands"], list(worker_tools.COMMANDS))
        self.assertEqual(first["ports_sha"], PORTS_SHA)
        names = [entry["name"] for entry in first["packages"]]
        self.assertLess(names.index("libworker"), names.index("gtar"))
        libworker = next(entry for entry in first["packages"] if entry["name"] == "libworker")
        self.assertEqual(libworker["local_file"], "All/libworker-1.2_3,1.pkg")
        self.assertEqual(
            first["install_order"], [entry["local_file"] for entry in first["packages"]]
        )

    def test_missing_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing worker-tool root"):
            worker_tools.resolve_worker_tools(catalog(omit="xmlstarlet"))

    def test_duplicate_package_and_json_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate package name"):
            worker_tools.resolve_worker_tools(catalog(package("gtar")))
        duplicate_key = (
            '{"name":"one","name":"two","version":"1",'
            '"origin":"devel/one","repopath":"All/one-1.pkg",'
            '"sum":"1$' + "0" * 64 + '","pkgsize":1}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            worker_tools.read_catalog([duplicate_key])

    def test_missing_dependency_is_rejected(self):
        records = [json.loads(line) for line in catalog()]
        records[0]["deps"] = {
            "not-there": {"version": "1.0", "origin": "devel/not-there"}
        }
        with self.assertRaisesRegex(ValueError, "missing dependency"):
            worker_tools.resolve_worker_tools(json.dumps(record) for record in records)

    def test_dependency_version_and_origin_must_match_catalog(self):
        dependency = package("libworker", version="2.0")
        records = [json.loads(line) for line in catalog(dependency)]
        records[0]["deps"] = {
            "libworker": {"version": "1.0", "origin": "devel/libworker"}
        }
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            worker_tools.resolve_worker_tools(json.dumps(record) for record in records)
        records[0]["deps"]["libworker"]["version"] = "2.0"
        records[0]["deps"]["libworker"]["origin"] = "devel/wrong"
        with self.assertRaisesRegex(ValueError, "origin mismatch"):
            worker_tools.resolve_worker_tools(json.dumps(record) for record in records)

    def test_dependency_cycle_is_rejected(self):
        first = package(
            "cycle-a",
            dependencies={"cycle-b": {"version": "1.0", "origin": "devel/cycle-b"}},
        )
        second = package(
            "cycle-b",
            dependencies={"cycle-a": {"version": "1.0", "origin": "devel/cycle-a"}},
        )
        records = [json.loads(line) for line in catalog(first, second)]
        for record in records:
            if record["name"] == "gtar":
                record["deps"] = {
                    "cycle-a": {"version": "1.0", "origin": "devel/cycle-a"}
                }
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            worker_tools.resolve_worker_tools(json.dumps(record) for record in records)

    def test_newest_package_generation_selects_one_ports_commit(self):
        old = package(
            "old-generation",
            timestamp="2026-07-18T02:00:00+0000",
            ports_commit="0" * 40,
        )
        manifest = worker_tools.resolve_worker_tools(catalog(old))
        self.assertEqual(manifest["ports_sha"], PORTS_SHA)

    def test_ambiguous_newest_ports_commits_are_rejected(self):
        conflict = package("conflict", ports_commit="8" * 40)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            worker_tools.resolve_worker_tools(catalog(conflict))

    def test_invalid_package_generation_metadata_is_rejected(self):
        record = package("invalid")
        record["annotations"] = {"build_timestamp": "not-a-date"}
        with self.assertRaisesRegex(ValueError, "build timestamp"):
            worker_tools.read_catalog([json.dumps(record)])

    def test_unsafe_paths_and_unsupported_checksums_are_rejected(self):
        dotted_version = package("usrinfo", version=".10_1", origin="sysutils/usrinfo")
        self.assertEqual(
            worker_tools.read_catalog([json.dumps(dotted_version)])["usrinfo"].version,
            ".10_1",
        )
        hashed = package(
            "hashed",
            repopath="All/Hashed/hashed-1.0~2$mizmz8w9.pkg",
        )
        self.assertEqual(
            worker_tools.read_catalog([json.dumps(hashed)])["hashed"].remote_path,
            hashed["repopath"],
        )
        with self.assertRaisesRegex(ValueError, "repository path"):
            worker_tools.read_catalog(
                [json.dumps(package("unsafe", repopath="All/../unsafe.pkg"))]
            )
        for checksum in ("3$" + "0" * 64, "4$" + "0" * 64, "6$" + "0" * 64):
            with self.subTest(checksum=checksum[:2]), self.assertRaisesRegex(
                ValueError, "unsupported"
            ):
                worker_tools.parse_checksum(checksum)


class WorkerToolChecksumTests(unittest.TestCase):
    CHECKSUMS = (
        "0$7ub7ikqurt3njgnud5micksfxyq5j6juyq1z3zqawmfjyx85zdcy",
        "1$7d865e959b2466918c9863afca942d0fb89d7c9ac0c99bafc3749504ded97730",
        "2$gf8mcrnmm6p6hg6wa9xkfb98zo8g6nxu8z4q7s93boz8hzf5ogrsr4qgpsb7utd6speio3op18ocyrsa9ms8jj15byttiq7ofbih8gn",
        "5$dqi4rzroazhfbq4sd33ektsg3jjsrye7mc37ggsa9bt3mhxsyddy",
    )

    def test_matches_freebsd_checksum_vectors_for_bar_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "fixture.pkg")
            path.write_bytes(b"bar\n")
            for checksum in self.CHECKSUMS:
                with self.subTest(checksum_type=checksum[0]):
                    worker_tools.verify_download(path, checksum, 4)

    def test_corruption_and_size_mismatches_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "fixture.pkg")
            path.write_bytes(b"bar\n")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                worker_tools.verify_download(path, self.CHECKSUMS[1], 5)
            path.write_bytes(b"bad\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                worker_tools.verify_download(path, self.CHECKSUMS[1], 4)

    def test_manifest_verification_requires_exact_download_set(self):
        checksum = self.CHECKSUMS[1]
        records = [json.loads(line) for line in catalog()]
        for record in records:
            record["sum"] = checksum
            record["pkgsize"] = 4
        manifest = worker_tools.resolve_worker_tools(json.dumps(record) for record in records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "All").mkdir()
            for local_file in manifest["install_order"]:
                (root / local_file).write_bytes(b"bar\n")
            worker_tools.verify_manifest_downloads(manifest, root)
            self.assertTrue(
                all(
                    package["sha256"]
                    == "7d865e959b2466918c9863afca942d0fb89d7c9ac0c99bafc3749504ded97730"
                    for package in manifest["packages"]
                )
            )
            (root / "All/unexpected-1.pkg").write_bytes(b"bar\n")
            with self.assertRaisesRegex(ValueError, "exact worker-tool closure"):
                worker_tools.verify_manifest_downloads(manifest, root)

    def test_manifest_verification_binds_exact_runtime_commands(self):
        manifest = {
            "schema_version": worker_tools.SCHEMA_VERSION,
            "ports_sha": PORTS_SHA,
            "roots": list(worker_tools.ROOT_NAMES),
            "commands": ["unexpected"],
            "package_count": 0,
            "packages": [],
            "install_order": [],
        }
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            ValueError, "command checks"
        ):
            worker_tools.verify_manifest_downloads(manifest, Path(directory))


if __name__ == "__main__":
    unittest.main()
