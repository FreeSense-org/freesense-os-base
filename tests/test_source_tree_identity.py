"""Components name what a build reads from a repository, not the commit."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import plan  # noqa: E402

REPOSITORY = "FreeSense-org/freesense-packages"


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class SourceTreeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.upstream = root / REPOSITORY
        self.upstream.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.upstream)
        (self.upstream / "net").mkdir()
        (self.upstream / "net" / "Makefile").write_text("PORTNAME=x\n", encoding="utf-8")
        (self.upstream / "docs").mkdir()
        (self.upstream / "docs" / "guide.md").write_text("one\n", encoding="utf-8")
        (self.upstream / "README.md").write_text("one\n", encoding="utf-8")
        git("add", "-A", cwd=self.upstream)
        git("commit", "-q", "-m", "first", cwd=self.upstream)
        patches = [
            mock.patch.object(plan, "SOURCE_TREE_CACHE", root / "cache"),
            mock.patch.object(plan, "SOURCE_URL", root.as_uri() + "/{repository}"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.directory.cleanup)

    def commit(self, path, text):
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git("add", "-A", cwd=self.upstream)
        git("commit", "-q", "-m", f"change {path}", cwd=self.upstream)
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def digest(self, commit):
        return plan.source_tree_digest(REPOSITORY, commit)

    def test_unread_paths_keep_the_identity_and_read_paths_change_it(self):
        first = git("rev-parse", "HEAD", cwd=self.upstream)
        docs = self.commit("docs/guide.md", "two\n")
        readme = self.commit("README.md", "two\n")
        self.assertNotEqual(first, readme)
        self.assertEqual(self.digest(first), self.digest(docs))
        self.assertEqual(self.digest(first), self.digest(readme))
        port = self.commit("net/Makefile", "PORTNAME=y\n")
        self.assertNotEqual(self.digest(readme), self.digest(port))

    def test_a_new_top_level_entry_counts_until_it_is_listed(self):
        first = git("rev-parse", "HEAD", cwd=self.upstream)
        added = self.commit("www/Makefile", "PORTNAME=z\n")
        self.assertNotEqual(self.digest(first), self.digest(added))

    def test_every_repository_a_component_reads_has_an_explicit_list(self):
        self.assertEqual(set(plan.SOURCE_UNREAD), {
            "FreeSense-org/freesense", "FreeSense-org/freesense-system-ports", "FreeSense-org/freesense-packages"})
        # LICENSE is embedded into packages by the builder.
        self.assertNotIn("LICENSE", plan.SOURCE_UNREAD["FreeSense-org/freesense"])

    def test_identities_name_trees_not_commits(self):
        source = (ROOT / "scripts/plan.py").read_text(encoding="utf-8")
        self.assertIn('"source_tree": source_tree,', source)
        self.assertIn('"system_ports_tree": system_ports_tree,', source)
        self.assertIn('"packages_tree": packages_tree,', source)
        self.assertNotIn('"source": latest_source_sha,', source)
        self.assertNotIn('"packages": packages_sha,', source)


if __name__ == "__main__":
    unittest.main()
