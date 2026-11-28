# Local checks

Run `python tools/check.py` from the refactor root with Python 3.12 or later.
Every run also fails on CRLF line endings and on a library file whose namespace or type name does not match its folder
and file name (`.gitattributes` keeps checkouts LF).
When to run what is set out in [How much to test](../AGENTS.md#how-much-to-test). Single tests: one `--php-test`
per file; a selection can span System and Optional Packages, and each test runs in a fresh PHP process.

## Jobs side by side

Browser suites, PHP, shell, Python and npm checks run as jobs. Every job reports: one line when it finishes, the full
output of each failure after the last one, and a non-zero status. `--jobs N` or `FREESENSE_CHECK_JOBS` sets the number
(default: a quarter of the processors, limited to one per 0.7 GB of free memory, 2 to 12; 1 runs one at a time).
PHP lint and tests run up to 12 processes inside their container (`FREESENSE_PHP_JOBS`), each with its own temporary
directory; native appliance tests always run one at a time. Job times are kept under `local-output/check-state/` so the
longest start first.

Containers read the workspace from a throwaway Docker volume (`snapshot.py`) taken at the start of the run, not from a
Windows bind mount, so a file edited during a run is not seen by that run's PHP processes. A run that is killed can
leave a `freesense-check-*` volume behind (`docker volume rm`).

`php-imports.php` reports a class name that resolves into `FreeSense\` but that no shipped file declares (a forgotten
`use`).

## Lab acceptance

`python tools/lab_accept.py` builds both System packages from the tree on the builder, updates Proxmox VM 150
(`--fresh`: new install; `--checks-only`: no deploy), compares every installed System file with the tree, reboots, runs
`tests/appliance`, and checks traffic, DHCP, DNS, WAN default deny, WebUI sign-in and the system log from VM 152. It
stops both VMs afterwards (`--keep` leaves them) and writes its report to `local-output/lab-accept/`. It does not cover
the installer media, upgrades between releases, Optional Packages, IPv6, VPN, PPP, multi-WAN, HA or the browser pages.

Use `--component freesense` to check a single admitted component. System and
System-ports selections both reconcile the installed System files with the
sibling port's manifest. The runner also verifies the local inventory.

Portable PHP checks use PHP 8.6 CLI and sh from PATH. On a host without PHP, Docker
can run the locally available `php:8.6.0-cli` image with networking disabled.
The command stages a temporary component tree, lints PHP entry points, detects
PHP diagnostics, and runs each standalone `tests/**/*Test.php` in its own
process. System types also load through their installed path in a separate
staging directory. The System port's file installer stages the exact manifest
into this temporary directory. The runner compares file modes and SHA-256
digests, then exercises the installed console CLI against a disposable config.
The port expects `FREESENSE_REFACTOR_SRC` to name the absolute admitted
`freesense/src` path. The appliance and this port use `PREFIX=/usr/local`.
An incorrect PHP version fails verification.

System sources and Optional package packing lists must keep PHP handlers outside
`/usr/local/www`. The check also inspects package source payloads for executable
PHP and direct form submissions. To scan the complete staged document root
after substitutions, run `python tools/public_php.py STAGEDIR`.

On FreeBSD run `php tools/php-ports.php /path/to/ports` to evaluate the actual
System and Optional port Makefiles and their PHP providers. The check reads
make variables from the supplied FreeBSD ports tree. It requires the real
`Mk` framework and each PHP provider origin. At ports revision
`68edce2ee778a82cc41138f080f66bc5b43cfbcc`, those providers resolved to the
8.6.0 RC4 package revision. The separately verified appliance CLI is PHP 8.6.0
final. The two versions describe different inputs.

| Installed caller | PHP facility | Direct FreeBSD package |
|---|---|---|
| System console, CLI entry points, package file manager | CLI and built-in filesystem, SPL, hash, JSON and PCRE functions | `php86` |
| `Auth/ApiIdentity.php`, `Network/Interfaces/InterfaceSnapshot.php` | Character classification | `php86-ctype` |
| `Config/ConfigurationDocument.php`, `Auth/ApiIdentity.php` | DOM and XPath; the interpreter supplies libxml support | `php86-dom` |
| `Network/Interfaces/LanAddress.php`, `Firewall/PolicyRule.php` | IP address validation | `php86-filter` |
| `Network/Gateways/GatewayQualityHistory.php` | Bounded gateway measurements and rollups | `php86-sqlite3` |
| Optional TFTP commands and node exporter `Settings.php` | Receive an authorized DOM snapshot and persist exporter settings with XPath | `php86-dom` |
| `tools/php-check.php` | Test-only source tokenization | `php86-tokenizer` |

The QEMU guest integration and AdGuard Home binary have no direct PHP runtime
requirement. Add the extension to the consuming port when admitting a new caller.

Run `python tools/check.py --appliance` on the designated local FreeBSD guest,
or set `FREESENSE_TEST_GUEST` on the host to that guest's existing SSH alias.
Set `FREESENSE_TEST_SSH_CONFIG` to a dedicated SSH config file when the alias
is managed separately from your personal SSH configuration.
Set `FREESENSE_TEST_PHP` to an absolute guest PHP CLI path to test a separately
installed interpreter. The installed command's shebang probe always uses the
appliance path `/usr/local/bin/php` and reports that version separately.
SSH uses batch authentication and a known host key. The guest requires PHP 8.6
CLI and tar. The runner transfers the staged sources and tests to a temporary
directory and executes the portable suite followed by `tests/appliance/**/*Test.php`. Appliance tests own
their hardware and service assertions. A missing guest, prerequisite, or test
returns a failure. Installer and update acceptance require their own tests as
those components enter the workspace.

Shell checks use the matching local interpreter and ShellCheck, or the locally
available PHP CLI and `koalaman/shellcheck:stable` Docker images. Python
components use standard unittest discovery.

The System gate opens a real headless Chromium browser through Python Playwright.
It uses installed Chrome on Windows and Playwright's installed Chromium on other
hosts. Set `FREESENSE_BROWSER_CHANNEL` to another installed Chromium channel when
needed. The browser fixtures cover clean URLs, partial navigation, reconnects,
JSON commands, uploads, field errors and the prohibition on native form submission.
With the TFTP and node exporter registrations present, the gate also opens the real PHP
8.6 controller and provider using the cached `php:8.6.0-cli` image. Both the
configuration and TFTP files live on disposable container filesystems. Chromium
saves an edited firewall draft, imports JSON, uploads binary data and verifies
a stale revision refusal, confirmed replacement and confirmed removal. It also saves the enabled state, port and collector choices through the exporter form and checks the stored XML. The
standalone `python -B tools/browser_management.py --evidence <lab-directory>`
command saves the real firewall editor and Optional file manager screenshots.
Projects with a package.json use their declared local validation, test, link
and version checks with npm and previously installed dependencies. Existing
generated output can also be checked independently with
`python tools/site_links.py <dist> --redirects <rules-file> --functions <handlers-dir>`.
Use the optional flags when the site defines redirects or concrete Pages Functions.

For a pristine, dedicated FreeBSD lab guest with PHP installed, run
`python -B tools/bootstrap.py --guest <alias> --ssh-config <lab-config>`.
The bootstrap stages and verifies the paired System manifest, installs the
reviewed files, enables the configuration rc service and checks initialization.
On repeat it verifies its installation receipt and preserves the configuration.
It operates on an existing FreeBSD filesystem. Image creation and partitioning
are separate stages.
