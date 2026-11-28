"""Validate root-relative links in a generated site without network access."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value and value.startswith("/") and not value.startswith("//"):
                self.paths.append(urlsplit(value).path)


def redirect_sources(path: Path | None) -> list[str]:
    if path is None:
        return []
    return [line.split()[0] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def resolves(site: Path, url: str, redirects: list[str]) -> bool:
    decoded = unquote(url, errors="strict")
    if "\\" in decoded or "\x00" in decoded or ".." in decoded.split("/"):
        return False
    target = (site / decoded.lstrip("/")).resolve()
    if not target.is_relative_to(site):
        return False
    if target.is_file():
        return True
    index = (target / "index.html").resolve()
    if index.is_relative_to(site) and index.is_file():
        return True
    return any(url.startswith(source[:-1]) if source.endswith("*")
               else source in {url, url + "/"} for source in redirects)


def handler_urls(functions: Path | None) -> set[str]:
    if functions is None:
        return set()
    if not functions.is_dir():
        raise ValueError(f"Missing Pages Functions directory: {functions}")
    # A concrete function file registers an exact path. Parameterized routes
    # need their own routing fixtures before the validator can credit them.
    paths = set()
    for source in functions.rglob("*"):
        if source.is_file() and source.suffix in {".js", ".ts"}:
            parts = source.relative_to(functions).with_suffix("").parts
            if all("[" not in part and not part.startswith("_") for part in parts):
                route = "/" + "/".join(parts[:-1] if parts[-1] == "index" else parts)
                paths.add(route)
    return paths


def check_site(site: Path, redirects: list[str], handlers: set[str] | None = None) -> tuple[int, set[str]]:
    site = site.resolve(strict=True)
    if not site.is_dir():
        raise ValueError(f"Expected a generated site directory: {site}")
    checked = 0
    errors = set()
    seen = {}
    pages = sorted(site.rglob("*.html"))
    if not pages:
        raise ValueError(f"No HTML pages under {site}")
    for page in pages:
        if not page.resolve().is_relative_to(site):
            raise ValueError(f"Page outside site root: {page}")
        parser = PageLinks()
        parser.feed(page.read_text(encoding="utf-8"))
        parser.close()
        for url in parser.paths:
            checked += 1
            if url not in seen:
                seen[url] = (handlers is not None and url in handlers) or resolves(site, url, redirects)
            if not seen[url]:
                errors.add(f"{page.relative_to(site).as_posix()}: {url}")
    return checked, errors


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("site", type=Path, help="generated site directory")
    cli.add_argument("--redirects", type=Path, help="Cloudflare Pages _redirects file")
    cli.add_argument("--functions", type=Path, help="directory containing concrete Pages Function routes")
    args = cli.parse_args()
    checked, errors = check_site(args.site, redirect_sources(args.redirects), handler_urls(args.functions))
    if errors:
        for error in sorted(errors):
            print(error, file=sys.stderr)
        return 1
    print(f"Internal links valid: {checked} links checked.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit(f"Site check failed: {error}")
