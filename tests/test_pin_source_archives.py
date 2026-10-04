"""Pinned FreeBSD src and ports archives: validation, planning and clone path."""
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import multiarch_pin  # noqa: E402
import pin_mirrors  # noqa: E402
import pin_source_archives  # noqa: E402
from test_multiarch import pin_fixture  # noqa: E402

SRC = "a" * 40
PORTS = "b" * 40


def entry(repository, letter):
    return {"repository": repository, "format": multiarch_pin.SOURCE_ARCHIVE_FORMAT,
            "ref": "refs/heads/main", "object": "inputs/sha256/" + letter * 64,
            "sha256": letter * 64, "size": 100}


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SourceArchiveValidationTests(unittest.TestCase):
    def test_pin_without_archives_stays_valid(self):
        pin = pin_fixture()
        pin.pop("source_archives", None)
        multiarch_pin.validate(pin)
        self.assertEqual(multiarch_pin.source_archive(pin, SRC, "freebsd/freebsd-src"), "")

    def test_lookup_is_bound_to_commit_and_repository(self):
        pin = pin_fixture()
        pin["source_archives"] = {SRC: entry("freebsd/freebsd-src", "c"), PORTS: entry("freebsd/freebsd-ports", "d")}
        multiarch_pin.validate(pin)
        self.assertEqual(multiarch_pin.source_archive(pin, SRC, "freebsd/freebsd-src"), "inputs/sha256/" + "c" * 64)
        self.assertEqual(multiarch_pin.source_archive(pin, SRC, "freebsd/freebsd-ports"), "")
        self.assertEqual(multiarch_pin.source_archive(pin, "e" * 40, "freebsd/freebsd-src"), "")

    def test_malformed_archives_fail_closed(self):
        broken = {
            "short commit": {"abc": entry("freebsd/freebsd-src", "c")},
            "other repository": {SRC: entry("FreeSense-org/freesense", "c")},
            "other format": {SRC: {**entry("freebsd/freebsd-src", "c"), "format": "tar"}},
            "other ref": {SRC: {**entry("freebsd/freebsd-src", "c"), "ref": "refs/heads/stable"}},
            "mutable object": {SRC: {**entry("freebsd/freebsd-src", "c"), "object": "latest/src.tar"}},
        }
        for label, archives in broken.items():
            pin = pin_fixture()
            pin["source_archives"] = archives
            with self.subTest(label), self.assertRaises(ValueError):
                multiarch_pin.validate(pin)


class SourceArchiveCycleTests(unittest.TestCase):
    def pin(self, mirror_ports=None):
        pin = {"freebsd_source": {"commit": SRC}, "freebsd_ports": {"commit": PORTS}, "targets": {}}
        if mirror_ports:
            pin["targets"]["amd64"] = {"mirror": {"ports_commit": mirror_ports}}
        return pin

    def test_mirror_ports_commit_is_archived_too(self):
        self.assertEqual(multiarch_pin.archivable_commits(self.pin("f" * 40)), {
            SRC: "freebsd/freebsd-src", PORTS: "freebsd/freebsd-ports", "f" * 40: "freebsd/freebsd-ports"})

    def test_previous_archives_are_carried_without_fetching_and_stale_ones_dropped(self):
        previous = {"source_archives": {SRC: entry("freebsd/freebsd-src", "c"),
                                        PORTS: entry("freebsd/freebsd-ports", "d"),
                                        "e" * 40: entry("freebsd/freebsd-ports", "e")}}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(pin_source_archives, "fetch_bare") as fetch:
            updated = pin_source_archives.archive(self.pin(), previous, "fsbuild", Path(directory))
        fetch.assert_not_called()
        self.assertEqual(set(updated["source_archives"]), {SRC, PORTS})

    def test_new_commits_are_stored_once(self):
        with tempfile.TemporaryDirectory() as directory,                 mock.patch.object(pin_source_archives, "store",
                                  side_effect=lambda repository, commit, *_: entry(repository, "c")) as store:
            updated = pin_source_archives.archive(self.pin(), {}, "fsbuild", Path(directory))
        self.assertEqual(sorted(call.args[:2] for call in store.call_args_list),
                         [("freebsd/freebsd-ports", PORTS), ("freebsd/freebsd-src", SRC)])
        self.assertEqual(updated["source_archives"][SRC]["repository"], "freebsd/freebsd-src")

    def test_recut_mirror_records_its_archive_and_drops_the_old_commit(self):
        old, new = "e" * 40, "f" * 40
        pin = self.pin(old)
        pin["source_archives"] = {SRC: entry("freebsd/freebsd-src", "c"), old: entry("freebsd/freebsd-ports", "e")}
        pin["targets"]["amd64"]["mirror"]["ports_commit"] = new
        recorded = pin_mirrors.ports_archive({"ports_commit": new},
                                             {"commit": new, **entry("freebsd/freebsd-ports", "d")})
        updated = multiarch_pin.with_source_archives(pin, recorded)
        self.assertEqual(set(updated["source_archives"]), {SRC, new})
        self.assertNotIn("commit", updated["source_archives"][new])

    def test_archive_for_another_commit_is_refused(self):
        with self.assertRaises(ValueError):
            pin_mirrors.ports_archive({"ports_commit": "f" * 40},
                                      {"commit": "e" * 40, **entry("freebsd/freebsd-ports", "d")})


class SourceArchiveCloneTests(unittest.TestCase):
    """The archive must serve the pinned commit to a plain file:// fetch."""

    def test_archived_commit_clones_and_fetches_over_file_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            git("init", "-q", "-b", "main", cwd=upstream)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "old", cwd=upstream)
            (upstream / "Makefile").write_text("all:\n", encoding="utf-8")
            git("add", "Makefile", cwd=upstream)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "pinned", cwd=upstream)
            commit = git("rev-parse", "HEAD", cwd=upstream)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "newer", cwd=upstream)

            bare = root / "work" / "freebsd-src.git"
            bare.parent.mkdir()
            pin_source_archives.fetch_bare("freebsd/freebsd-src", commit, bare, upstream.as_uri())
            tar = root / "work" / "freebsd-src.git.tar"
            pin_source_archives.write_tar(bare, tar)
            with tarfile.open(tar) as archive:
                members = archive.getmembers()
                self.assertTrue(all(m.uid == 0 and m.mtime == 0 for m in members))
                self.assertFalse(any("FETCH_HEAD" in m.name or "/hooks" in m.name for m in members))
                archive.extractall(root / "vm", filter="data")
            served = (root / "vm" / "freebsd-src.git").as_uri()

            # The builder's src path: fetch the exact commit into an empty tree.
            tree = root / "src"
            tree.mkdir()
            git("init", "-q", cwd=tree)
            git("remote", "add", "origin", served, cwd=tree)
            git("fetch", "-q", "--depth", "1", "origin", commit, cwd=tree)
            git("checkout", "-q", "-f", commit, cwd=tree)
            self.assertTrue((tree / "Makefile").is_file())

            # Poudriere's ports path: shallow clone of main, then pin the commit.
            ports = root / "ports"
            git("clone", "-q", "--depth", "1", "-b", "main", served, str(ports))
            self.assertEqual(git("rev-parse", "HEAD", cwd=ports), commit)
            git("fetch", "-q", "--depth", "1", "origin", commit, cwd=ports)


class WorkerRestoreTests(unittest.TestCase):
    """worker-common.sh restore_upstream and the real fetch_input against a real archive.

    Only rclone and FreeBSD's sha256 are stubbed: fetch_input assigns globals
    (sh has no locals), and a stubbed fetch_input once hid that it clobbered
    restore_upstream's destination.
    """

    @staticmethod
    def function(common, name):
        start = common.index(f"{name}() {{")
        return common[start:common.index("\n}\n", start) + 3]

    def restore(self, root, tar, object_name, destination, commit):
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        # Relative paths: GNU tar on Windows reads "C:" as a remote host.
        tar, destination = tar.relative_to(root).as_posix(), destination.relative_to(root).as_posix()
        script = ("set -eu\nphase() { :; }\nR2_BUCKET=bucket PREFIX=v1\n"
                  f"rclone() {{ for last; do :; done; cp '{tar}' \"$last\"; }}\n"
                  "sha256() { sha256sum \"$2\" | cut -d' ' -f1; }\n"
                  f"{self.function(common, 'fetch_input')}{self.function(common, 'restore_upstream')}"
                  f"restore_upstream '{object_name}' '{destination}' '{commit}'\n")
        return subprocess.run(["sh", "-c", script], cwd=root, capture_output=True, text=True)

    def test_restores_the_pinned_commit_and_refuses_anything_else(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            git("init", "-q", "-b", "main", cwd=upstream)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "pinned", cwd=upstream)
            commit = git("rev-parse", "HEAD", cwd=upstream)
            bare = root / "work" / "freebsd-ports.git"
            bare.parent.mkdir()
            pin_source_archives.fetch_bare("freebsd/freebsd-ports", commit, bare, upstream.as_uri())
            tar = root / "work" / "ports.tar"
            pin_source_archives.write_tar(bare, tar)
            vm = root / "vm"
            vm.mkdir()
            destination = vm / "freebsd-ports.git"
            import hashlib
            stored = "inputs/sha256/" + hashlib.sha256(tar.read_bytes()).hexdigest()
            restored = self.restore(root, tar, stored, destination, commit)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(git("rev-parse", "refs/heads/main", cwd=destination), commit)
            self.assertFalse((vm / "freebsd-ports.git.tar").exists())
            corrupt = self.restore(root, tar, "inputs/sha256/" + "c" * 64, destination, commit)
            self.assertNotEqual(corrupt.returncode, 0)
            self.assertIn("checksum mismatch", corrupt.stderr)
            wrong = self.restore(root, tar, stored, destination, "f" * 40)
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("does not hold the pinned commit", wrong.stderr)
            mutable = self.restore(root, tar, "latest/ports.tar", destination, commit)
            self.assertNotEqual(mutable.returncode, 0)
            self.assertIn("not an immutable input", mutable.stderr)

    def test_builder_uses_the_restored_trees_only_when_pinned(self):
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        self.assertIn('export POUDRIERE_PORTS_GIT_URL="${ports_url}"', common)
        self.assertIn("ports_url=https://github.com/freebsd/freebsd-ports.git", common)
        self.assertIn('UPSTREAM_URL="file:///root/freebsd-src.git"', common)
        render = (ROOT / "scripts/render-worker.py").read_text(encoding="utf-8")
        self.assertIn('"FREEBSD_SRC_OBJECT": ""', render)
        self.assertIn('"PORTS_OBJECT": ""', render)


class ShellExpansionTests(unittest.TestCase):
    """sh -n accepts a malformed ${...}; it only fails when the line runs."""

    def test_every_parameter_expansion_names_a_valid_variable(self):
        import re
        expansion = re.compile(r"\$\{([^}]*)\}")
        # Also allowed: bash arrays (${a[@]}), make variables in heredocs
        # (${.CURDIR:...}) and the inner half of an eval'd ${${name}_B64}.
        valid = re.compile(r"(\$\{)?#?([A-Za-z_.][A-Za-z0-9_.]*|[0-9]+|[@*#?$!-])(\[[^]]*\])?([:%#/+=?-].*)?", re.S)
        scripts = [ROOT / "scripts/runner/worker-common.sh", *sorted((ROOT / "scripts/runner").glob("*.sh")),
                   *sorted((ROOT / "scripts/runner/stages").glob("*.sh"))]
        for script in dict.fromkeys(scripts):
            for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
                for body in expansion.findall(line):
                    with self.subTest(script=script.name, line=number):
                        self.assertIsNotNone(valid.fullmatch(body), f"{script.name}:{number}: ${{{body}}}")


if __name__ == "__main__":
    unittest.main()
