import base64
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extract_serial_payload", ROOT / "scripts" / "extract_serial_payload.py")
module = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(module)


class ExtractTests(unittest.TestCase):
    def test_extracts_exact_json_payload(self):
        raw = base64.b64encode(b'{"b":2,"a":1}').decode()
        self.assertEqual(json.loads(module.extract(f"noise\nPIN_REQUIREMENTS_BEGIN\n{raw}\nPIN_REQUIREMENTS_END\n", "PIN_REQUIREMENTS")), {"a": 1, "b": 2})

    def test_rejects_missing_duplicate_and_malformed_payloads(self):
        block = "PIN_REQUIREMENTS_BEGIN\ne30=\nPIN_REQUIREMENTS_END\n"
        for value in ("", block + block, "PIN_REQUIREMENTS_BEGIN\n!\nPIN_REQUIREMENTS_END\n"):
            with self.assertRaises(ValueError): module.extract(value, "PIN_REQUIREMENTS")


if __name__ == "__main__": unittest.main()
