"""A Stable build seeds from the last Stable repository of its pin, then from Development."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("freesense_stable_seed", ROOT / "scripts/stable_farm_plan.py")
stable_farm_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stable_farm_plan)

PIN = "c" * 64
OTHER_PIN = "d" * 64


def records(**channels):
    calls = []

    def current(manifest, component, channel="devel"):
        calls.append((component, channel))
        return channels.get(channel, {})
    return current, calls


class StableSeedTests(unittest.TestCase):
    def seed(self, **channels):
        current, calls = records(**channels)
        original = stable_farm_plan.current_component_record
        stable_farm_plan.current_component_record = current
        try:
            return stable_farm_plan.stable_seed("manifest", "packages", {"freebsd_pin_id": PIN}), calls
        finally:
            stable_farm_plan.current_component_record = original

    def test_the_stable_record_of_the_same_pin_wins(self):
        seed, calls = self.seed(stable={"fingerprint": "a" * 64, "freebsd_pin_id": PIN},
                                devel={"fingerprint": "b" * 64, "freebsd_pin_id": PIN})
        self.assertEqual(seed, "a" * 64)
        self.assertEqual(calls, [("packages", "stable")])

    def test_development_is_used_when_stable_has_another_pin(self):
        # The regression: arm64 Development was built on another pin id, and
        # Stable was never consulted, so every arm64 Stable build compiled
        # all of its ports.
        seed, _ = self.seed(stable={"fingerprint": "a" * 64, "freebsd_pin_id": OTHER_PIN},
                            devel={"fingerprint": "b" * 64, "freebsd_pin_id": PIN})
        self.assertEqual(seed, "b" * 64)

    def test_no_record_on_the_pin_gives_no_seed(self):
        seed, _ = self.seed(stable={"fingerprint": "a" * 64, "freebsd_pin_id": OTHER_PIN})
        self.assertEqual(seed, "")

    def test_both_components_use_the_stable_seed(self):
        source = (ROOT / "scripts/stable_farm_plan.py").read_text(encoding="utf-8")
        self.assertIn('stable_seed(manifest, "system", system, seeds)', source)
        self.assertIn('stable_seed(manifest, "packages", packages, seeds)', source)
        self.assertEqual(stable_farm_plan.SEED_CHANNELS, ("stable", "devel"))


if __name__ == "__main__":
    unittest.main()
