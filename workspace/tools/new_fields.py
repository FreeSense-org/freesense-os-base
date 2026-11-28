"""Dump every new form descriptor (lib/Http *Form enums) to planning/inventory/new-fields.tsv.

Usage: python tools/new_fields.py [--json FILE]

The schemas come from new_fields.php, run in the php:8.6.0-cli image with networking disabled (as tools/check.py does).
--json reads a saved dump instead of starting Docker. The section of a field is read from the page's JavaScript module
(`{title: '...', fields: [...]}`), its condition from `visibleWhen`. The config path is the field name, because a form
field is named after its configuration element. Requires Python 3.12 or later.
"""

import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "freesense/src"
OUT = ROOT / "planning/inventory/new-fields.tsv"
PHP_IMAGE = "php:8.6.0-cli"
COLUMNS = ["page", "operation", "section", "field", "label", "type", "default", "choices", "conditions", "config_path",
           "permission", "grant", "required"]
CHOICE_LIMIT = 40
SKIP_TYPES = {"revision"}


def dump(src: Path) -> list[dict]:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("docker is required to run the descriptors under PHP 8.6")
    result = subprocess.run(
        [docker, "run", "--rm", "--pull=never", "--network", "none", "-v", f"{src}:/src:ro",
         "-v", f"{Path(__file__).resolve().parent / 'new_fields.php'}:/dump.php:ro", PHP_IMAGE, "php", "/dump.php", "/src"],
        check=True, capture_output=True, text=True, env=None)
    return json.loads(result.stdout)


def grants(src: Path) -> dict[str, str]:
    """Map ApiPermission case name to the legacy page grant it requires."""
    text = (src / "usr/local/FreeSense/lib/Auth/ApiPermission.php").read_text(encoding="utf-8")
    block = text[text.index("$pageGrant = match"):]
    block = block[:block.index("};")]
    found = {}
    for names, grant in re.findall(r"((?:self::\w+,?\s*)+)=>\s*'([\w-]+)'", block):
        for name in re.findall(r"self::(\w+)", names):
            found[name] = grant
    return found


def module_text(src: Path, value: str) -> str:
    root = src / "usr/local/www/assets"
    for file in sorted(root.rglob("*.js")):
        if file.parent.name == "shared":
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        if f"'{value}'" in text or f'"{value}"' in text:
            return text
    return ""


def js_sections(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for title, names in re.findall(r"\{\s*title:\s*['\"]([^'\"]*)['\"]\s*,\s*fields:\s*\[([^\]]*)\]", text):
        for name in re.findall(r"['\"]([\w\-.]+)['\"]", names):
            mapping.setdefault(name, title)
    return mapping


def render_choices(spec: dict) -> str:
    choices = spec.get("choices")
    if not isinstance(choices, list):
        return ""
    items = [str(c) if c != "" else "(empty)" for c in choices]
    if len(items) > CHOICE_LIMIT:
        return "; ".join(items[:5]) + f"; ... {len(items)} in all"
    return "; ".join(items)


def conditions(spec: dict, name: str, visible: dict[str, str]) -> str:
    parts = []
    if name in visible:
        parts.append(f"visible when {visible[name]} checked")
    for key in ("min", "max", "minBytes", "maxBytes", "format", "unique", "minItems", "maxItems"):
        if key in spec and key in ("min", "max", "maxItems"):
            parts.append(f"{key}={spec[key]}")
    if spec.get("format"):
        parts.append(f"format={spec['format']}")
    return "; ".join(parts)


def flatten(fields: dict, prefix: str = ""):
    if not isinstance(fields, dict):
        return
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            continue
        full = prefix + name
        if spec.get("type") in SKIP_TYPES:
            continue
        yield full, spec
        items = spec.get("items")
        if isinstance(items, dict) and isinstance(items.get("fields"), dict):
            yield from flatten(items["fields"], full + "[].")
        elif isinstance(spec.get("fields"), dict):
            yield from flatten(spec["fields"], full + ".")


def rows_from(forms: list[dict], src: Path) -> list[dict]:
    granted = grants(src)
    rows = []
    for form in forms:
        schema = form.get("schema")
        if not schema:
            continue
        page = form.get("path", "").removeprefix("/api/v1")
        text = module_text(src, form.get("value", ""))
        sections = js_sections(text)
        visible = dict(re.findall(r"(?:name|field):\s*['\"]([\w\-.]+)['\"][^}]*?visibleWhen:\s*['\"]([\w\-.]+)['\"]", text))
        permission = form.get("permission", "")
        for name, spec in flatten(schema.get("fields", {})):
            top = name.split("[")[0].split(".")[0]
            rows.append({
                "page": page, "operation": f"{form['class']}.{form['case']}", "section": sections.get(top, ""),
                "field": name, "label": spec.get("label", ""), "type": spec.get("type", ""),
                "default": str(spec["default"]) if "default" in spec else "", "choices": render_choices(spec),
                "conditions": conditions(spec, top, visible), "config_path": name,
                "permission": permission, "grant": granted.get(permission, ""), "required": "yes" if spec.get("required") else "",
            })
    return rows


def write(rows: list[dict], out: Path) -> None:
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([re.sub(r"\s+", " ", str(row.get(c, ""))).strip() for c in COLUMNS])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=SRC)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    forms = json.loads(args.json.read_text(encoding="utf-8")) if args.json else dump(args.src)
    rows = rows_from(forms, args.src)
    write(rows, args.out)
    print({"forms": len(forms), "rows": len(rows)})


if __name__ == "__main__":
    main()
