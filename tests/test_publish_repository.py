"""publish_repository copies unchanged packages server-side and uploads the rest once."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "p" * 64


def function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


@unittest.skipUnless(shutil.which("sh") and shutil.which("jq") and shutil.which("cmp"), "needs sh, jq and cmp")
class PublishRepositoryTests(unittest.TestCase):
    def run_publish(self, root: Path, previous: str, copy_fails: bool = False) -> list[str]:
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        calls = root / "rclone-calls"
        rclone = root / "bin" / "rclone"
        rclone.parent.mkdir()
        # Record each call with its file list; optionally fail server-side copies.
        rclone.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >>'{calls}'\n"
            "list=''; prev=''; for a in \"$@\"; do [ \"$prev\" = --files-from-raw ] && list=$a; prev=$a; done\n"
            f"[ -n \"$list\" ] && sed 's/^/  /' \"$list\" >>'{calls}'\n"
            + ("case \"$*\" in *'R2:bucket/v1/artifacts/'*' R2:'*) exit 1 ;; esac\n" if copy_fails else "")
            + "exit 0\n", encoding="utf-8")
        rclone.chmod(0o755)
        stubs = (
            "set -eu\n"
            "phase() { :; }\nrecord_upstream_provenance() { :; }\nrecord_package_provenance() { :; }\n"
            "upload_immutable() { printf 'upload_immutable %s\\n' \"$2\" >>'CALLS'; }\n"
            "sha256() { sha256sum \"$2\" | cut -d' ' -f1; }\n").replace("CALLS", str(calls))
        script = (
            stubs
            + function(common, "publish_repository").replace(
                "/root/previous-freesense-repository", str(root / "root/previous-freesense-repository"))
            + f"PATH='{rclone.parent}':$PATH\n"
            f"R2_BUCKET=bucket PREFIX=v1 RESULT=R2:bucket/v1/artifacts/system/new PACKAGE_ARCH=amd64 STAGE=system\n"
            f"PREVIOUS_FREESENSE_REPOSITORY='{previous}' PACKAGE_TRAIN=1.1 BINARY_SEED_OBJECT=''\n"
            "FINGERPRINT=f PLATFORM_ID=p SYSTEM_ID=s SOURCE_SHA=a SYSTEM_SHA=b PACKAGES_SHA=c FREEBSD_SHA=d PORTS_SHA=e\n"
            "OS_BASE_SHA=g IMAGE_SHA256=h WORKER_TOOLS_SHA256=i FREEBSD_PIN_ID=j JAIL_OBJECT=k derived_fingerprint=l\n"
            "ARCHITECTURE=amd64 IMAGE_PROFILE=generic-amd64 FIRMWARE=uefi IMAGE_CAPABILITIES='{}' GENERATION=1\n"
            "publish_repository repo\n")
        result = subprocess.run(["sh", "-c", script], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return calls.read_text(encoding="utf-8").splitlines(), result.stdout

    def tree(self, root: Path) -> None:
        for base in ("repo", "root/previous-freesense-repository"):
            (root / base / "All").mkdir(parents=True)
        for name, content in (("same-1.pkg", "same"), ("changed-1.pkg", "new")):
            (root / "repo/All" / name).write_text(content, encoding="utf-8")
        (root / "root/previous-freesense-repository/All/same-1.pkg").write_text("same", encoding="utf-8")
        (root / "root/previous-freesense-repository/All/changed-1.pkg").write_text("old", encoding="utf-8")
        (root / "repo/meta.conf").write_text("meta", encoding="utf-8")
        (root / "repo/package-provenance.json").write_text("{}", encoding="utf-8")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tree(self.root)

    def publish(self, previous=PREVIOUS, copy_fails=False):
        # The function reads /root/previous-freesense-repository; run it with
        # that path rewritten into the temporary tree.
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        self.assertIn("previous_repository=/root/previous-freesense-repository", source)
        return self.run_publish(self.root, previous, copy_fails)

    def test_unchanged_packages_are_copied_server_side_and_the_rest_uploaded_once(self):
        calls, output = self.publish()
        copy = next(i for i, c in enumerate(calls) if f"artifacts/system/{PREVIOUS}/amd64" in c)
        self.assertIn("--immutable", calls[copy])
        self.assertEqual(calls[copy + 1:copy + 2], ["  All/same-1.pkg"])
        upload = next(i for i, c in enumerate(calls) if c.startswith("copy ") and " repo R2:" in c)
        uploaded = [c.strip() for c in calls[upload + 1:] if c.startswith("  ")]
        self.assertEqual(sorted(uploaded), ["All/changed-1.pkg", "meta.conf", "package-provenance.json"])
        self.assertEqual([c for c in calls if c.startswith("copy ")].__len__(), 2)
        self.assertIn("1 copied server-side", output)

    def test_a_failed_server_side_copy_falls_back_to_uploading(self):
        calls, output = self.publish(copy_fails=True)
        upload = next(i for i, c in enumerate(calls) if c.startswith("copy ") and " repo R2:" in c)
        uploaded = [c.strip() for c in calls[upload + 1:] if c.startswith("  ")]
        self.assertIn("All/same-1.pkg", uploaded)
        self.assertIn("0 copied server-side", output)

    def test_without_a_previous_repository_everything_is_uploaded(self):
        calls, output = self.publish(previous="")
        self.assertFalse(any("artifacts/system/" in c and "/amd64 R2:" in c for c in calls))
        self.assertIn("4 uploaded", output)


if __name__ == "__main__":
    unittest.main()
