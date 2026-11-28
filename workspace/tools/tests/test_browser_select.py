"""Check the browser scripts' dropdown helper installs its Playwright patches."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BrowserSelectTest(unittest.TestCase):
	def test_patches_are_installed(self):
		try:
			from playwright.sync_api import Locator, LocatorAssertions
		except ImportError:
			self.skipTest("Playwright is not installed")
		import browser_select
		self.assertEqual(Locator.select_option.__name__, "_select_option_for_dropdowns")
		self.assertEqual(LocatorAssertions.to_have_value.__name__, "_to_have_value_for_dropdowns")
		self.assertEqual(Locator.locator.__name__, "_locator_for_dropdowns")
		self.assertTrue(callable(browser_select.pick) and callable(browser_select.value_of))


if __name__ == "__main__":
	unittest.main()
