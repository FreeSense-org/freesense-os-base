"""Images and the website release follow the FreeBSD pin, not every daily build."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import images_due  # noqa: E402

BASE = "https://pkg.example/v1"
PIN = {"amd64": "a" * 64, "arm64": "b" * 64}


def plan(pins=PIN):
    return {"targets": {arch: {"system": {"freebsd_pin_id": pin}} for arch, pin in pins.items()}}


def live(pins):
    objects = {}
    for arch, pin in pins.items():
        system = {"amd64": "1", "arm64": "2"}[arch] * 64
        objects[f"{BASE}/releases/devel.{arch}.json"] = {"system": system}
        objects[f"{BASE}/artifacts/system/{system}/complete.json"] = {"inputs": {"freebsd_pin_id": pin}}
    return objects.get


class ImagesDueTests(unittest.TestCase):
    def test_images_on_the_current_pin_are_not_rebuilt(self):
        self.assertEqual(images_due.decide(plan(), "auto", BASE, live(PIN))[0], False)

    def test_a_pin_rollover_on_either_architecture_makes_images_due(self):
        due, reason = images_due.decide(plan(), "auto", BASE, live({**PIN, "arm64": "c" * 64}))
        self.assertTrue(due)
        self.assertIn("arm64", reason)

    def test_a_missing_release_makes_images_due(self):
        due, reason = images_due.decide(plan(), "auto", BASE, live({"amd64": PIN["amd64"]}))
        self.assertTrue(due)
        self.assertIn("no arm64 image release", reason)

    def test_dispatch_can_force_or_skip(self):
        self.assertTrue(images_due.decide(plan(), "force", BASE, live(PIN))[0])
        self.assertFalse(images_due.decide(plan(), "skip", BASE, live({}))[0])
        with self.assertRaises(ValueError):
            images_due.decide(plan(), "sometimes", BASE, live(PIN))


if __name__ == "__main__":
    unittest.main()
