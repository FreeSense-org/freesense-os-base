# FreeSense notices

FreeSense is licensed under the Apache License 2.0 unless a file says otherwise. This file credits third-party
work that FreeSense includes or was derived from. The rule for 2.0: where we can write our own version with the
same function, we do, and the entry moves from "kept" to "replaced". Licence headers in kept files are never removed.

## FreeBSD
FreeSense is built on FreeBSD (BSD licences). The base OS is FreeBSD source at a pinned commit plus the FreeSense
change-set in `freesense-os-base/patches`.

## Kept for now (to be replaced where feasible)
| Component | Origin | Why kept | Plan |
| --- | --- | --- | --- |
| `if_ovpn` OpenVPN DCO kernel driver (`sys/net/if_ovpn.c`, `if_ovpn.h`, `nv_namespace.h`, man page, tests) | Rubicon Communications, LLC (Netgate), BSD-2-Clause | OpenVPN data-channel offload; headers kept as required | Re-evaluate against the FreeBSD tree at each pin; if upstream ships it, drop the patch; otherwise write our own |
| `copy_configxml_from_usb`, `fix_fstab` installer helpers | Rubicon Communications, LLC, Apache-2.0 | derived files | Rewrite as FreeSense installer scripts (M4) |
| pf test notes in `tests/sys/netpfil/pf/pfsync.sh` | Rubicon Communications, LLC | test-only comments | Drop the patch hunk (M1) |

## Already replaced in 2.0
- Netgate hardware driver (`sys/dev/netgate`): removed.
- `pfSense` kernel configurations: replaced by `FREESENSE` and `FREESENSE-DEBUG`.
- Installer copyright and trademark banner: replaced by a FreeSense notice.
- Boot splash, loader menu and installer images: replaced by FreeSense images rendered from `assets/branding` (`freesense-os-base/boot/make_boot_images.py`); the FreeBSD logo files are removed.
- Release configuration names, installer menu text and rc hook paths: renamed to FreeSense.

## Behaviour reproduced from pfSense / FreeSense 1.2 (own code)
- The automatic firewall rule set (default deny, link-local and port 0 blocks, anti-spoofing, bogon and private network
  blocks, static route bypass, the firewall host's own outbound passes) follows the behaviour of `filter.inc` of pfSense CE /
  FreeSense 1.2 (Rubicon Communications, LLC (Netgate) and Electric Sheep Fencing, Apache-2.0). FreeSense 2.0 writes these rules in
  its own classes (`Firewall/Automatic/HostDefaults`, `Firewall/Automatic/BogonTables`, `Firewall/Automatic/ReservedNetworks`, `Firewall/Automatic/LoginProtection`); no
  code was copied. Comparison: `planning/default-rules.md`.
- Bogon lists: the shipped initial lists are the IANA special-purpose address blocks; the full lists are downloaded at run time
  from Team Cymru (https://www.team-cymru.org/Services/Bogons/), as FreeSense 1.2 did. They are not part of the package.

## Third-party contributions fetched at build time
- intel-ipsec-mb (BSD-3-Clause) and NASM (BSD-2-Clause), pinned in `freesense-os-base/build/lock.json`.

## Vendored web components
| Component | Origin | Licence | Files |
| --- | --- | --- | --- |
| Bootstrap 6.0.0-alpha.2 (compiled CSS and the JavaScript bundle; release asset `bootstrap-6.0.0-alpha.2-dist.zip`, SHA-256 `42fdeae57a261e62b2d459030c65e1cd2ae7ecd0c831abaf4f2a5c138bc80cc1`, pinned exactly; the alpha changes between releases, so a bump re-runs the spike checks in `planning/bootstrap6-spike.md`) | The Bootstrap Authors, https://getbootstrap.com | MIT (`third-party/bootstrap/LICENSE`, `bc0cd18d94c028f1c0b62ec9fdaddcb9921c1e07c29cf52dc7df8cca9daf2488`). The Bootstrap documentation (CC BY 3.0) is not shipped. The bundle includes Floating UI (`@floating-ui/dom` 1.8.0, MIT, `third-party/bootstrap/LICENSE-floating-ui.txt`, `0e4c9a9b6c71019cbbea3bdc20b01223110a9035700f9c960c8fcbf78c2325ce`) and vanilla-calendar-pro 3.4.0 (MIT, `third-party/bootstrap/LICENSE-vanilla-calendar-pro.txt`, `b95f659670e7f7af29dedc3e69c1deaeee1267757729ad391318aba94108774d`); both texts are from their npm packages | `freesense/src/usr/local/www/assets/third-party/bootstrap/`: `bootstrap.min.css` (`d0120ad0f5c4dbe2d4d9b2728746c47b035116ba0570c6688cd798d9deaa4304`), `bootstrap.bundle.min.js` (ES module, `a7d7603683cab1987b00eba0b4ed9a16c4873ec238b190c7022b39f3743f31b2`). Browser floor: Chrome 138, Firefox 147, Safari 26 (CSS `light-dark()`, `oklch()`, relative colours, `interpolate-size`). Four small SVG masks in `assets/shell/icons/` (our own files) replace Bootstrap's `data:` images, which the content policy blocks. The JavaScript plugins in use (Dialog, Drawer, Menu, Popover, Tooltip) are reached only through `assets/shared/ui.js`; only Chromium is tested, and each upstream pre-release is re-checked (CSP sweep, screenshot matrix, component checks) before it replaces this one. The previous Bootstrap 5.3.8 state is kept locally for rollback (outside the repository) |
| Roboto (Latin subset, weights 300/400/500/700 and 400 italic; fontsource package @fontsource/roboto 5.3.0, npm tarball SHA-256 `77620f437249772e381698224ea1bddd01c985ecd60267a63ddc96b9eb9348f7`) | The Roboto Project Authors, https://github.com/googlefonts/roboto-3-classic; packaged by https://fontsource.org (https://registry.npmjs.org/@fontsource/roboto/-/roboto-5.3.0.tgz) | SIL Open Font License 1.1 (`third-party/roboto/LICENSE.txt`) | `freesense/src/usr/local/www/assets/third-party/roboto/`: `roboto-latin-300-normal.woff2` (`299f10a52fe1423dd5579ff7e83db8dbea312f1e924f3e06e55839286b4d1c1d`), `roboto-latin-400-normal.woff2` (`425c0713a8176f92273d378599c7eac57de7fafabd4bd0ed457b70eb8f80d371`), `roboto-latin-400-italic.woff2` (`5d0e978b6560631da2504e1612e91ee1ff959c04adae245eb320ac90cad2ba2f`), `roboto-latin-500-normal.woff2` (`5bcc3aa180e7f26f643cd5b2621cd7c2de193d0661d913a94afd3d4881a7a34b`), `roboto-latin-700-normal.woff2` (`b9d66d1708156f765ada51939bc24ed259dafa69eb631b36e443680fe9e15879`), `LICENSE.txt` (`84044c09058803d87a241bd28abb4ce30a433d7db9c27a04da3efad6a0ca539c`) |
| Font Awesome Free 6.7.2 (all.min.css and the solid, regular and brands woff2 files; release zip SHA-256 `ecdaaa6d347cd7da82c66054770995e97f3d066a57e8d58ac9c517f0f77561fb`) | Fonticons, Inc., https://github.com/FortAwesome/Font-Awesome (release 6.7.2, `fontawesome-free-6.7.2-web.zip`) | Fonts SIL OFL 1.1, icons CC BY 4.0, code MIT (`third-party/fontawesome/LICENSE.txt`, `9b914eae88817d63b576eab5aafde7068c7a1abae125d7cdfb034f1da43a9afc`) | `freesense/src/usr/local/www/assets/third-party/fontawesome/`: `css/all.min.css` (`74005d7c17d4a02f2f25404ec0655d9bc2fdaa53166874c87d7b7eec69d9088a`), `webfonts/fa-solid-900.woff2` (`aa75998623a391e61c6901794ace832e3ecdd288b56d608f21bea0411acc0b8e`), `webfonts/fa-regular-400.woff2` (`e3456d1283b9d75337a773dfd147bf908fd02c01b4bf48576d8603a69b13cbe5`), `webfonts/fa-brands-400.woff2` (`d7236a19bf23cbb2027280e8f51dc99d6c45976a2ed60de73382b034b18a2b68`) |
| uPlot 1.6.32 (`uPlot.esm.js`, `uPlot.min.css`, `LICENSE.txt` from the npm tarball https://registry.npmjs.org/uplot/-/uplot-1.6.32.tgz; used by `assets/shared/live-chart.js`) | Leon Sorokin, https://github.com/leeoniya/uPlot | MIT (`third-party/uplot/LICENSE.txt`, `8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd`) | `freesense/src/usr/local/www/assets/third-party/uplot/`: `uPlot.esm.js` (`5dd9b3281aa64b461b42d9945f6adb2649d346502b12281a9ae0d46599a80eba`), `uPlot.min.css` (`df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04`) |

The FreeSense look is our own `assets/shell/theme.css` on top of it; `assets/shared/ui.js` is the only file that names Bootstrap classes (and `assets/shared/bootstrap.js` the only module that imports its JavaScript bundle), and `assets/shared/icons.js` is the only file that names Font Awesome classes. Fonts load from this origin only (`assets/shell/fonts.css`; the Font Awesome stylesheet is unmodified).
