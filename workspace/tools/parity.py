"""Compare the old (1.2) form fields with the new form descriptors and write planning/page-parity/<area>.md.

Usage: python tools/parity.py [--all] [--root DIR]

Inputs: planning/inventory/old-fields.tsv (tools/old_forms.py), new-fields.tsv (tools/new_fields.py), page-audit.tsv and
planning/webui-doubts.md. An old page and a new form are joined by their legacy grant name (page-x), then a field by its
name, its config element (last config path segment), its normalised name or its label. By default only pages that the
audit lists as ported or partial are reported; --all adds the missing ones (which then have no new form).
A missing field that the audit or webui-doubts.md explains is flagged as an accepted drop with its doubt ID.
Requires Python 3.12 or later.
"""

import argparse
import csv
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIELD_TYPES = {"text", "checkbox", "select", "number", "password", "textarea", "radio", "multicheckbox", "select-multiple",
               "select+input", "email", "file", "tel", "url", "date", "time", "color", "range", "search"}
IGNORED_NAMES = {"submit", "save", "apply", "act", "id", "after", "if", "__csrf_magic", "displayrule", "retval", "referer"}
DOUBT_ID = re.compile(r"\b[A-Z]{1,2}\d?[a-z]?-\d+(?:-\d+)?\b")


def read_tsv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def normal(name: str) -> str:
    name = re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", name)
    name = re.sub(r"\d+$", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def words(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def doubt_index(path: Path) -> dict[str, str]:
    """Backticked token of a doubt row -> doubt ID."""
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not DOUBT_ID.fullmatch(cells[0]):
            continue
        for token in re.findall(r"`([\w\-.\[\]*{}$]+)`", " ".join(cells[1:4])):
            found.setdefault(token.lower(), cells[0])
    return found


def explanation(name: str, config: str, audit_row: dict, doubts: dict[str, str]) -> tuple[str, str]:
    """Return (kind, text) where kind is accepted, gap or unlisted."""
    keys = {name.lower(), config.rsplit("/", 1)[-1].lower()} - {""}
    for key in keys:
        if key in doubts:
            return "accepted", doubts[key]
    text = " ".join(audit_row.get(c, "") for c in ("missing", "covered", "changed_vs_audit1"))
    for key in keys:
        for sentence in re.split(r";\s*|\.\s+", text):
            if re.search(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", sentence, re.I):
                ids = DOUBT_ID.findall(sentence)
                if ids:
                    return "accepted", ids[0]
                return "gap", "audit: " + sentence.strip()[:90]
    return "unlisted", ""


def old_choices(text: str) -> list[str] | None:
    if not text or text.startswith("dynamic:") or text.startswith("text:") or text.startswith("columns:"):
        return None
    keys = []
    for item in text.split("; "):
        if item.startswith("(+dynamic)") or not item:
            continue
        keys.append(item.split("=", 1)[0])
    return keys


def new_choices(text: str) -> list[str] | None:
    if not text or "in all" in text:
        return None
    return [("" if c == "(empty)" else c) for c in text.split("; ")]


def match_fields(old: list[dict], new: list[dict]) -> tuple[dict[int, dict], list[dict]]:
    """Map index of an old field to its new field; returns (matches, unmatched new fields)."""
    matches: dict[int, dict] = {}
    used: set[int] = set()
    passes = [
        lambda o, n: o["field"] == n["field"].split("[]")[0] or o["field"] == n["field"],
        lambda o, n: o["config_path"].rsplit("/", 1)[-1] == n["field"].split(".")[-1] and o["config_path"] != "",
        lambda o, n: normal(o["field"]) == normal(n["field"]) or normal(o["field"]) == normal(n["field"].split(".")[-1]),
        lambda o, n: words(o["label"]) != "" and words(o["label"]) == words(n["label"]),
    ]
    for rule in passes:
        for i, o in enumerate(old):
            if i in matches or not o["field"]:
                continue
            for j, n in enumerate(new):
                if j not in used and rule(o, n):
                    matches[i] = n
                    used.add(j)
                    break
    return matches, [n for j, n in enumerate(new) if j not in used]


def compare(old_rows: list[dict], new_rows: list[dict], audit_row: dict, doubts: dict[str, str]) -> dict:
    fields = [r for r in old_rows if r["type"] in FIELD_TYPES and r["section"] != "(html)" or
              (r["type"] in FIELD_TYPES and r["field"].lower() not in IGNORED_NAMES and r["section"] == "(html)")]
    fields = [r for r in fields if r["field"].lower() not in IGNORED_NAMES]
    buttons = [r for r in old_rows if r["type"] in ("button", "link-button")]
    tables = [r for r in old_rows if r["type"] in ("table", "html") and r["field"] in ("(table)", "(html)")]
    tabs = [r for r in old_rows if r["type"] == "tabs"]
    new_fields = [r for r in new_rows if r["type"] not in ("revision",)]
    matches, extra = match_fields(fields, new_fields)
    result = {"fields": fields, "matched": [], "missing": [], "options": [], "labels": [], "defaults": [], "conditions": [],
              "extra": extra, "buttons": buttons, "tables": tables, "tabs": tabs, "order": ""}
    for i, row in enumerate(fields):
        if i not in matches:
            kind, text = explanation(row["field"], row["config_path"], audit_row, doubts)
            result["missing"].append((row, kind, text))
            continue
        new = matches[i]
        result["matched"].append((row, new))
        if row["label"] and new["label"] and words(row["label"]) != words(new["label"]):
            result["labels"].append((row, new))
        old_keys, new_keys = old_choices(row["choices"]), new_choices(new["choices"])
        if row["type"].startswith("select") and old_keys is not None and new_keys is not None:
            lost = [k for k in old_keys if k not in new_keys]
            added = [k for k in new_keys if k not in old_keys]
            if lost or added:
                result["options"].append((row, lost, added))
        if row["default"] and not new["default"] and row["type"] != "checkbox":
            result["defaults"].append((row, new))
        if "when" in row["depends_on"] and "visible when" not in new["conditions"]:
            result["conditions"].append((row, new))
    positions = [new_fields.index(m) for m in (matches[i] for i in sorted(matches))]
    result["order"] = "" if positions == sorted(positions) else "old order and new order differ"
    return result


def cell(text: str, limit: int = 110) -> str:
    text = re.sub(r"\s+", " ", text).strip().replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1] + "..."


def page_section(page: str, audit_row: dict, result: dict, new_forms: list[str]) -> list[str]:
    lines = [f"## {page.removeprefix('src/usr/local/www/')}", ""]
    lines.append(f"Menu: {audit_row.get('menu_title', '')}. Audit status: {audit_row.get('status', '')}. New forms: "
                 + (", ".join(sorted(set(new_forms))) or "none") + ".")
    lines.append("")
    accepted = [m for m in result["missing"] if m[1] == "accepted"]
    lines.append(f"Old fields {len(result['fields'])}, matched {len(result['matched'])}, missing {len(result['missing'])} "
                 f"(accepted drops {len(accepted)}), new-only {len(result['extra'])}.")
    lines.append("")
    if result["missing"]:
        lines += ["### Fields missing", "", "| Field | Label | Type | Section | Status |", "|---|---|---|---|---|"]
        for row, kind, text in result["missing"]:
            status = {"accepted": f"accepted drop {text}", "gap": text, "unlisted": "not listed in the audit"}[kind]
            lines.append(f"| `{cell(row['field'], 40)}` | {cell(row['label'], 50)} | {row['type']} | {cell(row['section'], 30)} | {cell(status)} |")
        lines.append("")
    if result["options"]:
        lines += ["### Options differ", "", "| Field | Missing in new | Only in new |", "|---|---|---|"]
        for row, lost, added in result["options"]:
            lines.append(f"| `{row['field']}` | {cell(', '.join(lost) or '-', 90)} | {cell(', '.join(added) or '-', 90)} |")
        lines.append("")
    if result["labels"]:
        lines += ["### Labels differ", "", "| Field | Old | New |", "|---|---|---|"]
        for row, new in result["labels"]:
            lines.append(f"| `{row['field']}` | {cell(row['label'], 60)} | {cell(new['label'], 60)} |")
        lines.append("")
    if result["order"]:
        lines += ["### Order", "", result["order"] + ".", ""]
    if result["defaults"]:
        lines += ["### Old defaults the descriptor does not declare", "", "| Field | Old default |", "|---|---|"]
        lines += [f"| `{row['field']}` | {cell(row['default'], 60)} |" for row, _ in result["defaults"]]
        lines.append("")
    if result["conditions"]:
        lines += ["### Conditional fields without a new condition", "", "| Field | Old condition |", "|---|---|"]
        for row, _ in result["conditions"]:
            condition = "; ".join(p for p in row["depends_on"].split("; ") if "when" in p)
            lines.append(f"| `{row['field']}` | {cell(condition, 120)} |")
        lines.append("")
    if result["extra"]:
        lines += ["### New fields without an old counterpart", "",
                  ", ".join(f"`{n['field']}`" for n in result["extra"][:40]) + (" ..." if len(result["extra"]) > 40 else ""), ""]
    if result["tabs"]:
        lines += ["### Tabs of the old page", "", cell(result["tabs"][0]["choices"], 400), ""]
    for table in result["tables"]:
        lines += ["### Table and actions of the old page", "", cell(table["choices"], 400), "", cell(table["help"], 600), ""]
    if result["buttons"]:
        lines += ["### Buttons", "", ", ".join(f"{cell(b['label'], 30)} (`{b['field']}`)" for b in result["buttons"][:30]), ""]
    return lines


def run(root: Path, include_all: bool = False) -> dict:
    inventory = root / "planning/inventory"
    audit = {r["path"]: r for r in read_tsv(inventory / "page-audit.tsv")}
    old = read_tsv(inventory / "old-fields.tsv")
    new = read_tsv(inventory / "new-fields.tsv")
    doubts = doubt_index(root / "planning/webui-doubts.md")
    new_by_grant: dict[str, list[dict]] = {}
    for row in new:
        if row["grant"]:
            new_by_grant.setdefault(row["grant"], []).append(row)
    old_by_page: dict[str, list[dict]] = {}
    for row in old:
        old_by_page.setdefault(row["old_file"], []).append(row)
    out_dir = root / "planning/page-parity"
    out_dir.mkdir(parents=True, exist_ok=True)
    areas: dict[str, list[str]] = {}
    summary = []
    for page, rows in sorted(old_by_page.items()):
        audit_row = audit.get(page, {})
        status = audit_row.get("status", "")
        if status not in ("ported", "partial") and not (include_all and status == "missing"):
            continue
        grant = rows[0]["grant"]
        seen, new_rows = set(), []
        for n in new_by_grant.get(grant, []):
            key = (n["field"], n["operation"].split(".")[0])
            # an Edit form repeats the Create fields; keep one row per field name
            if n["field"] not in seen:
                seen.add(n["field"])
                new_rows.append(n)
        result = compare(rows, new_rows, audit_row, doubts)
        area = audit_row.get("area", "Other")
        forms = [n["operation"] for n in new_by_grant.get(grant, [])]
        areas.setdefault(area, []).extend(page_section(page, audit_row, result, forms))
        counts = {"accepted": 0, "gap": 0, "unlisted": 0}
        for _, kind, _ in result["missing"]:
            counts[kind] += 1
        summary.append({"page": page, "area": area, "status": status, "old_fields": len(result["fields"]),
                        "matched": len(result["matched"]), "missing": len(result["missing"]), "accepted": counts["accepted"],
                        "gap": counts["gap"], "unlisted": counts["unlisted"], "options_differ": len(result["options"]),
                        "labels_differ": len(result["labels"]), "new_only": len(result["extra"])})
    for area, body in areas.items():
        rows_area = [s for s in summary if s["area"] == area]
        head = [f"# Page parity: {area}", "",
                "Generated by `tools/parity.py` from `planning/inventory/old-fields.tsv` and `new-fields.tsv`. "
                "Do not edit by hand; regenerate. A drop is accepted only when it cites a `webui-doubts.md` ID.", "",
                "| Page | Status | Old fields | Matched | Missing | Accepted | Options differ |", "|---|---|---:|---:|---:|---:|---:|"]
        head += [f"| {s['page'].removeprefix('src/usr/local/www/')} | {s['status']} | {s['old_fields']} | {s['matched']} | "
                 f"{s['missing']} | {s['accepted']} | {s['options_differ']} |" for s in rows_area]
        (out_dir / f"{area.lower()}.md").write_text("\n".join(head + [""] + body) + "\n", encoding="utf-8", newline="\n")
    columns = ["page", "area", "status", "old_fields", "matched", "missing", "accepted", "gap", "unlisted", "options_differ",
               "labels_differ", "new_only"]
    with (out_dir / "summary.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([[s[c] for c in columns] for s in summary])
    return {"pages": len(summary), "areas": sorted(areas)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--all", action="store_true", help="include pages the audit lists as missing")
    args = parser.parse_args()
    print(run(args.root, args.all))


if __name__ == "__main__":
    main()
