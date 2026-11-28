"""Check the appliance's public document root in source or a package STAGEDIR."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from pathlib import Path, PurePosixPath
from html.parser import HTMLParser
import re
import sys


DOCUMENT_ROOT = PurePosixPath("/usr/local/www")
PHP_NAME = re.compile(r"(?:^|\.)(?:php[0-9]*|phps|phtml|pht|phar|inc)(?:\.|$)", re.IGNORECASE)
PHP_TAG = re.compile(rb"<\?(?:php(?=[\s/]|$)|=)", re.IGNORECASE)
PHP_SHEBANG = re.compile(rb"^#![^\n]*\bphp(?:[0-9.]*)\b", re.IGNORECASE)
BROWSER_TRANSPORT = re.compile(r"\b(?:fetch\s*\(|new\s+XMLHttpRequest\b|navigator\s*\.\s*sendBeacon\s*\()")


def check_name(installed: str) -> None:
    path = PurePosixPath(installed)
    if path.is_relative_to(DOCUMENT_ROOT) and PHP_NAME.search(path.name):
        raise ValueError(f"PHP file under the public document root: {installed}")


def check_asset_location(path: PurePosixPath, *, optional: bool | None) -> None:
    if not path.is_relative_to(DOCUMENT_ROOT) or path.suffix.lower() not in {".js", ".mjs", ".css"}:
        return
    parts = path.relative_to(DOCUMENT_ROOT).parts
    if len(parts) < 3 or parts[0] != "assets":
        raise ValueError(f"Place browser code and styles in assets/<area>/: {path}")
    if (optional or parts[1] == "packages") and (len(parts) < 4 or parts[1] != "packages"):
        raise ValueError(f"Place Optional Package assets in assets/packages/<package-id>/: {path}")
    if optional is False and parts[1] == "packages":
        raise ValueError(f"System assets must use a System feature or shared folder: {path}")


def check_stage(root: Path, *, optional: bool | None = None) -> None:
    public = root.joinpath(*DOCUMENT_ROOT.parts[1:])
    if public.is_symlink() or public.is_junction():
        raise ValueError(f"Public document root is a link: {public}")
    if not public.exists():
        return
    for parent, directories, files in public.walk(on_error=_raise):
        for name in directories + files:
            path = parent / name
            installed = "/" + path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"Unresolved link under the public document root: {installed}")
            if name in directories:
                continue
            check_name(installed)
            check_asset_location(PurePosixPath(installed), optional=optional)
            if path.suffix.lower() in {".js", ".mjs", ".html", ".htm"}:
                check_browser_asset(path, shared_client=installed == "/usr/local/www/assets/shared/commands.js")
            with path.open("rb") as source:
                first = source.read(65536)
                if PHP_SHEBANG.search(first):
                    raise ValueError(f"PHP interpreter under the public document root: {installed}")
                if path.suffix.lower() == ".woff2" and first.startswith(b"wOF2"):
                    continue  # Compressed font data can contain any byte pair, including "<?=", and is only ever served as font/woff2.
                chunk = first
                tail = b""
                while chunk:
                    data = tail + chunk
                    if PHP_TAG.search(data):
                        raise ValueError(f"PHP code under the public document root: {installed}")
                    tail = data[-8:]
                    chunk = source.read(65536)
    # Private templates are served by the front controller and obey the same form contract.
    templates = root / "usr/local/FreeSense/web"
    if templates.is_dir():
        for template in templates.rglob("*.html"):
            check_browser_asset(template)


def _raise(error: OSError) -> None:
    raise error


class PackageMarkup(HTMLParser):
    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if ((tag == "form" and ("action" in fields or str(fields.get("method")).lower() == "post"))
                or fields.keys() & {"formaction", "formmethod", "usepost"}):
            raise ValueError("Package form bypasses the shared management router")


def check_browser_asset(path: Path, *, shared_client: bool = False) -> None:
    code = path.read_text(encoding="utf-8")
    if re.search(r"\.\s*(?:submit|requestSubmit)\s*\(", code):
        raise ValueError(f"{path}: native browser submission bypasses the management router")
    if not shared_client and BROWSER_TRANSPORT.search(code):
        raise ValueError(f"{path}: send management requests through the shared command client")
    if path.suffix.lower() in {".html", ".htm"}:
        parser = PackageMarkup()
        parser.feed(code)
        parser.close()


def check_package(port: Path) -> None:
    files = port / "files"
    check_stage(files, optional=True)
    for path in files.rglob("*") if files.is_dir() else ():
        if path.suffix.lower() not in {".php", ".inc", ".phtml", ".html", ".htm", ".js", ".mjs"}:
            continue
        content = path.read_text(encoding="utf-8")
        try:
            if path.suffix.lower() in {".js", ".mjs", ".html", ".htm"}:
                check_browser_asset(path)
            if re.search(r"\$_(?:POST|FILES)\b|\.\s*(?:submit|requestSubmit)\s*\(", content):
                raise ValueError("Package submission bypasses the shared management router")
            parser = PackageMarkup()
            parser.feed(content)
            parser.close()
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        sys.exit("Usage: python public_php.py STAGEDIR")
    try:
        check_stage(Path(sys.argv[1]))
    except (OSError, ValueError) as error:
        sys.exit(str(error))
