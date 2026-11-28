"""Statically extract the form fields of the pinned Development 1.2 WebUI pages.

Usage: python tools/old_forms.py [--old DIR] [--audit FILE] [--out FILE]

Every page listed in planning/inventory/page-audit.tsv is read as text (never executed) and described by rows of
planning/inventory/old-fields.tsv. A page built from Form_* helpers yields one row per field. A page that builds HTML
by hand yields one 'table' (or 'html') row listing the column headers and the actions, plus one row per literal
<input>/<select>/<textarea>. Pages whose fields or choices are only known at run time are listed in
planning/inventory/old-fields-dynamic.tsv for a rendering fallback. Requires Python 3.12 or later.
"""

import argparse
import csv
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT.parent / "active/freesense/src"
AUDIT = ROOT / "planning/inventory/page-audit.tsv"
OUT = ROOT / "planning/inventory/old-fields.tsv"
DYNAMIC = ROOT / "planning/inventory/old-fields-dynamic.tsv"
COLUMNS = ["old_file", "tab", "section", "field", "label", "type", "default", "choices", "help", "depends_on",
           "config_path", "repeat", "grant", "title"]

FIELD_CLASSES = {"Input", "IpAddress", "Select", "Checkbox", "Textarea", "StaticText", "Button", "MultiCheckbox",
                 "SelectInputCombo"}
HIDERS = ("hideInput", "hideCheckbox", "hideSelect", "hideGroupInput", "hideIpAddress", "hideLabel", "disableInput")
UNKNOWN = object()


# ------------------------------------------------------------------ lexical helpers

def mask_comments(source: str) -> str:
    """Blank PHP comments (same length) without touching HTML or strings."""
    out = list(source)
    i, n, php = 0, len(source), False
    while i < n:
        if not php:
            m = re.compile(r"<\?(?:php\b|=)?").search(source, i)
            if not m:
                break
            i, php = m.end(), True
            continue
        c = source[i]
        if source.startswith("?>", i):
            i, php = i + 2, False
        elif c in "'\"":
            i = skip_string(source, i)
        elif source.startswith("/*", i) or source.startswith("//", i) or (c == "#" and not source.startswith("#[", i)):
            if source.startswith("/*", i):
                end = source.find("*/", i + 2)
                end = n if end < 0 else end + 2
            else:
                end = i
                while end < n and source[end] != "\n" and not source.startswith("?>", end):
                    end += 1
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
        else:
            i += 1
    return "".join(out)


def skip_string(text: str, i: int) -> int:
    """Return the index after the quoted string that starts at i."""
    quote = text[i]
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return len(text)


def match_close(text: str, i: int) -> int:
    """Index of the bracket closing the one at text[i], string aware; len(text) when unbalanced."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[text[i]]]
    i += 1
    while i < len(text) and stack:
        c = text[i]
        if c in "'\"":
            i = skip_string(text, i)
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c == stack[-1]:
            stack.pop()
        i += 1
    return i - 1 if not stack else len(text)


def split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "'\"":
            i = skip_string(text, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            if sep != "." or not (text[i - 1:i].isdigit() and text[i + 1:i + 2].isdigit()):
                parts.append(text[start:i])
                start = i + len(sep)
                i += len(sep)
                continue
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def call_args(text: str, open_paren: int) -> tuple[list[str], int]:
    end = match_close(text, open_paren)
    inner = text[open_paren + 1:end]
    return ([a for a in split_top(inner)] if inner.strip() else []), end + 1


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\t", " ")).strip()


# ------------------------------------------------------------------ expression decoding

def unquote(token: str) -> str | None:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        body = token[1:-1]
        if token[0] == "'":
            return body.replace("\\'", "'").replace("\\\\", "\\")
        return re.sub(r"\\(.)", lambda m: {"n": " ", "t": " "}.get(m.group(1), m.group(1)), body)
    return None


def decode(expr: str | None) -> str:
    """Render a PHP expression as display text; unresolved parts stay visible as {expr}."""
    if expr is None:
        return ""
    expr = expr.strip()
    if not expr or expr.lower() == "null":
        return ""
    parts = split_top(expr, ".")
    if len(parts) > 1:
        return "".join(decode(p) for p in parts)
    if expr.startswith("(") and match_close(expr, 0) == len(expr) - 1:
        return decode(expr[1:-1])
    literal = unquote(expr)
    if literal is not None:
        return literal
    m = re.fullmatch(r"(?:gettext|_|htmlspecialchars|sprintf|strtoupper|ucfirst)\s*\((.*)\)", expr, re.S)
    if m:
        first = split_top(m.group(1))[0]
        return decode(first)
    if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false", expr, re.I):
        return expr
    return "{" + clean(expr) + "}"


def plain(text: str) -> str:
    text = re.sub(r"%\d+\$s|%s", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean(text.replace("&nbsp;", " ").replace("&amp;", "&"))


def literal_array(expr: str) -> list[tuple[str, str]] | None:
    expr = expr.strip()
    m = re.fullmatch(r"(?:array\s*\((.*)\)|\[(.*)\])", expr, re.S)
    if not m:
        return None
    inner = m.group(1) if m.group(1) is not None else m.group(2)
    items = []
    for number, piece in enumerate(split_top(inner)):
        if not piece:
            continue
        pair = split_top(piece, "=>")
        if len(pair) == 2:
            items.append((decode(pair[0]), decode(pair[1])))
        else:
            items.append((str(number), decode(pair[0])))
    return items


# ------------------------------------------------------------------ page model

class Page:
    def __init__(self, relative: str, source: str, old_root: Path):
        self.relative = relative
        self.raw = source
        self.text = mask_comments(source)
        self.old_root = old_root
        self.name = Path(relative).name
        self.dynamic: list[str] = []
        self.includes = self.included_text()
        self.pconfig = self.pconfig_map(self.text + "\n" + self.includes)
        self.assignments = self.variable_assignments()

    # -- included code that fills $pconfig
    def included_text(self) -> str:
        pieces = []
        for name in re.findall(r"(?:require_once|require|include_once|include)\s*\(?\s*[\"']([\w./-]+\.inc)[\"']", self.text):
            for base in (self.old_root / "usr/local/www", self.old_root / "etc/inc", self.old_root / "usr/local/www/includes",
                         self.old_root / "usr/local/FreeSense/include/www", self.old_root / "usr/local/FreeSense/include"):
                candidate = base / name
                if candidate.is_file():
                    pieces.append(mask_comments(candidate.read_text(encoding="utf-8", errors="replace")))
                    break
        return "\n".join(pieces)

    @staticmethod
    def pconfig_map(text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for m in re.finditer(r"\$pconfig\[\s*(['\"])([\w\-.]+)\1\s*\]\s*=(?!=)\s*([^;]+);", text):
            found.setdefault(m.group(2), clean(m.group(3)))
        return found

    def variable_assignments(self) -> dict[str, list[tuple[int, str]]]:
        out: dict[str, list[tuple[int, str]]] = {}
        for m in re.finditer(r"\$(\w+)\s*=\s*(array\s*\(|\[)", self.text):
            open_at = m.end() - 1
            end = match_close(self.text, open_at)
            out.setdefault(m.group(1), []).append((m.start(), self.text[m.end() - len(m.group(2)):end + 1]))
        return out

    # -- value classification
    def config_of(self, expr: str) -> str:
        expr = expr or ""
        m = re.search(r"\$pconfig\[\s*['\"]([\w\-.]+)['\"]\s*\]", expr)
        if m:
            return self.resolve_pconfig(m.group(1))
        return self.path_from(expr)

    def resolve_pconfig(self, key: str) -> str:
        rhs = self.pconfig.get(key, "")
        return self.path_from(rhs)

    @staticmethod
    def path_from(expr: str) -> str:
        m = re.search(r"(?:config_get_path|array_get_path\s*\(\s*\$config\s*,)\s*\(?\s*['\"]([^'\"]+)['\"]", expr)
        if m:
            return m.group(1)
        m = re.search(r"config_path_enabled\s*\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?", expr)
        if m:
            return m.group(1) + ("/" + m.group(2) if m.group(2) else "")
        m = re.search(r"\$config((?:\[\s*['\"][\w\-.]+['\"]\s*\])+)", expr)
        if m:
            return "/".join(re.findall(r"['\"]([\w\-.]+)['\"]", m.group(1)))
        m = re.search(r"\$a_\w+\[\$\w+\]((?:\[\s*['\"][\w\-.]+['\"]\s*\])+)", expr)
        if m:
            return "item/" + "/".join(re.findall(r"['\"]([\w\-.]+)['\"]", m.group(1)))
        return ""

    def default_of(self, expr: str) -> str:
        expr = (expr or "").strip()
        if not expr or expr.lower() == "null":
            return ""
        m = re.search(r"\$pconfig\[\s*['\"]([\w\-.]+)['\"]\s*\]", expr)
        if m:
            rhs = self.pconfig.get(m.group(1), "")
            literal = unquote(rhs) if rhs else None
            if literal is None and re.fullmatch(r"-?\d+", rhs or ""):
                literal = rhs
            fallback = re.search(r"\?\?\s*(['\"][^'\"]*['\"]|-?\d+)\s*$|:\s*(['\"][^'\"]*['\"]|-?\d+)\s*$", rhs or "")
            if literal is None and fallback:
                literal = decode(fallback.group(1) or fallback.group(2))
            return literal or ""
        literal = unquote(expr)
        if literal is not None:
            return literal
        if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false", expr, re.I):
            return expr
        return ""

    def choices_of(self, expr: str, position: int) -> str:
        expr = expr.strip()
        items = literal_array(expr)
        if items is None:
            m = re.fullmatch(r"\$(\w+)", expr)
            if m:
                known = [a for a in self.assignments.get(m.group(1), []) if a[0] < position]
                if known:
                    items = literal_array(known[-1][1])
                    extra = re.findall(r"\$%s\s*(?:\[[^\]]*\])\s*=" % re.escape(m.group(1)), self.text[:position])
                    if extra:
                        self.dynamic.append(f"choices of ${m.group(1)} are extended at run time")
                        return render_choices(items or []) + "; (+dynamic)"
        if items is None:
            if expr and expr.lower() != "null":
                self.dynamic.append(f"choices {clean(expr)[:60]}")
            return "dynamic:" + clean(expr)[:80] if expr else ""
        return render_choices(items)

    # -- tabs
    def tabs(self) -> tuple[str, list[tuple[str, str]]]:
        pairs: list[tuple[str, str]] = []
        active = ""
        for m in re.finditer(r"fs_tabs\(\s*['\"]([\w-]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", self.text):
            group, url = m.groups()
            registry = tab_registry(self.old_root).get(group, [])
            pairs = [(label, link) for label, link in registry]
            for label, link in registry:
                if link == url:
                    active = label
        if not pairs:
            for m in re.finditer(r"\$tab_array\[\]\s*=\s*(?:array\s*\(|\[)", self.text):
                args, _ = call_args(self.text, m.end() - 1) if self.text[m.end() - 1] == "(" else bracket_args(self.text, m.end() - 1)
                if len(args) >= 3:
                    label, link = plain(decode(args[0])), decode(args[2])
                    pairs.append((label, link))
                    if args[1].strip().lower() == "true":
                        active = label
            if pairs and not active:
                for label, link in pairs:
                    if self.name in link:
                        active = label
                        break
        return active, pairs

    def loop_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        text = self.text
        for m in re.finditer(r"\b(foreach|while|for)\s*\(", text):
            open_at = m.end() - 1
            close = match_close(text, open_at)
            j = close + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text):
                continue
            if text[j] == "{":
                ranges.append((m.start(), match_close(text, j)))
            elif text[j] == ":":
                depth, k = 1, j
                pattern = re.compile(r"\b(foreach|while|for)\s*\(|\bend(foreach|while|for)\b")
                for e in pattern.finditer(text, j):
                    depth += 1 if e.group(1) else -1
                    if depth == 0:
                        k = e.end()
                        break
                else:
                    k = len(text)
                ranges.append((m.start(), k))
            else:
                end = text.find(";", j)
                ranges.append((m.start(), len(text) if end < 0 else end))
        return ranges


def bracket_args(text: str, open_bracket: int) -> tuple[list[str], int]:
    end = match_close(text, open_bracket)
    inner = text[open_bracket + 1:end]
    return ([a for a in split_top(inner)] if inner.strip() else []), end + 1


def render_choices(items: list[tuple[str, str]]) -> str:
    return "; ".join(f"{key}={label}" if key != label else label for key, label in items)


_TABS: dict[Path, dict[str, list[tuple[str, str]]]] = {}


def tab_registry(old_root: Path) -> dict[str, list[tuple[str, str]]]:
    if old_root not in _TABS:
        groups: dict[str, list[tuple[str, str]]] = {}
        for file in sorted((old_root / "usr/local/www/includes/tabs").glob("*.inc")):
            text = mask_comments(file.read_text(encoding="utf-8", errors="replace"))
            for m in re.finditer(r"\$fs_tab_groups\[\s*['\"]([\w-]+)['\"]\s*\]\s*=\s*(?:array\s*\(|\[)", text):
                open_at = m.end() - 1
                end = match_close(text, open_at)
                body = text[open_at + 1:end]
                labels = []
                for tab in re.finditer(r"(?:array\s*\(|\[)\s*(?:gettext\(\s*)?(['\"])(.*?)\1\s*\)?\s*,\s*(['\"])([^'\"]+)\3", body):
                    labels.append((plain(tab.group(2)), tab.group(4)))
                groups[m.group(1)] = labels
        _TABS[old_root] = groups
    return _TABS[old_root]


# ------------------------------------------------------------------ script conditions

def script_conditions(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (id -> condition, css class -> condition) read from inline <script> handlers."""
    ids: dict[str, list[str]] = {}
    classes: dict[str, list[str]] = {}
    for block in re.findall(r"<script\b[^>]*>(.*?)</script>", raw, re.S | re.I):
        code = re.sub(r"//[^\n]*", "", block)
        for m in re.finditer(r"\b(hideClass|hideMultiClass|%s)\s*\(" % "|".join(HIDERS), code):
            name = m.group(1)
            args, _ = call_args(code, m.end() - 1)
            if len(args) < 2:
                continue
            target = unquote(args[0])
            if target is None:
                target = "{" + clean(args[0]) + "}"
            expr = condition_text(args[1])
            action = "disabled" if name == "disableInput" else "hidden"
            entry = f"{action} when {expr}"
            (classes if name.startswith("hideClass") or name == "hideMultiClass" else ids).setdefault(target, [])
            bucket = classes if name in ("hideClass", "hideMultiClass") else ids
            if entry not in bucket[target]:
                bucket[target].append(entry)
    return ({k: " | ".join(v) for k, v in ids.items()}, {k: " | ".join(v) for k, v in classes.items()})


def condition_text(expr: str) -> str:
    expr = clean(expr)
    expr = re.sub(r"\$\(\s*['\"]#([\w\-]+)['\"]\s*\)\.prop\(\s*['\"](checked)['\"]\s*\)", r"#\1 checked", expr)
    expr = re.sub(r"\$\(\s*['\"]#([\w\-]+)['\"]\s*\)\.(?:val\(\)|prop\(\s*['\"]value['\"]\s*\))", r"#\1", expr)
    expr = re.sub(r"\$\(\s*['\"]#([\w\-]+)['\"]\s*\)\.is\(\s*['\"]:checked['\"]\s*\)", r"#\1 checked", expr)
    expr = re.sub(r"\$\(this\)\.prop\(\s*['\"]checked['\"]\s*\)", "this checked", expr)
    return expr[:140]


# ------------------------------------------------------------------ extraction

def grant_of(raw: str) -> str:
    m = re.search(r"##\|\*IDENT=(\S+)", raw)
    return m.group(1) if m else ""


def title_of(page: Page) -> str:
    m = re.search(r"\$pgtitle\s*=\s*(?:array\s*\(|\[)", page.text)
    if not m:
        return ""
    args, _ = bracket_args(page.text, m.end() - 1) if page.text[m.end() - 1] == "[" else call_args(page.text, m.end() - 1)
    return " / ".join(plain(decode(a)) for a in args)


def chain_after(text: str, index: int, wrapped: bool) -> tuple[list[tuple[str, list[str]]], int]:
    """Parse ->method(args) calls that follow a constructor call ending at index."""
    j = index
    skip = lambda k: len(text[k:]) - len(text[k:].lstrip())
    if wrapped:
        j += skip(j)
        if text[j:j + 1] == ")":
            j += 1
    calls = []
    while True:
        k = j + skip(j)
        m = re.compile(r"->\s*(\w+)\s*\(").match(text, k)
        if not m:
            break
        args, j = call_args(text, m.end() - 1)
        calls.append((m.group(1), args))
    return calls, j


def form_rows(page: Page) -> list[dict]:
    text = page.text
    active_tab, tabs = page.tabs()
    sections = [(m.start(), decode(call_args(text, m.end() - 1)[0][0] if call_args(text, m.end() - 1)[0] else ""))
                for m in re.finditer(r"new\s+Form_Section\s*\(", text)]
    groups = [(m.start(), m.group(1), decode(call_args(text, m.end() - 1)[0][0] if call_args(text, m.end() - 1)[0] else ""))
              for m in re.finditer(r"\$(\w+)\s*=\s*new\s+Form_[Gg]roup\s*\(", text)]
    ids_cond, class_cond = script_conditions(page.raw)
    loops = page.loop_ranges()
    classes_of_var: dict[str, list[str]] = {}
    for m in re.finditer(r"\$(\w+)->addClass\(\s*['\"]([^'\"]+)['\"]", text):
        classes_of_var.setdefault(m.group(1), []).extend(m.group(2).split())
    group_help = {}
    for m in re.finditer(r"\$(\w+)->setHelp\s*\(", text):
        args, _ = call_args(text, m.end() - 1)
        if args:
            group_help[(m.group(1), m.start())] = plain(decode(args[0]))
    rows = []
    for m in re.finditer(r"new\s+Form_(\w+)\s*\(", text):
        kind = m.group(1)
        if kind not in FIELD_CLASSES:
            continue
        args, end = call_args(text, m.end() - 1)
        before = text[max(0, m.start() - 60):m.start()]
        wrap = re.search(r"\$(\w+)\s*->\s*(?:add|addInput)\s*\(\s*$", before)
        calls, _ = chain_after(text, end, bool(wrap))
        position = m.start()
        section = ""
        for start, title in sections:
            if start < position:
                section = title
        group_name = group_label = ""
        if wrap:
            for start, var, label in groups:
                if var == wrap.group(1) and start < position:
                    group_name, group_label = var, label
        row = field_row(page, kind, args, calls, position, group_label)
        row.update(old_file=page.relative, tab=active_tab, section=plain(section), grant=grant_of(page.raw), title=title_of(page))
        gh = ""
        if group_name:
            candidates = [(p, v) for (g, p), v in group_help.items() if g == group_name and p > position]
            gh = min(candidates)[1] if candidates else ""
            if gh and not row["help"]:
                row["help"] = gh
        css = []
        for name, call_args_ in calls:
            if name == "addClass" and call_args_:
                css.extend((unquote(call_args_[0]) or "").split())
        css.extend(classes_of_var.get(group_name, []))
        conditions = []
        for key in (row["field"], row["field"] + "[]"):
            if key in ids_cond:
                conditions.append(ids_cond[key])
        for c in css:
            if c in class_cond:
                conditions.append(f"class {c}: {class_cond[c]}")
        row["_conditions"] = conditions
        row["repeat"] = "yes" if any(a <= position <= b for a, b in loops) or "repeatable" in css else ""
        if row["repeat"] or "{" in row["field"]:
            if "{" in row["field"]:
                page.dynamic.append(f"field name {row['field']}")
        rows.append(row)
    return rows


def field_row(page: Page, kind: str, args: list[str], calls, position: int, group_label: str) -> dict:
    def arg(i: int) -> str:
        return args[i] if i < len(args) else ""

    row = dict.fromkeys(COLUMNS, "")
    help_text = ""
    options = ""
    label_expr = ""
    value_expr = ""
    if kind == "StaticText":
        row.update(field="", type="static")
        label_expr, value_expr = arg(0), arg(1)
        row["default"] = plain(decode(value_expr))
        config_expr = value_expr
    elif kind == "Button":
        row.update(field=decode(arg(0)), type="button")
        label_expr = arg(1)
        link = decode(arg(2))
        if link:
            row.update(type="link-button", choices=link)
        config_expr = ""
    else:
        row["field"] = decode(arg(0))
        config_expr = ""
        if kind == "Input":
            row["type"] = decode(arg(2)) or "text"
            label_expr, value_expr = arg(1), arg(3)
        elif kind == "IpAddress":
            row["type"] = "ip(" + (decode(arg(3)) or "BOTH").lower() + ")"
            label_expr, value_expr = arg(1), arg(2)
        elif kind == "Select":
            multiple = arg(4).strip().lower() == "true"
            row["type"] = "select-multiple" if multiple else "select"
            label_expr, value_expr = arg(1), arg(2)
            options = arg(3)
        elif kind == "Checkbox":
            row["type"] = "checkbox"
            label_expr, value_expr = arg(1), arg(3)
            description = plain(decode(arg(2)))
            if description:
                row["choices"] = "text: " + description
        elif kind == "MultiCheckbox":
            row["type"] = "multicheckbox"
            label_expr, value_expr = arg(1), arg(3)
            description = plain(decode(arg(2)))
            row["choices"] = ("text: " + description) if description else ""
            if arg(4):
                row["choices"] += f"; value={decode(arg(4))}"
        elif kind == "Textarea":
            row["type"] = "textarea"
            label_expr, value_expr = arg(1), arg(2)
        elif kind == "SelectInputCombo":
            row["type"] = "select+input"
            value_expr = arg(2)
        config_expr = value_expr
        if kind == "Select":
            row["choices"] = page.choices_of(options, position)
        if kind != "Select":
            row["default"] = page.default_of(value_expr) if kind != "Checkbox" else page.default_of(value_expr)
    if kind == "Select":
        row["default"] = page.default_of(value_expr)
    row["label"] = plain(decode(label_expr)).lstrip("*").strip() or group_label
    for name, call in calls:
        if name == "setHelp" and call:
            help_text = plain(decode(call[0]))
        elif name == "setOptions" and call:
            row["choices"] = page.choices_of(call[0], position)
        elif name == "displayAsRadio":
            row["type"] = "radio"
        elif name == "setReadonly":
            row["depends_on"] = ""
    row["help"] = help_text
    key = re.search(r"\$pconfig\[\s*['\"]([\w\-.]+)['\"]\s*\]", config_expr or "")
    row["config_path"] = page.config_of(config_expr or "")
    row["_pconfig"] = key.group(1) if key else ""
    row["_value_expr"] = clean(config_expr or "")[:80]
    return row


HTML_FIELD = re.compile(r"<(input|select|textarea)\b([^>]*?)(/?)>", re.I | re.S)


def html_rows(page: Page, known: set[str]) -> list[dict]:
    rows = []
    seen = set(known)
    active_tab = page.tabs()[0]
    for m in HTML_FIELD.finditer(page.raw):
        attrs = m.group(2)
        name = re.search(r"\bname\s*=\s*(['\"])(.*?)\1", attrs, re.S)
        if not name:
            continue
        kind = (re.search(r"\btype\s*=\s*(['\"])(.*?)\1", attrs) or [None, None, "text"])[2].lower() if m.group(1).lower() == "input" else m.group(1).lower()
        if kind in ("hidden", "submit", "button") and not re.search(r"\bvalue\s*=", attrs):
            continue
        fname = php_inline(name.group(2))
        if fname.startswith("__csrf") or fname in seen:
            continue
        seen.add(fname)
        value = re.search(r"\bvalue\s*=\s*(['\"])(.*?)\1", attrs, re.S)
        row = dict.fromkeys(COLUMNS, "")
        row.update(old_file=page.relative, tab=active_tab, section="(html)", field=fname, type=kind,
                   default=php_inline(value.group(2)) if value and kind not in ("hidden",) else "",
                   grant=grant_of(page.raw), title=title_of(page))
        if kind in ("submit", "button") and value:
            row["label"] = php_inline(value.group(2))
        if kind == "hidden":
            row["default"] = php_inline(value.group(2)) if value else ""
        if "{" in fname:
            page.dynamic.append(f"html field name {fname}")
        rows.append(row)
    return rows


def php_inline(text: str) -> str:
    def expr(m: re.Match) -> str:
        return decode(re.sub(r";\s*$", "", m.group(1)))
    text = re.sub(r"<\?=\s*(.*?)\s*\?>", expr, text, flags=re.S)
    text = re.sub(r"<\?php\s*(.*?)\s*\?>", lambda m: "{" + clean(m.group(1))[:40] + "}", text, flags=re.S)
    return plain(text)


def table_row(page: Page) -> dict | None:
    raw = page.raw
    headers = []
    for m in re.finditer(r"<th\b[^>]*>(.*?)</th>", raw, re.S | re.I):
        text = php_inline(m.group(1))
        if text and text not in headers:
            headers.append(text)
    has_table = bool(re.search(r"<table\b|<th\b|fs_table", raw, re.I))
    actions: list[str] = []
    for m in re.finditer(r"<(a|button)\b([^>]*)>(.*?)</\1>", raw, re.S | re.I):
        attrs, inner = m.group(2), m.group(3)
        href = re.search(r"href\s*=\s*(['\"])(.*?)\1", attrs, re.S)
        title = re.search(r"(?:title|aria-label)\s*=\s*(['\"])(.*?)\1", attrs, re.S)
        name = re.search(r"\bname\s*=\s*(['\"])(.*?)\1", attrs, re.S)
        text = php_inline(inner)
        looks_action = "fa-" in attrs or "fa-" in inner or "btn" in attrs or "action" in attrs.lower() or (name and m.group(1).lower() == "button")
        if not looks_action:
            continue
        label = php_inline(title.group(2)) if title else text
        target = php_inline(href.group(2)) if href else (php_inline(name.group(2)) if name else "")
        entry = clean(f"{label}|{target}")
        if label.strip() and entry not in actions:
            actions.append(entry)
    for m in re.finditer(r"new\s+Form_Button\s*\(", page.text):
        args, _ = call_args(page.text, m.end() - 1)
        if len(args) >= 2:
            entry = f"{plain(decode(args[1]))}|{decode(args[0])}"
            if entry not in actions:
                actions.append(entry)
    if not (headers or has_table or actions):
        return None
    row = dict.fromkeys(COLUMNS, "")
    row.update(old_file=page.relative, tab=page.tabs()[0], section="", field="(table)" if has_table else "(html)",
               label=page.name, type="table" if has_table else "html", choices="columns: " + "; ".join(headers),
               help="actions: " + "; ".join(actions[:40]), grant=grant_of(page.raw), title=title_of(page))
    return row


def page_rows(relative: str, old_root: Path) -> tuple[list[dict], list[str]]:
    path = old_root / relative.removeprefix("src/")
    if not path.is_file():
        return [], ["missing file"]
    source = path.read_text(encoding="utf-8", errors="replace")
    page = Page(relative, source, old_root)
    rows = form_rows(page)
    for row in rows:
        parts = []
        if row["config_path"]:
            parts.append("config:" + row["config_path"])
        if row["_pconfig"]:
            parts.append("pconfig:" + row["_pconfig"])
        parts.extend(row.pop("_conditions", []))
        row["depends_on"] = "; ".join(parts)
    known = {r["field"] for r in rows}
    rows += html_rows(page, known)
    pages_tab = page.tabs()
    if pages_tab[1]:
        extra = dict.fromkeys(COLUMNS, "")
        extra.update(old_file=relative, tab=pages_tab[0], field="(tabs)", type="tabs", label=page.name,
                     choices="; ".join(f"{label}={link}" for label, link in pages_tab[1]), grant=grant_of(page.raw),
                     title=title_of(page))
        rows.append(extra)
    table = table_row(page)
    if table and (not any(r["type"] not in ("html",) and r["section"] != "(html)" for r in rows) or "<th" in source):
        rows.append(table)
    if not rows:
        placeholder = dict.fromkeys(COLUMNS, "")
        placeholder.update(old_file=relative, field="(none)", type="none", grant=grant_of(page.raw), title=title_of(page))
        rows.append(placeholder)
    if re.search(r"new\s+Form\b", page.text) and not any(r["section"] != "(html)" and r["type"] not in ("tabs", "table") for r in rows):
        page.dynamic.append("Form without literal fields")
    if re.search(r"\b(?:eval|include|require)\s*\(?\s*\$", page.text):
        page.dynamic.append("includes code chosen at run time")
    for row in rows:
        for hidden in ("_pconfig", "_value_expr"):
            row.pop(hidden, None)
    return rows, sorted(set(page.dynamic))


def run(old_root: Path, audit: Path, out: Path, dynamic: Path) -> dict:
    with audit.open(encoding="utf-8", newline="") as stream:
        pages = [r["path"] for r in csv.DictReader(stream, delimiter="\t") if r["path"].endswith(".php")]
    all_rows: list[dict] = []
    dynamic_rows: list[tuple[str, str]] = []
    for relative in pages:
        rows, why = page_rows(relative, old_root)
        all_rows.extend(rows)
        if why:
            dynamic_rows.append((relative, "; ".join(why)))
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(COLUMNS)
        for row in all_rows:
            writer.writerow([clean(str(row.get(c, ""))) for c in COLUMNS])
    with dynamic.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["old_file", "reason"])
        writer.writerows(dynamic_rows)
    return {"pages": len(pages), "rows": len(all_rows), "dynamic": len(dynamic_rows)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", type=Path, default=OLD)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--dynamic", type=Path, default=DYNAMIC)
    args = parser.parse_args()
    print(run(args.old, args.audit, args.out, args.dynamic))


if __name__ == "__main__":
    main()
