"""A Development run fixes its release host when it starts; publication can only be stopped mid-run."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/development-multiarch.yml").read_text(encoding="utf-8")


def job(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z_0-9]+:\n|\Z)", WORKFLOW, re.S | re.M)
    assert match, name
    return match.group(1)


class RunSettingsTests(unittest.TestCase):
    def test_prepare_reads_the_variables_once(self):
        prepare = job("prepare")
        self.assertIn("image_host: ${{ steps.settings.outputs.image_host }}", prepare)
        self.assertIn("publication_enabled: ${{ steps.settings.outputs.publication_enabled }}", prepare)
        self.assertIn("vars.DEVELOPMENT_RELEASE_HOST", prepare)
        self.assertIn("vars.MULTIARCH_PUBLICATION_ENABLED == 'true'", prepare)

    def test_image_jobs_use_the_host_fixed_at_start(self):
        for arch in ("amd64", "arm64"):
            release = job(f"release_{arch}")
            self.assertIn("image_host: ${{ needs.prepare.outputs.image_host }}", release)
            self.assertNotIn("vars.DEVELOPMENT_RELEASE_HOST", release)

    def test_publication_needs_the_flag_at_start_and_at_publish_time(self):
        for arch in ("amd64", "arm64"):
            publish = job(f"publish_{arch}")
            self.assertRegex(publish, r"needs: \[prepare, ")
            self.assertIn("needs.prepare.outputs.publication_enabled == 'true'", publish)
            self.assertIn("vars.MULTIARCH_PUBLICATION_ENABLED == 'true'", publish)
            self.assertIn("github.ref == 'refs/heads/main'", publish)

    def test_no_later_job_reads_these_variables_directly(self):
        later = WORKFLOW.split("\n  system_amd64:", 1)[1]
        self.assertNotIn("vars.DEVELOPMENT_RELEASE_HOST", later)
        self.assertEqual(later.count("vars.MULTIARCH_PUBLICATION_ENABLED"), 2)


if __name__ == "__main__":
    unittest.main()
