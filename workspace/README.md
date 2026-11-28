# FreeSense 2.0 local workspace

This is the fresh-install PHP 8.6 refactor. System, System ports, [Optional Packages](freesense-packages/README.md), OS-base,
the security site's advisory parser and the public website's release handlers
retain their own directories. Follow [AGENTS.md](AGENTS.md)
for admission, architecture and script ownership. The
[capability ledger](planning/ledger.md) records verified behavior and gaps.
The [roadmap](planning/roadmap.md) covers the whole appliance and project estate.
The [firewall foundation plan](planning/firewall-foundation.md) defines the next
milestone and how to prove it with traffic between two hosts.

The installed amd64 appliance now serves a LAN DHCP lease, resolves DNS
and forwards IPv4 through NAT after a cold boot. Its console shows the
assigned interfaces, routes and local Fleet status. Boot animation (local evidence)
and installed console (local evidence)

Physical console option 3 pairs a browser for read-only LAN status over HTTPS.
See [pairing, certificate verification and recovery](planning/lan-https.md).

Run the local checks from this directory.

```sh
python tools/check.py
python tools/check.py --component freesense
python tools/inventory.py --verify-sources --workspace ../..
```

The default check uses Python 3.12 or later. The portable PHP checks can use
the locally cached Docker image `php:8.6.0-cli` and the shell checks can use
the cached ShellCheck image. For native acceptance run
`python tools/check.py --appliance` on FreeBSD or set `FREESENSE_TEST_GUEST`
to an SSH alias for the isolated lab guest. `FREESENSE_TEST_SSH_CONFIG` can
select a private SSH config. The runner stages files in a temporary directory.
On the FreeBSD 16 test guest, the native firewall parser requires the PF
kernel module even for a parse-only check. Load the module for the test boot
with `kldload pf` if it is absent. Filtering can stay disabled.

## Configure and check firewall policy

With one assigned and enabled LAN in the fresh 2.0 configuration, run the
following from the staged System or an installation containing the new CLI.
The command prints a PF candidate. It performs zero configuration writes and
does not invoke PF.

```sh
printf '%s\n' '[{"action":"pass","source":"192.0.2.10","destination":"198.51.100.20"},{"action":"block","source":"192.0.2.0/24","destination":"any"}]' | /usr/local/sbin/freesense-firewall preview
```

The first matching rule wins for traffic in the selected IP family entering LAN. The example lets
one client reach one host and blocks the rest of its subnet. The candidate
starts with a default block for both directions and both address families.
An allowed ingress tags the packet so a forwarded connection can leave the
firewall and receive a reply. Each rule requires `action`, `source` and
`destination`. Set `family` to `inet6` for IPv6. An omitted family or
`inet` selects IPv4. Optional `protocol` values are `any`, `tcp`, `udp`,
`tcp/udp` and `icmp`. For ICMP, supply `icmp_types` as a list of unique
integers from 0 through 255 to restrict the matched types. The IPv6
compiler emits ICMPv6 selectors. For TCP and UDP, supply `source_port` or `destination_port`
for a single port and a corresponding `_port_end` field for an inclusive
range. Ports are integers from 1 through 65535. Addresses may be `any`, a
numeric host or an aligned prefix in the selected family. IPv4 source NAT
has its own settings. Input is limited to 128 rules
and 64 KiB. An invalid rule rejects the complete preview. This first version expects an
unshared LAN assignment with a conventional FreeBSD device name.

| Local administrator command | Result |
| --- | --- |
| `freesense-firewall list` | Returns the saved rule revision and the ordered records, including disabled rules. |
| `freesense-firewall save REVISION < rules.json` | Replaces the ordered rules under the configuration lock. Include a Boolean `enabled` on each record. Omit `id` to add a rule, retain its ID to edit or move it, or omit the record to delete it. A conflicting revision exits with status 75. |
| `freesense-firewall nat` | Returns the configured IPv4 outbound NAT mode and its edit revision. Existing configurations without a NAT section default to disabled. |
| `freesense-firewall nat-set REVISION automatic` | Translates the static LAN subnet to the address on a distinct, enabled WAN. Use `disabled` to turn it off. |
| `freesense-firewall preview-saved` | Prints the combined NAT and filter candidate for the current interface assignments. |
| `freesense-forward list` / `freesense-forward replace REVISION` | Reads or replaces the ordered static WAN port-forward draft. Use the normal firewall check and apply commands to activate it. |
| `freesense-firewall check` | Parses that candidate with FreeBSD PF and returns its SHA-256 and both configuration revisions. |
| `freesense-firewall apply CHECKED_SHA256` | Applies the exact checked candidate as local root. Records intent first, then verifies native filter and NAT readback before confirming. The first activation requires a disabled, empty PF. |
| `freesense-firewall status` | Shows the configured, active, last-confirmed, previous and pending digests together with the local kernel state. |
| `freesense-firewall rollback ACTIVE_SHA256` | Restores the previous confirmed policy while preserving saved edits and unrelated settings. |
| `freesense-firewall recover` | Reconciles an interrupted load or restores the last confirmed policy to an empty PF. Refuses an unknown active ruleset. |

The native operations are local administrative commands. The isolated
traffic test exercised apply, rollback, and recovery from an empty kernel
with a WAN peer checking the source of each connection. Acceptance log (local evidence)

On a System containing the routing service, assign and address a distinct
LAN and WAN and confirm the firewall policy first. Set
`gateway_enable="NO"`, `pf_enable="NO"` and `freesense_routing_enable="YES"`
in rc.conf so FreeSense owns IPv4 forwarding and PF startup. Use
`service freesense_routing start` to recover the confirmed policy, verify
both interfaces and enable forwarding. A DHCP WAN arms a supervised observer
that resumes forwarding after a verified late lease or renewal and suspends it
when ownership, lease or native policy is lost. See the [WAN lease checks](planning/wan-lease.md). Option 12 in the physical console
shows the active generation and offers rollback or recovery as appropriate.
Routing service traffic test (local evidence)

## Fresh-install configuration

The local amd64 UEFI installer has a guided wizard for monitor and serial,
and an unattended mode bound to a machine UUID and a physical disk ID. The
installed System has a boot progress screen and a full-screen appliance
overview. Read the [installation and provisioning guide](planning/guided-installation.md)
for the USB file format, console controls and current feature coverage.
The [payload preparation notes](planning/installer-payload.md) describe the base image.

On the local FreeBSD installation, `freesense-config init` initializes
`/cf/conf/freesense/config.xml` with an empty assignment list. It validates
existing settings on subsequent runs. To enable this initialization at boot,
set `freesense_config_enable="YES"` in rc.conf. The
`FREESENSE_CONFIG_DIR` process environment variable selects an alternate
absolute directory for isolated tests.

```sh
umask 077
freesense-config backup > my-config.xml
freesense-config restore < my-config.xml
```

Backups use a UTF-8 `<freesense format="2.0">` document. A restore validates the
document before replacing the file. Restore and initialization require access
to the private config directory. A full restore replaces the entire document.

## Local management interface

On a staged installation, start the PHP 8.6 management router and open
`http://127.0.0.1:8080/` on that machine. Enable the REST API and use an
API key with the appropriate direct grants. The fresh configuration starts
with zero accounts and keys. The browser keeps the key in memory until
sign-out or refresh.

```sh
/usr/local/sbin/freesense-web 8080
```

| Clean URL | Operation |
| --- | --- |
| `/overview` | Inspect the appliance's assigned interfaces and subscribe to live traffic counters when the service is enabled. |
| `/interfaces` | Read current interface details with a status grant. |
| `/services/dns` | Inspect the resolver enable setting and ordered upstreams. A resolver editor can save up to four unicast IPv4 forwarders or clear them for local-only answers. |
| `/firewall` | Read ordered rules or outbound NAT with the corresponding grant. Authorized editors can change one NAT setting, edit and reorder several rules or upload a JSON rule file. These operations save the draft. Apply a checked policy locally. |
| `/firewall/aliases` | Manage [reusable host and network groups](planning/firewall-aliases.md) for rule sources and destinations. |
| `/firewall/nat/port-forwards` | Create, edit, enable, reorder or remove [static IPv4 WAN port forwards](planning/port-forwards.md) with the applicable NAT grant. |
| `/packages/mdns-bridge` | Configure discovery on selected internal networks when the Optional Package is installed. Use [local multicast rules](planning/local-multicast-rules.md) on the firewall page to approve its traffic. |
| `/packages` | Discover installed management providers visible to this credential. |
| `/packages/tftp` | With the TFTP file-management package staged, list files, upload a batch of up to eight files or confirm removal. Read and write access are checked independently. |

System and Optional Package forms use the same native JavaScript command
client. It reads operation descriptors from `GET /api/v1/forms`, reports field
errors and uses JSON or multipart uploads. The server checks authorization,
the expected revision and actual input before writing. Its content security
policy blocks ordinary HTML form submissions. Package PHP providers run
outside the document root. [Management and package checks](planning/management.md)

The listener supports newly installed package forms immediately. PHP checks
each command's own request and upload limits. For TFTP the limit is
64 MiB per file and per batch, with filenames of up to 128 ASCII bytes.

For the local loopback runner, give the router and socket service the same
`FREESENSE_WEB_ORIGIN=http://127.0.0.1:8080`, and set `FREESENSE_LIVE_DIR`
to an existing private mode-0700 directory owned by their service account.
Start `/usr/local/sbin/freesense-live-stats` alongside the router. Its
default loopback port is 8765. Set `FREESENSE_LIVE_PORT` in both processes
to change it. The HTTP snapshot remains available when the stream is offline.
On the installed appliance, console option 3 sets up the LAN HTTPS listener
and its live status worker together. A paired browser connects to the same
address and port for snapshots and live data. Broader LAN administration,
account provisioning and browser login sessions remain open.
