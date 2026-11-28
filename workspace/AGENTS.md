# FreeSense 2.0 local refactor

Build for fresh installations on FreeBSD with PHP 8.6. This directory is a local
workspace containing independent components. Keep work, checks, and artifacts
local. Read the parent workspace guidance for credentials, lab ownership, and
release safeguards. Publication requires a separate user request.

Use parallel agents for independent planning, implementation and verification.
Keep the total team at ten agents or fewer, including the coordinating agent.
Give each change a clear owner when agents share a working directory.

## Admit one capability at a time

1. Trace the installed entry points, callers, privileges, configuration, dynamic
   hooks and side effects at the recorded Development revision. Read the source
   repository's guidance. Capture observable behavior in tests against that
   revision. Document and test intentional differences separately.
2. Develop under `local-output/`. Admit reviewed replacement files, tests,
   attribution and corresponding package entries together under the owning
   component. Keep raw VM evidence under `test-lab/`; use synthetic fixtures
   here. Preserve third-party licenses. Record reference revisions and test
   environments in the capability ledger.
3. Exclude 1.1 implementations, upgrade shims, old scripts without a traced
   caller, bulk source copies and dependencies on other workspace trees. Reject
   incompatible backups before writing. Keep an administrative recovery path
   for backups produced by this system.
4. Group work by operator capability and test it as set out under
   [How much to test](#how-much-to-test). Keep the System and Optional Package
   code shared across architectures. Record portable and native results
   separately. Add checks for new behavior.

## How much to test

Further development must not cost hours of testing per change.

- While working: `python tools/check.py --affected` (packaging gates, lint and the missing-import check on the
  changed files, and only the tests that name them; shared files add their group from
  `tools/check-affected.json`). It compares against the last full System pass, or `--since 6h`; `--list` previews.
- At the end of a batch, once: `python tools/check.py` (every component). Also when a change to shared files is not
  covered by its group.
- Lab acceptance, `python tools/lab_accept.py`, only for boot, firewall or service changes (and `--fresh` when the
  installer, packaging or dependencies changed). Run native tests that `--affected` names there or on the builder.
  It takes over lab VMs 150 and 152 and stops them again ([Proxmox lab](planning/proxmox-lab.md)).
- Do not rerun passing checks to be sure, and do not run the full check or the lab for each change.
- Jobs run side by side (`--jobs` or `FREESENSE_CHECK_JOBS`; PHP processes: `FREESENSE_PHP_JOBS`). A new test must
  be safe next to the others: port 0, its own temporary directory, no shared files. In containers, mount the
  workspace snapshot (`ui_helpers.workspace_mount`), never the tree: a Windows bind mount is up to 100 times slower.
  Wait for conditions rather than fixed times.

## Configuration compatibility

This tree is FreeSense 2.0 (the local rebuild of 1.2, called 1.3 from 2026-11-22 to 2026-11-28; see
[rename-2-0](planning/rename-2-0.md). Development 1.2 on `main` is the old reference). The goal is a near 1:1 replacement of Development 1.2 and its Optional Packages on the new backend. A
`config.xml` from the pinned Development 1.2 release must import (through the 1.2 importer into the 2.0 format) and
run, and no operator function may be lost.

- Keep the pinned config element names, value semantics, defaults and ordering for every capability. New settings
  are additive and optional; record an intentional difference with its reason in the capability's planning note.
- Keep each page's fields, options, labels and conditional behaviour recognisable from the pinned page. Add the
  shared AJAX commands and client and server validation without dropping an option; a page that works for the
  core flow but lacks options is `partial` in `planning/page-audit.md`, not ported.
- Accept backups from the pinned Development 1.2 release (the step 3 rule above rejects only foreign and 1.1
  input). The store loads only `format="2.0"`; a 1.2 file goes through the importer, which writes a backup of the
  original first. Validate before writing, preserve unknown but well-formed sections for later capabilities, and
  keep the administrative recovery path.
- Test each admitted capability against a fixture in the pinned config format (synthetic data only), and test
  that saving it again leaves unrelated sections byte-for-byte unchanged.

## Verify parity by operator action

Use the pinned FreeSense 1.2 Development experience as the baseline for System
and Optional Packages. Record each operator action, its grant, and the observable
effect on the running appliance in the [workflow tracker](planning/workflow-parity.md).
A list view can be verified while its editors, commands, status updates, or recovery
actions remain open. Exercise each migrated action from its normal UI, API, or
console entry point as applicable. Preserve the familiar navigation, labels, and
common page layout across those entry points. Review the pinned page itself
when implementing or revisiting a workflow. Compare its field labels, defaults,
conditional fields, help, navigation, list columns, and row and page actions.
Retain applicable controls and record omissions with their rationale and coverage.
Record intentional consolidation or
retirement with the old callers, the replacement when one is needed, and the evidence
that the workflow is covered.

Select individual PHP checks with repeatable `--php-test` arguments from this
directory. For example, this exercises shared network assignment handling and
the Optional mDNS service's membership check in the same PHP 8.6 runtime.

```sh
python tools/check.py --php-test freesense/tests/MulticastLinksTest.php --php-test freesense-packages/tests/MdnsMembershipTest.php
```

## PHP and domain ownership

- Use FreeSense names for the policy engine, commands and package-owned
  identifiers. Use `fs` for short identifiers such as generated rule tags and
  table names. The shared engine owns rule semantics, ordering, validation,
  activation and rollback. At the FreeBSD boundary use the native names for
  PF syntax, kernel fingerprints, `pfctl` and `/dev/pf`. Preserve names in
  existing data or external protocols when callers depend on them.
- Measure engine optimizations on a representative ruleset. Distinguish
  configuration and apply latency from packet throughput. Verify that the
  running filter, translations and tables still match the confirmed policy
  before authorizing dependent operations.
- Use UTF-8, LF, tabs for first-party PHP, Apache-2.0 headers for new FreeSense
  files, and `declare(strict_types=1)`. Decode HTTP, CLI, XML and process output
  at entry points. Check types before passing values into domain logic.
- Map `FreeSense\` to `/usr/local/FreeSense/lib/` through the same relocatable
  loader in source and installed layouts. Use one PascalCase type per matching
  file, camelCase properties and methods, explicit visibility, native parameter
  and return types, and readonly state when it cannot change.
- Name the runtime and extension packages for PHP 8.6. In FreeBSD ports use
  the `php86` flavor and `USE_PHP` for extensions used by installed code.
  Put test-only modules in `TEST_DEPENDS`. Check resolved package names
  (`php86-ctype`, `php86-dom`, `php86-filter`, `php86-sqlite3`) against the catalogue.
  Use the native unversioned extension identifier inside PHP itself.
- Give each class one responsibility. Pass dependencies through constructors.
  Introduce value objects for invariants and interfaces at interchangeable I/O
  boundaries. Keep standalone calculations pure. Avoid static utility classes,
  global service locators, and interfaces created solely to mirror one class.
- Give each validation, persistence and policy rule an owner. REST, CLI and WebUI
  call that implementation. Enforce the caller's privilege at each entry point,
  before inspecting protected data or launching processes. Escape output for
  its destination and keep secrets out of responses, logs and fixtures.
- Keep OS commands, filesystem access and configuration commits explicit.
  Invoke fixed executables with argument vectors. Test refusal, interrupted
  writes, locking, retries, and diagnostic handling where relevant.
- For a foreground FreeBSD daemon managed with `daemon(8)`, use
  `System\PrivateServiceState` and `System\SupervisedDaemon` for the service
  lock, child PID ownership and termination. Its owning service supplies the
  native readiness check, such as the required sockets and multicast groups.
  Match the executable and argument vector before signaling a process.

For firewall changes, keep policy validation and PF text generation free of
side effects. Give native policy loading and recovery one owner shared by
boot, interface events and administrator actions. Report a policy as active
only after native verification. Preserve configured ordering across all
callers. Put resolved address-alias memberships in the confirmed generation.
Check native table flags and members alongside the filter and NAT streams,
and verify ownership before removing a retired table. The
[firewall plan](planning/firewall-foundation.md) defines the
traffic and recovery checks for admitting a change to enforcement.

## WebUI continuity

Use the pinned Development WebUI as the visual and interaction reference for
each System and Optional Package page. Preserve recognizable menu placement,
page titles, tabs, field labels and order, tables, actions and the steps an
operator takes to finish a task. Reimplement the page with the shared router,
PHP classes, JavaScript client and CSS. Keep validation beside the affected
field and update content in place while preserving the operator's input.

Provide two selectable navigation layouts in **Account > Appearance**:
**Sidebar** and **Top menu bar**. Render both from the same authorized menu
definitions, including installed Optional Package entries and active-page
state. Use one collapsed navigation menu on small screens. Keep the choice
per account. The top menu has its own scroll/fixed and icon-label preferences.

Use a common icon set, theme tokens and page components for System and
Optional Packages. Apply the Dashboard reference's visual language across
page headers, tabs, actions, forms, tables, status badges and cards.
Icons that perform an action need an accessible name. Keep the familiar
page groups and controls so appearance upgrades preserve operator workflows.
The [shell parity plan](planning/webui-appearance.md) lists the original
Appearance controls and how to verify both menu layouts.

For repeated structured rows in System or Optional Package forms, declare a bounded list of records in the command schema. Name the allowed member fields, their types and their individual limits. Use the shared client and server validators for both the outer list and each record. Keep cross-field checks in the shared condition vocabulary when both sides need them. The DNS host override editor shows how additional names use this contract.

Group settings that operators change together. Consolidate duplicate actions
and simplify a page when the existing arrangement makes a task harder to find
or finish. Use the same labels, layout conventions, CSS and components for
forms, tables, actions, status and feedback across System and Optional Package
pages. Preserve access to each supported option when regrouping it.

Register a package's original Services menu entry on its landing page. Use
the package title as the default label. Where the established menu label
differs from the page title, declare `menuTitle` in the package registration.
Build the menu from the pages the signed-in operator can open.

For each ported screen, compare a reference screenshot or the pinned template
with the replacement at the same viewport and exercise the same operator task.
Record intentional visible differences with the reason, including approved
improvements such as inline validation, upload feedback and live statistics.
The guided installer, boot display and fullscreen console have their own
approved designs.

## WebUI page design (2.0)

Binding for every System and Optional Package page. The catalogue, icon table, wireframes and the ranked worst
pages are in [page design](planning/page-design.md). Keep the approved shell, tabs and panel look; upgrade pages.

- Length: a page (each tab) needs at most about two screens of scrolling at 1280x800. Longer: split into tabs
  by task (3 to 7, 1 to 2 words, icon + label, URL fragment, one Save per page), collapse rarely used options
  under `Advanced`, or use a modal editor or wizard. Never nest tabs.
- Panels: icon + H2 title + one-line description, at most 8 rows, at most 3 panels per tab. Group related
  checkboxes under one label. No paragraphs above or between panels.
- Collapsible panels: when a tab still has more than three panels or would run past two screens, make the
  panels an accordion like an FAQ page: the heading bar (icon, title, one-line summary of the current values,
  chevron) is the button; click or Enter/Space opens it. Open by default: the first panel and any panel with
  an error or an unsaved change; the rest closed, remembered per page in the browser. Several may be open
  (not exclusive), plus "Expand all / Collapse all" in the page actions when there are four or more. Closed
  panels still validate and save (never drop a closed panel's values); an invalid field forces its panel
  open and focuses it. Use the shared `accordion`/`panel(title, disclosure)` component only (real
  `<details>`/`<summary>` or button with `aria-expanded` and `aria-controls`), animate only height and
  respect reduced motion. Group `Advanced` options in one closed panel at the end of the tab. Never hide a
  required field or a risk warning inside a closed panel; never nest accordions.
- Motion and feedback: a Save shows a toast (`shared/notify.js`: success closes after about 4 s, errors stay, hover/focus pauses; the `#feedback` line stays the screen-reader announcement). Motion is subtle: only `opacity`/`transform` (and accordion height), 120/180/240 ms from the `--fs-t-*` tokens, nothing above 250 ms, no motion that carries meaning alone, all of it zero under `prefers-reduced-motion`. Pages and tabs rise in, toasts slide, buttons press 1 px; no bounce, parallax or looping animation.
- Help: one-line helper text (110 characters at most); anything longer goes into a `?` popover (keyboard and
  Esc), page help into the header `?`. Warnings that change risk stay visible as one line. Move, never delete,
  the pinned text and note where it went.
- Icons: one Font Awesome 6 name per concept (table in the design doc), registered in `shared/icons.js`, the same
  in menu, tab, panel heading and button. Every panel and tab has one. Field-level icons only for search,
  copy, show/hide, units and validation state. Icon-only controls need an accessible name.
- Rows: every form row is label-left (fixed column), control-right, in the shared grid, in all panels and
  modals; labels go above controls under 720 px. Save lives in the sticky save bar; Cancel/Back then primary,
  right-aligned, one row.
- Lists: toolbar with search and filter chips, at most 7 columns, icon row actions, empty state, paging, filters
  kept in the URL.
- Shared components only: select, multiselect, picker, repeater, table, modal, tabs, popover come from
  `shared/ui.js`. No one-off pickers or selects; add or extend the shared component first.
- i18n and a11y: strings through `message()`, labels fit German at 260 px with wrapping, AA contrast in dark,
  light and six accents, theme tokens only, `aria` for tabs, popovers and errors.
- Tests and evidence: each page type has a browser test for label/control alignment (1 px), no overflow at 360
  and 1280 px, tab height budget, and icon/name presence. Capture screenshots in Sidebar and Top menu layouts,
  desktop and mobile, under `test-lab/`, and note them in the page's planning note.
- A `partial` page that gains fields is designed in the page design doc first, so it stays short.

## Web forms and AJAX

- Give the entire System and admitted package pages clean URL paths in one
  management router. Register each page, HTTP method, operation, privilege
  and query/body contract explicitly. A feature registers its paths, public
  files, grants and commands in its `lib/Http/<Feature>/<Feature>Routes.php`
  (`FeatureRoutes`); the route names are the cases of `ManagementRoute`, and
  library folders match namespaces. Keep handlers and templates outside
  the public document root. Public assets have explicit paths. Fresh pages
  must support direct links, refresh, browser history and AJAX navigation.
- Use an authenticated WebSocket for live statistics when a subscription
  is available. Authorize the stream to the same scope as its HTTP snapshot,
  recheck access during the connection, and release subscriptions on
  navigation, logout and disconnect. Keep a usable HTTP snapshot when the
  socket is offline. Keep credentials out of URLs and browser persistence.
- For an appliance LAN listener, bind the TLS address, allowed client,
  firewall permit and request authority to the same approved configuration.
  Run HTTP handling as an unprivileged account. Route protected reads through
  the management broker, which rechecks the current credential and exposes only
  the registered operation. Exercise the installed path from a separate peer.

- Organize public JavaScript and CSS under `www/assets/<area>/`. Use
  `shell/` for the app entry point and its site-wide styling, `shared/` for
  code used across features (including the AJAX client and validators), and
  the owning navigation area such as `firewall/`, `diagnostics/` or `status/`
  for page modules and feature styles. Name files for their purpose in
  lowercase kebab-case. Keep a feature's helpers together until another
  feature uses them.
- Optional Packages put their own browser assets under
  `www/assets/packages/<registered-package-id>/` and reuse System modules.
  When moving or adding assets, update module imports, shell references,
  the router's explicit asset permissions, the owning package manifest and
  focused browser checks together.

- Use native browser AJAX (`fetch`) for all WebUI form submissions and API
  calls. Route requests through a shared client so authentication, CSRF
  protection for state changes, response handling and field errors work the
  same way across pages.
- These rules cover every Optional Package that offers a management page.
  Register its clean page, read operation, permissions and commands with
  the System router. Put providers outside the document root. Use the
  System's `/assets/shared/commands.js` for single settings, multi-field edits
  and file transfers. Keep each form inert until its handler is registered
  and enforce `form-action 'none'` in the management response.
- Send JSON settings and multipart file uploads with the registered command
  identifier. Authenticate and authorize prior to parsing. Validate the
  method, exact origin, encoding, bounded size, allowed fields, field
  types, file count and actual file contents on the server. Check edit
  revisions and current privileges under the owner's write lock. A registered
  package command receives the authorized DOM under the System configuration
  lock. Edit that DOM directly for a settings save. Keep native service
  reconciliation in a separate command that reads the saved settings.
- For each file field define accepted file count, per-file bytes, total
  bytes, filename length and whether empty files are valid. Specify filename
  rules, duplicate handling and content validation for the operation.
  Stream opaque files and prevalidate every batch member before modifying
  managed state. Exercise exact limits, one unit over, concurrent changes
  and interruption during a batch. For text specify bytes and, when needed,
  Unicode character limits. Repeated values have a count limit and explicit
  ordering and uniqueness semantics.
- Forms may mix single-file and multi-file controls. Use `maxUploadBytes`
  when several controls share a byte budget. Count the actual bytes across
  those controls before calling the provider. Size the complete HTTP request
  from its registered fields, including JSON escaping and multipart framing.
  Check that a malformed package registration leaves other forms available.
  Run the Optional Packages registration test against every shipped management
  manifest and provider. Unknown properties, including misspelled size limits,
  must fail that check.
- Define field types, constraints and cross-field rules centrally for each
  operation. Reuse those definitions for client-side form feedback and
  server-side validation. The server validates every request before applying
  changes, including calls made directly to an endpoint. Keep authorization
  and data-dependent checks on the server.
- Declare an explicit boolean `required` on each submitted field. Exercise
  the actual System registry and every shipped package descriptor against the
  validator, including paths, permissions and request limits.
- For network fields select a specific host, interface CIDR or network
  format and the accepted family. Reuse the common hostname, MAC and bounded
  integer checks. Register relationships such as a conditionally required
  or forbidden setting, ordered numeric or IP interval, or on-link gateway
  on the operation. Static IPv6 routing uses distinct LAN and WAN /64s and
  a WAN-scoped link-local or on-link unicast gateway. Reuse those checks
  in Optional Packages when they accept the same fields. Compare IP ranges
  in network byte order across their full address width. An operation can
  derive its schema from the authorized configuration, as the LAN DHCP
  editor does for its pool bounds.
  Recheck those constraints against the current configuration under the
  writer lock and verify live prerequisites when activating the service.
  Add shared valid and invalid fixtures for every new field rule.
- Return consistent field and form errors that the client can display next to
  the relevant inputs. Exercise both browser submissions and direct requests
  with invalid, missing and unexpected values.
  Use `Validation\InvalidField` for an operator-facing error tied to a
  registered field. Keep native process diagnostics and protected values
  out of that message. Verify the response identifies the field and the
  shared browser client marks its control invalid.

## Scripts, packages and tooling

Record interpreter, caller, arguments, exit status, privileges, startup phase,
and failure behavior before replacing a script. Check rc, cron, pkg, console,
operator documentation and dynamic registrations before retiring it. Retain a
small POSIX sh entry point where FreeBSD requires one. Early boot and rescue
may rely only on tools available at that stage. Use task-specific environment
variables in ports. Keep System and Optional Packages independent.

Every admitted System file needs a matching install path in the sibling
System port manifest. Record REST route privileges and operations with their
handlers. Enforce shared rules in local tools. Keep the workspace free of
generated bundles, release credentials, VM captures and production data.
