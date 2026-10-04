"""A build seeds unchanged packages from the published repository on its pin."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from multiarch_pin import digest  # noqa: E402
from multiarch_plan import previous_seed  # noqa: E402

PIN = "c" * 64
PUBLISHED = {"fingerprint": "a" * 64, "freebsd_pin_id": PIN}


class PreviousSeedTests(unittest.TestCase):
    def test_same_pin_seeds_from_the_published_repository(self):
        self.assertEqual(previous_seed(PUBLISHED, {"freebsd_pin_id": PIN}), "a" * 64)

    def test_another_pin_or_missing_record_gives_no_seed(self):
        self.assertEqual(previous_seed(PUBLISHED, {"freebsd_pin_id": "d" * 64}), "")
        self.assertEqual(previous_seed({}, {"freebsd_pin_id": PIN}), "")
        self.assertEqual(previous_seed(PUBLISHED, {}), "")
        self.assertEqual(previous_seed({**PUBLISHED, "fingerprint": "not-a-digest"}, {"freebsd_pin_id": PIN}), "")

    def test_the_whole_pin_file_digest_is_not_the_pin_identity(self):
        # The regression: published records carry plan.py's freebsd_pin_id,
        # which a digest of the pin document can never equal.
        pin_document = json.loads((ROOT / "config/freebsd-16.json").read_text(encoding="utf-8"))
        self.assertEqual(previous_seed(PUBLISHED, {"freebsd_pin_id": digest(pin_document)}), "")
        source = (ROOT / "scripts/multiarch_plan.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("previous_seed(previous, "), 2)
        self.assertNotIn('previous.get("freebsd_pin_id") == pin_id', source)


if __name__ == "__main__":
    unittest.main()
