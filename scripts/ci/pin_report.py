#!/usr/bin/env python3
"""Report how far the pins in build/lock.json are behind. It changes nothing: a pin moves only by a reviewed commit.

usage: pin_report.py build/lock.json
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


def remote_head(url: str) -> str:
    result = subprocess.run(["git", "ls-remote", url, "HEAD"], check=True, capture_output=True, text=True, timeout=120)
    return result.stdout.split()[0]


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def report(lock: dict, head=remote_head, available=reachable) -> list[str]:
    lines = ["| Pin | Locked | Upstream |", "| --- | --- | --- |"]
    sources = {"FreeBSD source": lock["freebsd_source"],
               "intel-ipsec-mb": lock.get("kernel_contrib", {}).get("intel-ipsec-mb")}
    for name, entry in sources.items():
        if not entry:
            continue
        try:
            newest = head(entry["url"])
        except (OSError, subprocess.SubprocessError, IndexError):
            lines.append(f"| {name} | {entry['commit'][:12]} | not reachable |")
            continue
        state = "same" if newest == entry["commit"] else f"{newest[:12]} (newer default branch head)"
        lines.append(f"| {name} | {entry['commit'][:12]} | {state} |")
    downloads = {"builder image": lock.get("builder_image", {}).get("url")}
    for name, entry in lock.get("kernel_contrib", {}).items():
        if "urls" in entry:
            downloads[name] = entry["urls"][0]
    for name, url in downloads.items():
        if url:
            state = "still downloadable" if available(url) else "GONE: archive it or move the pin"
            lines.append(f"| {name} | {url.rsplit('/', 1)[-1]} | {state} |")
    return lines


if __name__ == "__main__":
    print("## Pins", *report(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))), sep="\n")
