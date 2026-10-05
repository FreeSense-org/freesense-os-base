"""Devices get System and Packages every run; images and the website follow the FreeBSD pin."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
MULTIARCH = (WORKFLOWS / "development-multiarch.yml").read_text(encoding="utf-8")
PUBLISH = (WORKFLOWS / "publish-qualified-development.yml").read_text(encoding="utf-8")
AGGREGATE = (WORKFLOWS / "development-multiarch-publish.yml").read_text(encoding="utf-8")


def job(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z_0-9]+:\n|\Z)", text, re.S | re.M)
    assert match, name
    return match.group(1)


class DailyAndImagePublicationTests(unittest.TestCase):
    def test_devices_are_published_after_repository_verification_without_images(self):
        verify = job(MULTIARCH, "verify_repositories")
        self.assertIn("needs: [repository_amd64, repository_arm64]", verify)
        self.assertIn("--repositories-only", verify)
        self.assertNotIn("multiarch-release-", verify)
        for arch in ("amd64", "arm64"):
            publish = job(MULTIARCH, f"publish_repos_{arch}")
            self.assertIn(f"needs: [prepare, repository_{arch}, verify_repositories]", publish)
            self.assertIn(f"with: {{architecture: {arch}, release: false}}", publish)
            self.assertNotIn("release_", publish.split("with:")[0])

    def test_images_run_only_when_due_and_survive_disabled_publication(self):
        due = job(MULTIARCH, "images_due")
        self.assertIn("scripts/images_due.py", due)
        self.assertIn("inputs.images || 'auto'", due)
        self.assertIn("!cancelled()", due)
        for arch in ("amd64", "arm64"):
            release = job(MULTIARCH, f"release_{arch}")
            self.assertIn("images_due", release.split("\n", 1)[0])
            self.assertIn("needs.images_due.outputs.due == 'true'", release)
            self.assertIn("!cancelled()", release)
            images = job(MULTIARCH, f"publish_images_{arch}")
            self.assertIn("verify_pair", images.split("\n", 1)[0])
            self.assertIn(f"with: {{architecture: {arch}, release: true}}", images)
        self.assertIn("needs.release_amd64.result == 'success' && needs.release_arm64.result == 'success'",
                      job(MULTIARCH, "verify_pair"))
        self.assertIn("options: [auto, force, skip]", MULTIARCH)

    def test_the_publish_workflow_commits_repositories_or_the_full_release(self):
        self.assertIn("release: {type: boolean, required: true}", PUBLISH)
        self.assertRegex(PUBLISH, r"name: Commit the repositories devices update from\n\s+if: \$\{\{ !inputs.release \}\}")
        self.assertIn("multiarch commit-repositories", PUBLISH)
        for step in ("Acquire downloads writer", "Publish immutable downloads", "Commit qualified architecture"):
            self.assertRegex(PUBLISH, rf"name: {step}\n\s+if: inputs.release")
        # A device-only publication leaves the cycle's image status as it was.
        self.assertIn('($old.artifacts // "pending")', PUBLISH)

    def test_the_aggregate_release_runs_only_for_runs_that_released_images(self):
        self.assertIn("multiarch-authoritative-completion", job(AGGREGATE, "released"))
        publish = job(AGGREGATE, "publish")
        self.assertIn("needs: released", publish)
        self.assertIn("if: needs.released.outputs.images == 'true'", publish)


if __name__ == "__main__":
    unittest.main()
