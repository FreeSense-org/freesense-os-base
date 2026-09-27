"""Recording cut mirrors in the FreeBSD pin."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pin_mirrors  # noqa: E402

# The amd64 values mirror run 35510255406 produced and #224 pinned.
PLAN = {"schema_version": "freesense.mirror-plan/v1", "abi": "FreeBSD:16:amd64",
        "fingerprint": "667633eecf199f592f9364d1d900913f6abcb32eda5e516c64bcce89e405e80e",
        "ports_commit": "3bbd676e12f5e919cc28b5e2530e18417d2b8b15"}
SHA = "e97857b9d78f278831de5661a05ed7676e20e9ebd2feb872d19e2c596e6c0515"
BLOB = {"schema_version": "freesense.blob/v1", "created": True, "sha256": SHA, "size": 249162,
        "key": f"inputs/sha256/{SHA}", "object": f"inputs/sha256/{SHA}"}
PINNED_BY_224 = {"fingerprint": PLAN["fingerprint"], "object": f"inputs/sha256/{SHA}",
                 "ports_commit": PLAN["ports_commit"]}


def current_pin() -> dict:
    return json.loads((ROOT / "config/freebsd-16.json").read_text(encoding="utf-8"))


def inside(pin: dict) -> datetime:
    return datetime.fromisoformat(pin["valid_from"].replace("Z", "+00:00")) + timedelta(seconds=1)


class PinMirrorsTests(unittest.TestCase):
    def test_entry_reproduces_the_hand_pinned_mirror(self):
        self.assertEqual(pin_mirrors.mirror_entry("amd64", PLAN, BLOB), PINNED_BY_224)

    def test_rejects_plans_for_another_abi_or_unsealed_blobs(self):
        with self.assertRaises(ValueError):
            pin_mirrors.mirror_entry("arm64", PLAN, BLOB)
        with self.assertRaises(ValueError):
            pin_mirrors.mirror_entry("amd64", {**PLAN, "ports_commit": "e26b1e4"}, BLOB)
        with self.assertRaises(ValueError):
            pin_mirrors.mirror_entry("amd64", PLAN, {**BLOB, "object": "inputs/sha256/" + "0" * 64})

    def test_missing_lists_targets_without_a_mirror(self):
        pin = current_pin()
        for arch in pin_mirrors.ARCHES:
            pin["targets"][arch].pop("mirror", None)
        self.assertEqual(pin_mirrors.missing(pin), ["amd64", "arm64"])
        pin["targets"]["amd64"]["mirror"] = PINNED_BY_224
        self.assertEqual(pin_mirrors.missing(pin), ["arm64"])

    def test_apply_writes_a_valid_pin_and_leaves_the_input_alone(self):
        pin = current_pin()
        before = json.dumps(pin, sort_keys=True)
        updated = pin_mirrors.apply(pin, {"amd64": PINNED_BY_224}, now=inside(pin))
        self.assertEqual(updated["targets"]["amd64"]["mirror"], PINNED_BY_224)
        self.assertEqual(json.dumps(pin, sort_keys=True), before)

    def test_record_keeps_the_pin_file_format(self):
        pin = current_pin()
        with tempfile.TemporaryDirectory() as tmp:
            pin_path = Path(tmp) / "freebsd-16.json"
            pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            mirror = Path(tmp) / "mirror-amd64"
            mirror.mkdir()
            (mirror / "mirror-plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
            (mirror / "mirror-plan.blob.json").write_text(json.dumps(BLOB), encoding="utf-8")
            if datetime.now().astimezone() >= datetime.fromisoformat(pin["valid_until"].replace("Z", "+00:00")):
                self.skipTest("the checked-in pin has expired; apply() is covered with an explicit time")
            subprocess.run([sys.executable, str(ROOT / "scripts/pin_mirrors.py"), "--pin", str(pin_path),
                            "record", "--mirror", "amd64", str(mirror)], check=True)
            written = pin_path.read_text(encoding="utf-8")
            self.assertEqual(written, json.dumps(json.loads(written), indent=2, sort_keys=True) + "\n")
            self.assertEqual(json.loads(written)["targets"]["amd64"]["mirror"], PINNED_BY_224)


if __name__ == "__main__":
    unittest.main()
