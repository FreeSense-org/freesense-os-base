# SPDX-License-Identifier: Apache-2.0
"""Read-only crawler that captures every page of the old FreeSense 1.2 WebUI as a reference.

Usage: python tools/old_ui_crawl.py --url https://192.0.2.1/ --out DIR [--dry-run] [--only TEXT] [--limit N]
       [--no-discover] [--delay-min S] [--delay-max S] [--max-errors N] [--width PX] [--headed]

The credentials come from the environment only: FS_USER (default admin) and FS_PW. Nothing is typed on the command
line, printed or stored. The login form is submitted once; every other request is a GET (or HEAD): a Playwright route
handler aborts any other method, and any document or fetch request whose URL the classifier refuses. The crawler never
clicks a submit button or a control whose text or target matches the denylist; it expands panels, changes selects and
checkboxes client-side and reads the result, and never submits a form.

Per page it writes DIR/<slug>/{dark,light,expanded,tab-N,option-*}.png and data.json; DIR/index.tsv and
DIR/old-fields-rendered.tsv (the columns of planning/inventory/old-fields.tsv, for tools/parity.py) are rebuilt from the
data.json files, so a rerun resumes: pages whose data.json says status ok are skipped. Requires Python 3.12 or later
and Playwright (browser channel from FREESENSE_BROWSER_CHANNEL, default chrome on Windows).
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "planning/inventory/page-audit.tsv"
MENU = ROOT / "planning/base-shell/old-menu.tsv"
TABS = ROOT / "planning/base-shell/old-tabs.tsv"
DEFAULT_OUT = ROOT.parents[1] / "test-lab" / "old-reference"
WWW_PREFIX = "src/usr/local/www/"
COLUMNS = ["old_file", "tab", "section", "field", "label", "type", "default", "choices", "help", "depends_on",
           "config_path", "repeat", "grant", "title"]
INDEX_COLUMNS = ["page", "url", "tabs", "screenshots", "json", "status", "error"]

# ------------------------------------------------------------------ safety classification

DENY_WORDS = ("logout", "del", "delete", "act=", "reboot", "halt", "restore", "clear", "reset", "install", "upgrade",
              "download", "apply", "reload", "kill", "restart")
DENY_PAGES = {"diag_reboot.php", "diag_halt.php", "diag_defaults.php", "diag_edit.php", "diag_resetstate.php",
              "diag_backup_download.php", "pkg_mgr_install.php", "system_firmware_upgrade.php",
              "system_firmware_restorefullbackup.php", "config.xml"}
ALLOW_PAGES = {"pkg_mgr_installed.php"}  # lists installed packages; the word 'install' is not an action here
SAFE_ACT = re.compile(r"(^|&)act=(edit|new|add|view|show)(?=&|$)")
BAD_TEXT = re.compile(r"\b(save|apply|delete|remove|add|reboot|halt|restore|upgrade|install|clear|reset|download|restart|"
                      r"reload|kill|logout|log out|sign out|submit|import|create|generate|flush|empty|test|run|"
                      r"update|start|stop|enable|disable|send|upload)\b", re.I)
BAD_CLICK_ATTR = re.compile(r"submit|location|href|delete|reboot|apply|reset|clear|restore|\.ajax|fetch|post", re.I)
SECRET_NAME = r"pass|psk|secret|token|apikey|api_key|privkey|private|prv|passphrase|\bkey\b|_key|key_|crt|cert"
SECRET_RE = re.compile(SECRET_NAME, re.I)
TYPE_SELECT = re.compile(r"type|proto|mode|family|method|direction|action|ipaddr|gateway|if$|^if", re.I)
VOLATILE_PARAMS = {"__csrf_magic", "t", "_", "rand"}


def canonical_url(url: str, base: str = "") -> str:
    """Absolute URL without fragment and volatile parameters; parameters sorted."""
    absolute = urljoin(base, url) if base else url
    parts = urlparse(absolute)
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in VOLATILE_PARAMS)
    path = parts.path or "/"
    return parts._replace(path=path, query=urlencode(query, safe="/:.,"), fragment="").geturl()


def classify_url(url: str, base: str) -> tuple[bool, str]:
    """Return (allowed, reason). Only same-origin http(s) GET targets without an action word are allowed."""
    parts, root = urlparse(url), urlparse(base)
    if parts.scheme not in ("http", "https"):
        return False, "scheme"
    if parts.netloc.lower() != root.netloc.lower():
        return False, "external"
    page = parts.path.rsplit("/", 1)[-1].lower()
    query = SAFE_ACT.sub("", parts.query.lower()).strip("&")
    if page in DENY_PAGES:
        return False, "denied page " + page
    if page == "diag_backup.php" and parts.query:
        return False, "backup content"
    if page == "diag_confbak.php" and re.search(r"newver|oldver|download|rev", query):
        return False, "raw configuration"
    path = "" if page in ALLOW_PAGES else parts.path.lower()
    for word in DENY_WORDS:
        if word in path or word in query:
            return False, "denied word " + word
    return True, ""


def safe_click(tag: str, kind: str, text: str, href: str, onclick: str, base: str) -> tuple[bool, str]:
    """Decide whether the crawler may click this control (tab or panel toggles only, never actions)."""
    tag, kind = tag.lower(), (kind or "").lower()
    if kind in ("submit", "image", "reset"):
        return False, "submit control"
    if tag == "button" and not kind:
        return False, "submit control"  # a bare button submits its form
    if BAD_TEXT.search(text or ""):
        return False, "action text"
    if onclick and BAD_CLICK_ATTR.search(onclick):
        return False, "action handler"
    if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
        ok, why = classify_url(urljoin(base, href), base)
        if not ok:
            return False, why
    return True, ""


def is_secret(tag: str, kind: str, name: str, ident: str) -> bool:
    kind = (kind or "").lower()
    if kind == "password":
        return True
    if kind in ("checkbox", "radio", "hidden", "submit", "button", "select-one", "select-multiple"):
        return False
    return bool(SECRET_RE.search(f"{name} {ident}")) and (tag != "select")


class RouteGuard:
    """Playwright route handler: aborts anything but same-origin GET/HEAD (plus one login POST when allowed)."""

    def __init__(self, base: str):
        self.base = base
        self.allow_login_post = False
        self.blocked: list[str] = []

    def __call__(self, route) -> None:
        request = route.request
        method = request.method.upper()
        if method == "POST" and self.allow_login_post and urlparse(request.url).path == urlparse(self.base).path \
                or (method == "POST" and self.allow_login_post and request.resource_type == "document"):
            self.allow_login_post = False
            route.continue_()
            return
        if method not in ("GET", "HEAD"):
            self.blocked.append(f"{method} {request.url}")
            route.abort()
            return
        if request.resource_type in ("document", "xhr", "fetch", "other"):
            ok, why = classify_url(request.url, self.base)
            if not ok:
                self.blocked.append(f"GET {request.url} ({why})")
                route.abort()
                return
        elif urlparse(request.url).netloc.lower() != urlparse(self.base).netloc.lower():
            route.abort()
            return
        route.continue_()


# ------------------------------------------------------------------ page list

def read_tsv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def usable_target(target: str) -> str | None:
    target = (target or "").strip().split(" ")[0]
    if not target or any(c in target for c in "<>${}") or target.endswith("="):
        return None
    if "?" not in target and not target.endswith(".php") and target != "/":
        return None
    return target.lstrip("/") if target != "/" else "index.php"


def seed_paths(audit: Path = AUDIT, menu: Path = MENU, tabs: Path = TABS) -> list[str]:
    """Relative page targets (path?query) from the inventory, deduplicated, in file order."""
    found: list[str] = []
    for row in read_tsv(audit) if audit.exists() else []:
        path = row.get("path", "")
        if row.get("status") == "obsolete" or not path.startswith(WWW_PREFIX) or not path.endswith(".php"):
            continue
        relative = path[len(WWW_PREFIX):]
        if relative.split("/")[0] in ("classes", "includes", "api", "widgets", "vendor", "js", "css"):
            continue
        found.append(relative)
    for row in read_tsv(menu) if menu.exists() else []:
        target = usable_target(row.get("target", ""))
        if target:
            found.append(target)
    for row in read_tsv(tabs) if tabs.exists() else []:
        target = usable_target(row.get("tab_target", ""))
        if target:
            found.append(target)
    out, seen = [], set()
    for item in found:
        key = canonical_url(item, "https://x/")
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def plan_urls(base: str, **sources) -> list[str]:
    urls = []
    for item in seed_paths(**sources):
        url = canonical_url(item, base)
        if classify_url(url, base)[0]:
            urls.append(url)
    return list(dict.fromkeys(urls))


def slugify(url: str) -> str:
    parts = urlparse(url)
    text = parts.path.strip("/") or "index"
    if parts.query:
        text += "__" + parts.query
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.replace("/", "_")).strip("-")
    if len(text) > 90:
        text = text[:70] + "-" + hashlib.sha1(text.encode()).hexdigest()[:10]
    return text


def dynamic_variants(url: str, base: str) -> list[str]:
    """Pages that exist per interface, derived from an interfaces.php?if=<id> link."""
    parts = urlparse(url)
    if parts.path.rsplit("/", 1)[-1] != "interfaces.php":
        return []
    ident = dict(parse_qsl(parts.query)).get("if")
    if not ident:
        return []
    return [canonical_url(f"{page}?if={quote(ident)}", base) for page in ("firewall_rules.php", "services_dhcp.php")]


def discover(data: dict, url: str, base: str, leaf: bool) -> list[str]:
    """Links worth visiting from one captured page: menu, own tabs, interface variants and edit/add views."""
    out: list[str] = []

    def add(href: str) -> None:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return
        target = canonical_url(href, url)
        if classify_url(target, base)[0]:
            out.append(target)

    for href in data.get("nav_links", []):
        add(href)
    for tab in data.get("tabs", []):
        add(tab.get("href", ""))
    for link in data.get("nav_links", []) + [t.get("href", "") for t in data.get("tabs", [])]:
        for variant in dynamic_variants(canonical_url(link, url), base) if link else []:
            out.append(variant)
    if not leaf:
        firsts: dict[str, str] = {}
        for link in data.get("content_links", []):
            href, text = link.get("href", ""), link.get("text", "")
            path = urlparse(urljoin(url, href)).path.rsplit("/", 1)[-1]
            edit = "edit" in path or SAFE_ACT.search(urlparse(href).query) or text.strip().lower().startswith("add")
            if not edit or not path.endswith(".php"):
                continue
            key = path + ("#add" if text.strip().lower().startswith("add") or "new" in href else "#edit")
            if key not in firsts:
                firsts[key] = href
        for href in firsts.values():
            add(href)
            bare = urlparse(urljoin(url, href))._replace(query="").geturl()
            add(bare)
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ in-page scripts

EXTRACT_JS = r"""(secretPattern) => {
  const secret = new RegExp(secretPattern, 'i');
  const text = e => (e ? (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const labelOf = e => {
    let t = '';
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = text(l); }
    if (!t) { const l = e.closest('label'); if (l) t = text(l); }
    if (!t) { const g = e.closest('.form-group'); if (g) t = text(g.querySelector('label, .control-label')); }
    return t;
  };
  const helpOf = e => { const g = e.closest('.form-group'); return g ? text(g.querySelector('.help-block, .text-muted, .form-text, small')) : ''; };
  const sectionOf = e => { const p = e.closest('.panel, .card'); return p ? text(p.querySelector('.panel-title, .card-title, .panel-heading')) : ''; };
  const controls = [];
  document.querySelectorAll('input, select, textarea').forEach((e, index) => {
    const type = (e.type || e.tagName).toLowerCase();
    if (type === 'hidden' && /^__csrf_magic$/.test(e.name)) return;
    if (['submit', 'button', 'image', 'reset'].includes(type)) return;
    const c = {index, tag: e.tagName.toLowerCase(), name: e.name || '', id: e.id || '', type, label: labelOf(e),
      help: helpOf(e), section: sectionOf(e), required: !!e.required, disabled: !!e.disabled, readonly: !!e.readOnly,
      visible: type !== 'hidden' && shown(e), maxlength: e.maxLength > 0 ? e.maxLength : null, placeholder: e.placeholder || ''};
    const isSecret = type === 'password' || (!['checkbox','radio','hidden','select-one','select-multiple'].includes(type)
      && secret.test((e.name || '') + ' ' + (e.id || '')));
    c.secret = isSecret;
    if (e.tagName === 'SELECT') {
      c.options = Array.from(e.options).map(o => ({value: o.value, label: text(o), selected: o.defaultSelected}));
      c.default = Array.from(e.options).filter(o => o.defaultSelected).map(o => o.value).join(',');
      if (!c.default && e.options.length && !e.multiple) c.default = e.options[0].value;
    } else if (type === 'checkbox' || type === 'radio') {
      c.checked = e.defaultChecked; c.value = e.value; c.default = e.defaultChecked ? (e.value || 'on') : '';
    } else { c.default = isSecret ? '' : e.defaultValue; }
    if (isSecret) { c.state = (e.defaultValue || '').length ? 'set' : 'empty'; }
    controls.push(c);
  });
  const tabs = Array.from(document.querySelectorAll('.nav-tabs a, ul.nav-tabs li a')).map(a => ({
    label: text(a), href: a.getAttribute('href') || '',
    active: a.classList.contains('active') || (a.parentElement && a.parentElement.classList.contains('active')),
    js: (a.getAttribute('data-toggle') || a.getAttribute('data-bs-toggle')) === 'tab' || (a.getAttribute('href') || '').startsWith('#')}));
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], a.btn')).map(b => ({
    text: text(b) || b.value || b.title || '', type: (b.type || '').toLowerCase(), href: b.getAttribute('href') || '', name: b.name || ''}));
  const tables = Array.from(document.querySelectorAll('table')).map(t => {
    const row = t.querySelector('tbody tr');
    return {headers: Array.from(t.querySelectorAll('thead th, tr:first-child th')).map(text).filter(Boolean),
      rows: t.querySelectorAll('tbody tr').length,
      row_actions: row ? Array.from(row.querySelectorAll('a, button')).map(a => ({text: text(a) || a.title || '', title: a.title || '',
        href: a.getAttribute('href') || '', cls: a.className || ''})) : []};
  });
  const nav = Array.from(document.querySelectorAll('nav a[href], .navbar a[href], #sidebar a[href], .dropdown-menu a[href]'))
    .map(a => a.getAttribute('href'));
  const content = Array.from(document.querySelectorAll('a[href]')).filter(a => !a.closest('nav, .navbar, #sidebar, .nav-tabs'))
    .map(a => ({href: a.getAttribute('href'), text: text(a) || a.title || '', cls: a.className || ''}));
  return {
    title: document.title,
    heading: text(document.querySelector('h1, .page-header, #pagetitle')),
    breadcrumbs: Array.from(document.querySelectorAll('.breadcrumb li, ol.breadcrumb a')).map(text),
    tabs, panel_headings: Array.from(document.querySelectorAll('.panel-title, .card-title, .panel-heading h2')).map(text),
    controls, buttons, tables, nav_links: nav, content_links: content,
    alerts: Array.from(document.querySelectorAll('.alert, .notice, .alert-info, .alert-warning, .alert-danger')).map(text),
    login: !!document.querySelector('#usernamefld'),
    collapsed: document.querySelectorAll('.collapse:not(.in):not(.show), details:not([open])').length,
  };
}"""

VISIBLE_JS = r"""() => Array.from(document.querySelectorAll('input, select, textarea')).filter(e =>
  e.type !== 'hidden' && (e.offsetWidth || e.offsetHeight || e.getClientRects().length)).map(e => e.name || e.id)"""

MASK_JS = r"""(secretPattern) => {
  const secret = new RegExp(secretPattern, 'i');
  let n = 0;
  document.querySelectorAll('input, textarea').forEach(e => {
    const type = (e.type || 'textarea').toLowerCase();
    if (['checkbox','radio','hidden','submit','button','image','reset','file'].includes(type)) return;
    if ((type === 'password' || secret.test((e.name || '') + ' ' + (e.id || ''))) && e.value) {
      e.value = '\u2022'.repeat(8); n++;
    }
  });
  return n;
}"""

EXPAND_JS = r"""() => {
  let n = 0;
  document.querySelectorAll('.collapse:not(.in):not(.show)').forEach(e => {
    e.classList.add('in', 'show'); e.style.height = 'auto';
    if (getComputedStyle(e).display === 'none') e.style.display = 'block';
    n++;
  });
  document.querySelectorAll('details:not([open])').forEach(e => { e.open = true; n++; });
  return n;
}"""

NOSUBMIT_JS = r"""() => {
  HTMLFormElement.prototype.submit = function () {};
  HTMLFormElement.prototype.requestSubmit = function () {};
  document.addEventListener('submit', e => { e.preventDefault(); e.stopImmediatePropagation(); }, true);
}"""


# ------------------------------------------------------------------ TSV writers

def field_rows(data: dict) -> list[dict]:
    """Rows in the columns of planning/inventory/old-fields.tsv for one captured page."""
    url = data["url"]
    parts = urlparse(url)
    old_file = WWW_PREFIX + (parts.path.strip("/") or "index.php")
    active = next((t["label"] for t in data.get("tabs", []) if t.get("active")), "")
    tab = active or (("?" + parts.query) if parts.query else "")
    title = " / ".join(data.get("breadcrumbs") or [data.get("heading") or data.get("title", "")])
    deps = data.get("dependencies", {})
    rows = []
    for c in data.get("controls", []):
        name = c["name"] or c["id"]
        if not name:
            continue
        choices = "; ".join(f"{o['value']}={o['label']}" if o["value"] != o["label"] else o["label"]
                            for o in c.get("options", []))
        rows.append(dict(zip(COLUMNS, [old_file, tab, c.get("section", ""), name, c.get("label", ""),
                                       c["type"] if c["tag"] == "input" else c["tag"],
                                       "set" if c.get("secret") and c.get("state") == "set" else c.get("default", ""),
                                       choices, c.get("help", ""), deps.get(name, ""), "", "", "", title])))
    for t in data.get("tables", []):
        if t["headers"]:
            actions = sorted({a["text"] or a["title"] for a in t["row_actions"] if a["text"] or a["title"]})
            rows.append(dict(zip(COLUMNS, [old_file, tab, "(html)", "table", "", "table", "",
                                           "columns: " + "; ".join(t["headers"]), "actions: " + "; ".join(actions),
                                           "", "", "", "", title])))
    return rows


def write_tsv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([re.sub(r"[\t\r\n]+", " ", str(row.get(c, ""))) for c in columns])


def load_pages(out: Path) -> list[dict]:
    pages = []
    for path in sorted(out.glob("*/data.json")):
        try:
            pages.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return pages


def write_outputs(out: Path) -> tuple[int, int]:
    pages = load_pages(out)
    index, fields = [], []
    for data in pages:
        meta = data.get("meta", {})
        index.append({"page": meta.get("slug", ""), "url": data.get("url", ""),
                      "tabs": "; ".join(t["label"] for t in data.get("tabs", [])),
                      "screenshots": " ".join(meta.get("screenshots", [])), "json": f"{meta.get('slug', '')}/data.json",
                      "status": meta.get("status", ""), "error": meta.get("error", "")})
        if meta.get("status") == "ok":
            fields += field_rows(data)
    seen, unique = set(), []
    for row in fields:
        key = (row["old_file"], row["tab"], row["field"], row["section"], row["type"], row["label"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    write_tsv(out / "index.tsv", INDEX_COLUMNS, index)
    write_tsv(out / "old-fields-rendered.tsv", COLUMNS, unique)
    return len(index), len(unique)


# ------------------------------------------------------------------ browser work

class Crawler:
    def __init__(self, page, guard: RouteGuard, base: str, out: Path, args):
        self.page, self.guard, self.base, self.out, self.args = page, guard, base, out, args

    def pause(self) -> None:
        time.sleep(random.uniform(self.args.delay_min, self.args.delay_max))

    def settle(self) -> None:
        page = self.page
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        height = page.evaluate("document.documentElement.scrollHeight")
        for y in range(0, height, 600):
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(100)
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        previous = -1
        for _ in range(4):  # let charts and lazy content finish growing the page
            page.wait_for_timeout(700)
            now = page.evaluate("document.documentElement.scrollHeight")
            if now == previous:
                break
            previous = now
        page.evaluate("window.scrollTo(0,0)")

    def login(self) -> None:
        password = os.environ.get("FS_PW")
        if not password:
            raise SystemExit("FS_PW is not set in the environment")
        self.page.goto(self.base, wait_until="domcontentloaded")
        if not self.page.query_selector("#usernamefld"):
            return
        self.page.fill("#usernamefld", os.environ.get("FS_USER", "admin"))
        self.page.fill("#passwordfld", password)
        self.guard.allow_login_post = True
        self.page.click("button[name=login]")
        self.page.wait_for_load_state("domcontentloaded")
        self.guard.allow_login_post = False
        self.page.wait_for_timeout(1500)
        if self.page.query_selector("#usernamefld"):
            raise SystemExit("login failed (check FS_USER / FS_PW); not retrying")

    def shot(self, directory: Path, name: str, shots: list[str]) -> None:
        self.page.evaluate(MASK_JS, SECRET_NAME)
        path = directory / name
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            self.page.screenshot(path=str(path))
        shots.append(name)

    def capture(self, url: str) -> dict:
        page = self.page
        slug = slugify(url)
        directory = self.out / slug
        directory.mkdir(parents=True, exist_ok=True)
        shots: list[str] = []
        page.emulate_media(color_scheme="dark")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if page.query_selector("#usernamefld"):
            self.login()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if page.query_selector("#usernamefld"):
                raise RuntimeError("session lost")
        page.evaluate(NOSUBMIT_JS)
        self.settle()
        data = page.evaluate(EXTRACT_JS, SECRET_NAME)
        data["url"] = canonical_url(page.url, self.base)
        for control in data["controls"]:  # never keep a secret value, only set/empty
            if control["secret"]:
                control["default"] = ""
                control.pop("value", None)
        self.shot(directory, "dark.png", shots)
        page.emulate_media(color_scheme="light")
        page.wait_for_timeout(300)
        self.shot(directory, "light.png", shots)
        page.emulate_media(color_scheme="dark")
        if page.evaluate(EXPAND_JS):
            page.wait_for_timeout(300)
            self.shot(directory, "expanded.png", shots)
        self.js_tabs(data, directory, shots)
        data["dependencies"] = self.interactions(data, directory, shots)
        data["blocked_requests"] = list(self.guard.blocked)
        self.guard.blocked.clear()
        data["meta"] = {"slug": slug, "status": "ok", "error": "", "screenshots": shots,
                        "captured": time.strftime("%Y-%m-%dT%H:%M:%S")}
        return data

    def js_tabs(self, data: dict, directory: Path, shots: list[str]) -> None:
        page = self.page
        for number, tab in enumerate(data["tabs"], 1):
            if not tab.get("js") or tab.get("active"):
                continue
            ok, _ = safe_click("a", "", tab["label"], tab["href"], "", self.base)
            if not ok:
                continue
            target = page.locator(".nav-tabs a").filter(has_text=tab["label"]).first
            try:
                target.click(timeout=3000)
                page.wait_for_timeout(400)
                self.shot(directory, f"tab-{number}.png", shots)
            except Exception:
                continue

    def interactions(self, data: dict, directory: Path, shots: list[str]) -> dict:
        """Change selects and checkboxes client-side only; record which fields appear or vanish."""
        page = self.page
        deps: dict[str, list[str]] = {}
        base_url = page.url
        selects = [c for c in data["controls"] if c["tag"] == "select" and c["visible"] and not c["disabled"]
                   and 1 < len(c.get("options", [])) <= 40][:8]
        boxes = [c for c in data["controls"] if c["type"] == "checkbox" and c["visible"] and not c["disabled"]][:30]
        for control in selects:
            name = control["name"] or control["id"]
            locator = page.locator("select").nth(self.dom_index(data, control, "select"))
            original = control["default"] or control["options"][0]["value"]
            before = set(page.evaluate(VISIBLE_JS))
            shoot = bool(TYPE_SELECT.search(name))
            for number, option in enumerate(control["options"][:12]):
                try:
                    locator.select_option(value=option["value"], timeout=2000)
                    page.wait_for_timeout(250)
                except Exception:
                    break
                after = set(page.evaluate(VISIBLE_JS))
                self.note(deps, name, option["value"], before, after)
                if shoot:
                    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{name}-{option['value']}")[:50]
                    self.shot(directory, f"option-{number}-{safe}.png", shots)
            self.restore(locator, original, base_url)
        for control in boxes:
            name = control["name"] or control["id"]
            attrs = page.evaluate("(i) => { const e = document.querySelectorAll('input, select, textarea')[i]; "
                                  "return (e.getAttribute('onclick') || '') + ' ' + (e.getAttribute('onchange') || ''); }",
                                  control["index"])
            if BAD_CLICK_ATTR.search(attrs):
                continue
            locator = page.locator("input, select, textarea").nth(control["index"])
            before = set(page.evaluate(VISIBLE_JS))
            try:
                locator.evaluate("e => { e.checked = !e.checked; e.dispatchEvent(new Event('click', {bubbles:true})); "
                                 "e.dispatchEvent(new Event('change', {bubbles:true})); }")
                page.wait_for_timeout(200)
                after = set(page.evaluate(VISIBLE_JS))
                self.note(deps, name, "checked" if not control["checked"] else "unchecked", before, after)
                locator.evaluate("e => { e.checked = !e.checked; e.dispatchEvent(new Event('change', {bubbles:true})); }")
            except Exception:
                pass
            if page.url != base_url:
                page.goto(base_url, wait_until="domcontentloaded")
                page.evaluate(NOSUBMIT_JS)
        result: dict[str, str] = {}
        for controller, items in deps.items():
            for target, how in items:
                result.setdefault(target, "")
                result[target] += ("; " if result[target] else "") + how
        return result

    @staticmethod
    def dom_index(data: dict, control: dict, tag: str) -> int:
        return sum(1 for c in data["controls"] if c["tag"] == tag and c["index"] < control["index"])

    @staticmethod
    def note(deps: dict, controller: str, value: str, before: set, after: set) -> None:
        for field in sorted(after - before):
            deps.setdefault(controller, []).append((field, f"visible ({controller}={value})"))
        for field in sorted(before - after):
            deps.setdefault(controller, []).append((field, f"hidden ({controller}={value})"))

    def restore(self, locator, original: str, base_url: str) -> None:
        try:
            locator.select_option(value=original, timeout=2000)
        except Exception:
            pass
        if self.page.url != base_url:
            self.page.goto(base_url, wait_until="domcontentloaded")
            self.page.evaluate(NOSUBMIT_JS)


def run(args) -> int:
    base = args.url if args.url.endswith("/") else args.url + "/"
    urls = plan_urls(base)
    if args.only:
        urls = [u for u in urls if args.only in u]
    if args.dry_run:
        for url in urls:
            print(url)
        print(f"{len(urls)} URLs planned from the inventory (discovery adds menu, tab, interface and edit views)")
        return 0
    from playwright.sync_api import sync_playwright
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    done = {d["url"] for d in load_pages(out) if d.get("meta", {}).get("status") == "ok"}
    queue = [u for u in urls]
    seen = set(queue) | done
    errors = captured = 0
    channel = os.environ.get("FREESENSE_BROWSER_CHANNEL", "chrome" if os.name == "nt" else None)
    with sync_playwright() as engine:
        browser = engine.chromium.launch(channel=channel, headless=not args.headed)
        context = browser.new_context(viewport={"width": args.width, "height": 900}, ignore_https_errors=True,
                                      service_workers="block")
        guard = RouteGuard(base)
        context.route("**/*", guard)
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        crawler = Crawler(page, guard, base, out, args)
        crawler.login()
        index = 0
        while index < len(queue) and captured < args.limit:
            url = queue[index]
            index += 1
            if url in done:
                continue
            try:
                data = crawler.capture(url)
                errors = 0
                captured += 1
                leaf = bool(re.search(r"_edit\.php|[?&]id=", url))
                if not args.no_discover:
                    for link in discover(data, url, base, leaf):
                        if link not in seen:
                            seen.add(link)
                            queue.append(link)
                print(f"[{captured}] ok  {url} (queue {len(queue) - index})", flush=True)
            except SystemExit:
                raise
            except Exception as error:
                errors += 1
                slug = slugify(url)
                (out / slug).mkdir(parents=True, exist_ok=True)
                (out / slug / "data.json").write_text(json.dumps(
                    {"url": url, "meta": {"slug": slug, "status": "error", "error": str(error).splitlines()[0][:200],
                                          "screenshots": []}}, indent=1), encoding="utf-8")
                print(f"[err] {url}: {str(error).splitlines()[0][:120]}", flush=True)
                if errors >= args.max_errors:
                    print(f"stopping after {errors} consecutive errors")
                    break
            else:
                (out / slugify(url) / "data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False),
                                                               encoding="utf-8")
            write_outputs(out)
            crawler.pause()
        browser.close()
    pages, fields = write_outputs(out)
    print(f"summary: {captured} captured this run, {pages} pages in index.tsv, {fields} field rows, "
          f"{len(queue) - index} left in queue; resume by running the same command again")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true", help="list the planned inventory URLs and exit")
    parser.add_argument("--only", default="", help="only plan URLs containing this text")
    parser.add_argument("--limit", type=int, default=2000, help="stop after this many captured pages")
    parser.add_argument("--no-discover", action="store_true", help="skip live menu/tab/edit discovery")
    parser.add_argument("--delay-min", type=float, default=0.4)
    parser.add_argument("--delay-max", type=float, default=0.8)
    parser.add_argument("--max-errors", type=int, default=5, help="stop after this many consecutive errors")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--headed", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
