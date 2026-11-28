"""Make the container browser scripts work with the enhanced dropdowns (assets/shared/select.js)."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import importlib.util
from pathlib import Path
import sys

WEB_TESTS = Path(__file__).resolve().parents[1] / "freesense/tests/web"


def load_ui_helpers():
	"""Import the web tests' ui_helpers once; it patches select_option, to_have_value and locator("option") for enhanced selects."""
	if "ui_helpers" in sys.modules:
		return sys.modules["ui_helpers"]
	spec = importlib.util.spec_from_file_location("ui_helpers", WEB_TESTS / "ui_helpers.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules["ui_helpers"] = module
	spec.loader.exec_module(module)
	return module


helpers = load_ui_helpers()
pick, value_of, options_of, text_of = helpers.pick, helpers.value_of, helpers.options_of, helpers.text_of
