"""Render the FreeSense boot images (loader splash, menu logo and brand) from the branding SVGs.

Usage: python boot/make_boot_images.py <branding-dir> <output-dir>

Needs Playwright with Chromium (the SVG embeds its font). Output, transparent PNGs for a dark console:
  freesense-logo-rev.png           monogram, shown by the EFI loader as the boot and shutdown splash and in the menu
  freesense-brand-rev.png          lockup (monogram and wordmark) for the loader menu
  freesense-install-brand-rev.png  lockup for the installer
The installed copies live in /boot/images (see patches/0014).
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

IMAGES = (
    # (output, source svg, source width, source height, scale)
    ("freesense-logo-rev.png", "monogram-icon-reversed.svg", 134, 120, 2.0),
    ("freesense-brand-rev.png", "monogram-lockup-dark.svg", 425, 120, 1.2),
    ("freesense-install-brand-rev.png", "monogram-lockup-dark.svg", 425, 120, 1.2),
)


def main(branding: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for name, svg, w, h, scale in IMAGES:
            width, height = round(w * scale), round(h * scale)
            page = browser.new_page(viewport={"width": width, "height": height})
            data = (branding / svg).read_text(encoding="utf-8")
            page.set_content(
                f"<html><body style='margin:0;background:transparent'>"
                f"<div style='width:{width}px;height:{height}px'>"
                f"{data.replace('width=\"%d\" height=\"%d\"' % (w, h), 'width=\"100%\" height=\"100%\"')}</div></body></html>"
            )
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / name), omit_background=True)
            print(f"{name}: {width}x{height}")
            page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
