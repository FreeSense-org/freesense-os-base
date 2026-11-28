"""Drive the real System router and installed Optional package providers on isolated local fixtures."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import SimpleQueue
import secrets
import struct
import subprocess
from threading import Thread, Timer
import time
from xml.etree import ElementTree

from browser_shell import sign_out, menu_link, fill_api_key, nav_link
from playwright.sync_api import expect, sync_playwright

from browser_aliases import exercise_aliases, exercise_alias_authority
from browser_fleet import exercise_fleet
from browser_fleet_controller import exercise_fleet_controller
from browser_nut import exercise_nut
from browser_ups_widget import exercise_ups_widget
from browser_config_history import exercise_config_history
from browser_dhcp_leases import exercise_dhcp_leases
from browser_static_routing import exercise_static_routing
from browser_dnssec import exercise_dnssec
from browser_admin_access import exercise_admin_access
from browser_appearance import exercise_appearance
from browser_menu_search import exercise_menu_search
from browser_account_menu import exercise_account_menu
from browser_account_profile import exercise_account_profile
from browser_account_language import exercise_account_language
from browser_admin_key_issuance import exercise_admin_key_issuance
from browser_personal_key_issuance import exercise_personal_key_issuance
from browser_system_api_keys import exercise_system_api_keys
from browser_account_security import exercise_account_security
from browser_dashboard_system import exercise_dashboard_system
from browser_dashboard_preferences import exercise_dashboard_preferences
from browser_dashboard import exercise_dashboard
from browser_dns_access import exercise_dns_access
from browser_virtual_ips import exercise_virtual_ips
from browser_port_aliases import exercise_port_aliases
from browser_one_to_one import exercise_one_to_one
from browser_outbound import exercise_outbound
from browser_port_forwards import exercise_port_forwards
from browser_local_rules import exercise_local_rules
from browser_dns_lookup import exercise_dns_lookup
from browser_ping import exercise_ping, exercise_traceroute
from browser_ingress_rules import exercise_ingress_rules
from browser_interface_groups import exercise_interface_groups
from browser_floating_rules import exercise_floating_rules
from browser_group_rules import exercise_group_rules
from browser_opt_assignments import exercise_opt_assignments
from browser_ra import exercise_ra
from browser_ntp import exercise_ntp
from browser_ppp import exercise_ppp
from browser_dhcp_mappings import exercise_dhcp_mappings
from browser_multicast_bridge import exercise_multicast_bridge
from browser_softflowd import exercise_softflowd
from browser_andwatch import exercise_andwatch
from browser_services import exercise_services
from browser_net_snmp import exercise_net_snmp
from browser_bind import exercise_bind
from browser_bind_widget import exercise_bind_widget
from browser_telegraf import exercise_telegraf
from browser_zabbix_agent import exercise_zabbix_agent
from browser_avahi import exercise_avahi
from browser_dns_hosts import exercise_dns_hosts
from browser_traffic_totals import exercise_traffic_totals
from browser_mdns_bridge import exercise_mdns_bridge
from browser_diagnostic_states import exercise_states
from browser_kernel_routes import exercise_kernel_routes
from browser_gateway_status import exercise_gateway_status
from browser_gateway_groups import exercise_gateway_groups
from browser_remote_syslog import exercise_remote_syslog
from browser_filter_log_summary import exercise_filter_log_summary
from browser_log_storage import exercise_log_storage
from browser_open_vm_tools import exercise_open_vm_tools
from browser_default_rule_log import exercise_default_rule_log
from browser_lldp import exercise_lldp
from browser_log_display import exercise_log_display
from browser_filter_log import exercise_filter_log
from browser_udp_relay import exercise_udp_relay
from browser_ipv6_settings import exercise_ipv6_settings
from browser_tftp_settings import exercise_tftp_settings
from browser_tftp_backup import exercise_tftp_backup
from browser_wireguard import exercise_wireguard

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "php:8.6.0-cli"
TFTP = "ftp/FreeSense-pkg-tftpd/files/usr/local"
EXPORTER = "sysutils/FreeSense-pkg-node_exporter/files/usr/local"


def docker(*arguments, **options):
    return subprocess.run(["docker", *arguments], check=True, capture_output=True, **options).stdout


# One long-lived PHP in the container forwards length-framed requests, each on its own loopback connection. Starting a
# process per request (docker exec) cost about 0.1 s each and was most of this check's run time.
RELAY = (
    'while (strlen($head = (string)stream_get_contents(STDIN, 4)) === 4) { '
    '$request = (string)stream_get_contents(STDIN, unpack("N", $head)[1]); '
    '$s = stream_socket_client("tcp://127.0.0.1:" . $argv[1], timeout: 3); if (!$s) exit(1); '
    'stream_set_timeout($s, 12); fwrite($s, $request); $response = (string)stream_get_contents($s); fclose($s); '
    'echo pack("N", strlen($response)), $response; fflush(STDOUT); }')


class Relays:
    """A few forwarding processes, so concurrent browser requests do not queue behind each other."""
    def __init__(self, container: str, port: int, count: int = 6):
        self.command = ["docker", "exec", "-i", container, "php", "-r", RELAY, str(port)]
        self.free = SimpleQueue()
        for _ in range(count):
            self.free.put(None)  # started on first use

    def send(self, request: bytes, timeout: float = 15) -> bytes:
        process = self.free.get()
        try:
            if process is None:
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            watchdog = Timer(timeout, process.kill)
            watchdog.start()
            try:
                process.stdin.write(struct.pack(">I", len(request)) + request)
                process.stdin.flush()
                head = process.stdout.read(4)
                length = struct.unpack(">I", head)[0] if len(head) == 4 else None
                response = b"" if length is None else process.stdout.read(length)
            finally:
                watchdog.cancel()
            if length is None or len(response) != length:
                raise RuntimeError("The request relay into the test container stopped or timed out.")
            return response
        except BaseException:
            if process is not None:
                process.kill()
                process = None
            raise
        finally:
            self.free.put(process)

    def close(self) -> None:
        while not self.free.empty():
            process = self.free.get()
            if process is not None:
                process.stdin.close()
                process.wait(timeout=10)


class LoopbackRelay(BaseHTTPRequestHandler):
    """Forward the original HTTP request into the test namespace without exposing a Docker network port."""
    def log_message(self, *args):
        pass

    def forward(self):
        headers = [self.requestline, *(k + ": " + v for k, v in self.headers.items() if k.lower() != "connection"),
                   "Connection: close", "", ""]
        request = "\r\n".join(headers).encode("latin-1") + self.rfile.read(int(self.headers.get("Content-Length", 0)))
        # The controller receives the browser's real Host, Origin, credential, multipart boundaries and body.
        response = self.server.relays.send(request)
        self.close_connection = True
        try:
            self.connection.sendall(response)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    do_GET = forward
    do_PUT = forward
    do_POST = forward
    do_DELETE = forward


def config(secret):
    grants = ["page-system-restapi-keys", "page-firewall-rules", "page-firewall-rules-edit",
              "page-firewall-nat-outbound", "page-services-tftpd", "page-services-node-exporter", "page-services-dnsresolver", "page-services-dnsresolver-advanced", "page-services-dnsresolver-editdomainoverride",
              "page-services-dhcpserver", "page-interfaces", "page-firewall-aliases", "page-firewall-alias-edit"]
    return ('<freesense format="2.0"><interfaces><lan><enable/><if>em0</if><ipaddr>192.0.2.1</ipaddr>'
            '<subnet>24</subnet></lan><wan><enable/><if>em1</if></wan></interfaces>'
            '<unbound><enable/><hosts><host>printer</host><domain>office.test</domain><ip>192.0.2.30</ip></hosts></unbound>'
            '<system><user><name>test-operator</name>' + ''.join('<priv>'+name+'</priv>' for name in grants) +
            '</user></system><restapi><enable/><apitoken><username>test-operator</username><id>000000000001</id>'
            '<scopes>firewall.rules:write firewall.nat:write packages.tftp:write packages.node-exporter:write services.dns:write '
            'services.dhcp:write interfaces.wan:write interfaces.ipv6:write firewall.aliases:write</scopes><hash>' +
            hashlib.sha256(secret.encode()).hexdigest()+'</hash></apitoken></restapi></freesense>').encode()


def start(name, system, optional):
    return docker("run", "-d", "--rm", "--pull=never", "--network", "none", "--name", name,
           "--tmpfs", "/check:rw,nosuid,nodev,mode=0700,size=16m",
           "--tmpfs", "/tftpboot:rw,noexec,nosuid,nodev,mode=0755,size=1m",
           "-e", "FREESENSE_CONFIG_DIR=/check/config",
           "-v", f"{system}:/source:ro", "-v", f"{optional}:/optional:ro", IMAGE, "sleep", "infinity").decode().strip()


def prepare(name, port, secret):
    script = """set -eu
mkdir /check/prefix
cp -R /source/. /check/prefix/
cp -R /optional/sysutils/FreeSense-pkg-node_exporter/files/usr/local/. /check/prefix/
printf '%s' disposable-file > /tftpboot/image.txt
printf '%s' retained-file > /tftpboot/keep.txt
chmod 0644 /tftpboot/image.txt /tftpboot/keep.txt
php /check/prefix/sbin/freesense-config init >/dev/null"""
    docker("exec", name, "sh", "-c", script)
    docker("exec", "-i", name, "php", "/check/prefix/sbin/freesense-config", "restore", input=config(secret))
    docker("exec", "-d", name, "sh", "/check/prefix/sbin/freesense-web", str(port))
    for _ in range(50):
        check = 'exit(@stream_socket_client("tcp://127.0.0.1:".$argv[1],timeout:.2)?0:1);'
        try:
            docker("exec", name, "php", "-r", check, str(port))
            break
        except subprocess.CalledProcessError:
            time.sleep(.1)
    else:
        raise RuntimeError("Disposable PHP controller did not start.")
    # Admit the file manager after PHP has started to exercise package discovery and listener limits.
    docker("exec", name, "cp", "-R", "/optional/ftp/FreeSense-pkg-tftpd/files/usr/local/.", "/check/prefix/")


def exercise_wan(page, name):
    """Use the genuine System form; the isolated Linux namespace has no assigned FreeBSD em1."""
    endpoint = '/api/v1/interfaces/wan'
    original = docker("exec", name, "sh", "-c", 'FREESENSE_CONFIG_DIR=/check/config php /check/prefix/sbin/freesense-config backup')
    menu_link(page, "Interfaces", "WAN IPv4").click()
    submit = page.get_by_role("button", name="Save WAN settings", exact=True)
    form = page.locator("form").filter(has=submit)
    mode = form.get_by_role("combobox", name="IPv4 mode")
    address = form.get_by_label("IPv4 address and prefix", exact=True)
    gateway = form.get_by_label("Default gateway", exact=True)
    expect(mode).to_have_value("dhcp")
    expect(address).to_be_hidden()
    expect(gateway).to_be_hidden()
    mode.select_option("static")
    address.fill("198.51.100.1/24")
    gateway.fill("203.0.113.1")
    sent = []
    page.on("request", lambda request: sent.append(request) if request.method == "PUT" and request.url.endswith(endpoint) else None)
    submit.click()
    expect(form.locator('[data-error-for="gateway"]')).to_contain_text("local IPv4 subnet")
    assert not sent, "Invalid WAN gateway reached the router"
    mode.select_option("dhcp")
    expect(gateway).to_be_hidden()
    form.get_by_label("WAN interface", exact=True).fill("em9")
    with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith(endpoint)) as denied:
        submit.click()
    assert denied.value.status == 422 and set(denied.value.request.post_data_json) == {"revision", "device", "mode"}
    assert set(denied.value.json()["error"]["fields"]) == {"device"}
    expect(form.locator('[data-error-for="device"]')).to_contain_text("current WAN assignment")
    expect(form.get_by_label("WAN interface", exact=True)).to_have_attribute("aria-invalid", "true")
    current = docker("exec", name, "sh", "-c", 'FREESENSE_CONFIG_DIR=/check/config php /check/prefix/sbin/freesense-config backup')
    assert original == current, "A rejected WAN assignment changed the configuration"


def exercise_dhcp(page, name, evidence):
    endpoint = '/api/v1/services/dhcp/lan'
    with page.expect_response(lambda r: r.url.endswith(endpoint) and r.request.method == "GET") as first:
        menu_link(page, "Services", "LAN DHCP").click()
    initial = first.value.json()["data"]
    assert not initial["enabled"] and initial["cidr"] == "192.0.2.1/24" and initial["device"] == "em0"
    save = page.get_by_role("button", name="Save DHCP settings", exact=True)
    form = page.locator("form").filter(has=save)
    enabled = form.get_by_role("checkbox", name="Enable LAN DHCP", exact=True)
    start = form.get_by_label("First pool address", exact=True)
    end = form.get_by_label("Last pool address", exact=True)
    gateway = form.get_by_label("Router IPv4 address", exact=True)
    expect(start).to_be_hidden()
    enabled.check()
    sent = []
    page.on("request", lambda r: sent.append(True) if r.method == "PUT" and r.url.endswith(endpoint) else None)
    start.fill("192.0.2.100")
    end.fill("192.0.2.99")
    save.click()
    expect(form.locator('[data-error-for="to"]')).to_contain_text("at or after the starting IP")
    assert not sent, "A reversed DHCP pool reached PHP"
    start.fill("198.51.100.100")
    end.fill("198.51.100.140")
    save.click()
    expect(form.locator('[data-error-for="to"]')).to_contain_text("usable hosts on the assigned LAN")
    assert not sent, "A DHCP pool outside the configured LAN reached PHP"
    start.fill("192.0.2.100")
    end.fill("192.0.2.140")
    form.get_by_role("combobox", name="Router option", exact=True).select_option("explicit")
    gateway.fill("192.0.2.254")
    dns = form.get_by_role("group", name="DNS servers", exact=True)
    dns.get_by_role("button", name="Add item").click()
    dns.get_by_role("textbox", name="DNS servers 1").fill("192.0.2.53")
    writes = lambda r: r.url.endswith(endpoint) and r.request.method == "PUT"
    with page.expect_response(writes) as updated:
        save.click()
    assert updated.value.status == 200, "Could not save the valid LAN DHCP pool"
    expect(page.get_by_text("DHCP settings saved.")).to_be_visible()
    expect(gateway).to_have_value("192.0.2.254")
    if evidence:
        page.screenshot(path=evidence / "lan-dhcp.png", full_page=True)
    enabled.uncheck()
    expect(start).to_be_hidden()
    expect(gateway).to_be_hidden()
    with page.expect_response(writes) as disabled:
        save.click()
    assert disabled.value.status == 200 and set(disabled.value.request.post_data_json) == {"revision", "enabled"}
    retained = disabled.value.json()["data"]
    assert not retained["enabled"] and retained["from"] == "192.0.2.100" and retained["to"] == "192.0.2.140"
    assert retained["routerMode"] == "explicit" and retained["gateway"] == "192.0.2.254" and retained["dns"] == ["192.0.2.53"]
    expect(page.get_by_text("DHCP settings saved.")).to_be_visible()
    enabled.check()
    expect(start).to_have_value(retained["from"])
    xml = ElementTree.fromstring(docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup"))
    lan = xml.find("./dhcpd/lan")
    assert lan is not None and lan.find("enable") is None and lan.findtext("range/to") == retained["to"]
    assert lan.findtext("gateway") == retained["gateway"] and lan.findtext("dnsserver") == retained["dns"][0]


def exercise_dns(page, name, evidence):
    endpoint = '/api/v1/services/dns/forwarders'
    backup = lambda: docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup")
    original = ElementTree.canonicalize(backup(), strip_text=True)
    reads = lambda response: response.url.endswith(endpoint) and response.request.method == "GET"
    writes = lambda response: response.url.endswith(endpoint) and response.request.method == "PUT"
    with page.expect_response(reads) as initial:
        menu_link(page, "Services", "DNS resolver").click()
    assert initial.value.json()["data"]["forwarders"] == [] and initial.value.json()["data"]["enabled"] is True
    submit = page.get_by_role("button", name="Save forwarders")
    form = page.locator("form").filter(has=submit)
    requests = []
    page.on("request", lambda request: requests.append(True) if request.method == "PUT" and request.url.endswith(endpoint) else None)
    form.get_by_role("button", name="Add item").click()
    address = form.get_by_role("textbox", name="Upstream DNS addresses 1", exact=True)
    address.fill("127.0.0.53")
    submit.click()
    expect(form.locator('[data-error-for="forwarders"]')).to_contain_text("Item 1")
    assert not requests, "Invalid DNS upstream reached PHP"
    address.fill("2001:0db8::53")
    form.get_by_role("button", name="Add item").click()
    second = form.get_by_role("textbox", name="Upstream DNS addresses 2", exact=True)
    second.fill("2001:db8::53")
    submit.click()
    expect(form.get_by_text("Choose distinct list items.", exact=True)).to_be_visible()
    assert not requests, "Equivalent IPv6 upstreams reached PHP"
    form.get_by_role("button", name="Remove", exact=True).last.click()
    tls = form.get_by_role("checkbox", name="Use DNS over TLS", exact=True)
    tls.check()
    address.fill("2001:0db8::53#Dot.Example.")
    with page.expect_response(writes) as updated:
        submit.click()
    assert updated.value.status == 200 and updated.value.json()["data"]["tls"]
    assert updated.value.json()["data"]["forwarders"] == ["2001:db8::53#dot.example"]
    expect(page.get_by_text("Forwarders saved.", exact=True)).to_be_visible()
    expect(address).to_have_value("2001:db8::53#dot.example")
    saved = ElementTree.fromstring(backup())
    upstream = saved.find("./unbound/forwarder")
    assert upstream is not None and upstream.text == "2001:db8::53#dot.example", "Forwarder was not saved to the configuration"
    saved.find("./unbound").remove(upstream)
    saved.find("./unbound").remove(saved.find("./unbound/forward_tls_upstream"))
    assert ElementTree.canonicalize(ElementTree.tostring(saved), strip_text=True) == original, "Another configuration setting changed"
    if evidence:
        page.screenshot(path=evidence / "dns-resolver.png", full_page=True)
    form.get_by_role("button", name="Remove", exact=True).click()
    tls.uncheck()
    with page.expect_response(writes) as cleared:
        submit.click()
    assert cleared.value.status == 200 and cleared.value.json()["data"]["forwarders"] == []
    expect(page.get_by_text("Forwarders saved.", exact=True)).to_be_visible()
    assert ElementTree.canonicalize(backup(), strip_text=True) == original, "Clearing upstreams did not restore the saved state"


def focused_dns_forwarders(page, url, key, name, _docker, evidence):
    page.goto(url + "/")
    fill_api_key(page, key)
    page.get_by_role("button", name="Sign in").click()
    exercise_dns(page, name, evidence)
    endpoint = url + "/api/v1/services/dns/settings"
    headers = {"Authorization": "Bearer " + key, "Origin": url, "X-FreeSense-Command": "saveResolverService"}
    view = url + "/api/v1/services/dns/forwarders"
    initial = page.request.get(view, headers=headers).json()["data"]
    enabled = page.get_by_label("Enable DNS Resolver", exact=True)
    lan = page.get_by_label("Listen on the assigned LAN", exact=True)
    enabled.set_checked(not initial["service"]["enabled"])
    lan.set_checked(not initial["service"]["listenLan"])
    with page.expect_response(lambda x: x.url == endpoint and x.request.method == "PUT") as saved:
        page.get_by_role("button", name="Save service settings").click()
    assert saved.value.status == 200, saved.value.text()
    after = page.request.get(view, headers=headers).json()["data"]
    assert after["service"]["enabled"] != initial["service"]["enabled"] and after["service"]["listenLan"] != initial["service"]["listenLan"]
    assert after["forwarders"] == initial["forwarders"] and after["revision"] == initial["revision"]
    assert page.request.put(endpoint, headers=headers, data=initial["service"]).status == 409
    assert page.request.put(endpoint, headers=headers, data=initial["service"] | {"revision":after["service"]["revision"]}).status == 200
    page.get_by_role("link", name="Advanced Settings", exact=True).click()
    advanced = url + "/api/v1/services/dns/advanced"
    old = page.request.get(advanced, headers=headers).json()["data"]
    for label in ["Hide identity","Hide version","Prefetch frequently queried records","Serve expired cache records"]:
        page.get_by_label(label, exact=True).check()
    min_ttl = page.get_by_label("Minimum cache TTL (seconds)", exact=True)
    max_ttl = page.get_by_label("Maximum cache TTL (seconds)", exact=True)
    min_ttl.fill("180")
    max_ttl.fill("15")
    sent = []
    page.on("request", lambda req: sent.append(req.url) if req.method == "PUT" and req.url == advanced else None)
    page.get_by_role("button", name="Save advanced settings").click()
    expect(page.get_by_text("Enter a value greater than or equal to the starting value.", exact=True)).to_be_visible()
    assert not sent
    min_ttl.fill("15")
    max_ttl.fill("180")
    page.get_by_role("combobox", name="Message cache size (MiB)", exact=True).select_option("10")
    page.get_by_role("combobox", name="Incoming TCP buffers per thread", exact=True).select_option("0")
    with page.expect_response(lambda x: x.url == advanced and x.request.method == "PUT") as updated:
        page.get_by_role("button", name="Save advanced settings").click()
    assert updated.value.status == 200
    latest = updated.value.json()["data"]
    expect(page.get_by_text("Advanced settings saved.", exact=True)).to_be_visible()
    page.reload()
    fill_api_key(page, key)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_label("Hide version", exact=True)).to_be_checked()
    if evidence:
        page.screenshot(path=evidence / "resolver-advanced.png", full_page=True)
    advanced_headers = headers | {"X-FreeSense-Command": "saveResolverOptions"}
    assert page.request.put(advanced, headers=advanced_headers, data=old).status == 409
    assert latest["msgcachesize"] == "10" and latest["incoming_num_tcp"] == "0"
    assert page.request.put(advanced, headers=advanced_headers, data=latest | {"msgcachesize":"999"}).status == 422
    flags = {k:v for k,v in latest.items() if isinstance(v,bool) or k == "revision"}
    assert page.request.put(advanced, headers=advanced_headers, data=flags).json()["data"] == latest
    assert page.request.put(advanced, headers=advanced_headers, data=flags | {"cache_max_ttl":5}).status == 422
    assert page.request.get(advanced, headers=headers).json()["data"] == latest
    assert latest["cache_min_ttl"] == 15 and latest["cache_max_ttl"] == 180
    assert all(latest[option] for option in ("hideidentity", "hideversion", "prefetch", "dnsrecordcache"))
    assert page.request.put(advanced, headers=advanced_headers, data=old | {"revision":latest["revision"]}).status == 200
    print("Chromium verified advanced privacy and cache settings through AJAX, persistence and conflict detection.")
    exercise_dns_domains(page, url, key, name, _docker, evidence)
    print("Chromium verified resolver service and listener selection, independent revisions and refusal of stale edits.")



def exercise_dns_domains(page, url, key, name, docker, evidence):
    page.get_by_role("link", name="Domain Overrides", exact=True).click()
    expect(page.get_by_text("No domain overrides yet.")).to_be_visible()
    endpoint = url + "/api/v1/services/dns/domain-overrides"
    headers = {"Authorization":"Bearer " + key, "Origin":url, "X-FreeSense-Command":"createDnsDomainOverride"}
    original = page.request.get(endpoint, headers=headers).json()["data"]
    page.get_by_role("link", name="Add domain override").click()
    domain = page.get_by_label("Domain", exact=True)
    domain.fill("Office.Example.")
    address = page.get_by_label("IP address", exact=True)
    address.fill("127.0.0.1")
    save = page.get_by_role("button", name="Save domain override")
    sends = []
    page.on("request", lambda req: sends.append(req.url) if req.url.startswith(endpoint) and req.method == "PUT" else None)
    save.click()
    expect(address).to_have_attribute("aria-invalid","true")
    assert not sends
    address.fill("2001:0db8::53")
    page.get_by_label("Upstream port").fill("8853")
    page.get_by_label("Use DNS over TLS for this domain").check()
    page.get_by_label("TLS certificate hostname").fill("DNS.Office.Example.")
    page.get_by_label("Description", exact=True).fill("Private DNS")
    if evidence:
        page.screenshot(path=evidence / "dns-domain-editor.png", full_page=True)
    with page.expect_response(lambda res: res.url == endpoint and res.request.method == "PUT") as saved:
        save.click()
    assert saved.value.status == 200, saved.value.text()
    expect(page.get_by_role("cell", name="2001:db8::53", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="DNS over TLS", exact=True)).to_be_visible()
    row = page.request.get(endpoint,headers=headers).json()["data"]["rows"][0]
    assert row["domain"] == "office.example" and row["hostname"] == "dns.office.example" and row["effectivePort"] == 8853
    if evidence:
        page.screenshot(path=evidence / "dns-domain-overrides.png", full_page=True)
    body = {"revision":original["revision"],"domain":"office.example","address":"2001:db8::54","tls":False,"description":""}
    assert page.request.put(endpoint,headers=headers,data=body).status == 409
    fresh = page.request.get(endpoint,headers=headers).json()["data"]
    assert page.request.put(endpoint,headers=headers,data=body | {"revision":fresh["revision"],"address":row["address"],"port":8853}).status == 422
    assert page.request.put(endpoint,headers=headers,data=body | {"revision":fresh["revision"],"hostname":"test.example"}).status == 422
    page.get_by_role("link", name="Edit office.example via 2001:db8::53").click()
    expect(page.get_by_label("TLS certificate hostname")).to_have_value("dns.office.example")
    page.get_by_label("Use DNS over TLS for this domain").uncheck()
    expect(page.get_by_label("TLS certificate hostname")).to_be_hidden()
    save.click()
    expect(page.get_by_role("cell", name="DNS", exact=True)).to_be_visible()
    page.once("dialog", lambda event: event.accept())
    page.get_by_role("button", name="Delete office.example via 2001:db8::53").click()
    expect(page.get_by_text("No domain overrides yet.")).to_be_visible()
    original_xml = docker("exec",name,"php","/check/prefix/sbin/freesense-config","backup")
    def grant(remove, scope):
        config = ElementTree.fromstring(original_xml)
        user = config.find("./system/user")
        for node in user.findall("priv"):
            if node.text == remove:
                user.remove(node)
        config.find("./restapi/apitoken/scopes").text = scope
        docker("exec","-i",name,"php","/check/prefix/sbin/freesense-config","restore",input=ElementTree.tostring(config))
    try:
        grant("page-services-dnsresolver", "services.dns:write")
        assert page.request.get(endpoint,headers=headers).status == 403
        assert page.request.get(endpoint + "/edit",headers=headers).status == 200
        grant("page-services-dnsresolver-editdomainoverride", "services.dns:write")
        assert page.request.get(endpoint,headers=headers).status == 200
        assert page.request.get(endpoint + "/edit",headers=headers).status == 403
        assert page.request.put(endpoint,headers=headers,data=body).status == 403
        grant("page-services-dnsresolver-editdomainoverride", "services.dns:read")
        registry = page.request.get(url + "/api/v1/forms",headers=headers).json()["data"]["operations"]
        assert not any(op in registry for op in ("createDnsDomainOverride","editDnsDomainOverride","removeDnsDomainOverride"))
    finally:
        docker("exec","-i",name,"php","/check/prefix/sbin/freesense-config","restore",input=original_xml)
    print("Chromium verified scoped DNS override forms, TLS, canonical IP and DNS names, conflict handling and removal.")


def exercise_icmp(page, name, existing, evidence):
    page.get_by_role("button", name="Add rule").click()
    rule = page.get_by_role("group", name="Rule 2", exact=True)
    rule.get_by_role("combobox", name="Action", exact=True).select_option("pass")
    rule.get_by_label("Source", exact=True).fill("192.0.2.0/24")
    rule.get_by_label("Destination", exact=True).fill("198.51.100.10")
    rule.get_by_label("Enabled", exact=True).check()
    rule.get_by_role("combobox", name="Protocol", exact=True).select_option("icmp")
    types = rule.get_by_role("group", name="ICMP types", exact=True)
    for index, code in enumerate([0, 8], start=1):
        types.get_by_role("button", name="Add item").click()
        types.get_by_role("spinbutton", name=f"ICMP types {index}", exact=True).fill(str(code))
    endpoint = "/api/v1/firewall/rules"
    with page.expect_response(lambda r: r.url.endswith(endpoint) and r.request.method == "GET") as reread:
        with page.expect_response(lambda r: r.url.endswith(endpoint) and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save rules", exact=True).click()
    assert saved.value.status == 200 and reread.value.status == 200
    assert reread.value.json()["data"]["rules"][1]["icmp_types"] == [0, 8]
    expect(types.get_by_role("spinbutton", name="ICMP types 1", exact=True)).to_have_value("0")
    expect(types.get_by_role("spinbutton", name="ICMP types 2", exact=True)).to_have_value("8")
    xml = ElementTree.fromstring(docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup"))
    rules = xml.findall("./firewall/rule")
    assert len(rules) == 2 and rules[0].attrib == existing.attrib
    assert rules[1].get("protocol") == "icmp" and rules[1].get("icmp_types") == "0,8"
    # The address family changes the valid endpoints and the wire ICMP selector on the same saved rule.
    rule.get_by_role("combobox", name="Address family", exact=True).select_option("inet6")
    requests = []
    page.on("request", lambda request: requests.append(True) if request.method == "PUT" and request.url.endswith(endpoint) else None)
    page.get_by_role("button", name="Save rules", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="Invalid source IPv6 host or network.")).to_be_visible()
    assert not requests, "A mixed-family firewall rule reached the server"
    rule.get_by_label("Source", exact=True).fill("2001:db8:1::/64")
    rule.get_by_label("Destination", exact=True).fill("2001:db8:2::7")
    for index, code in enumerate([128, 129], start=1):
        types.get_by_role("spinbutton", name=f"ICMP types {index}", exact=True).fill(str(code))
    with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == "GET") as refreshed:
        with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == "PUT") as result:
            page.get_by_role("button", name="Save rules", exact=True).click()
    assert result.value.status == 200 and refreshed.value.json()["data"]["rules"][1]["family"] == "inet6"
    expect(rule.get_by_role("combobox", name="Address family", exact=True)).to_have_value("inet6")
    rule.get_by_role("checkbox", name="Log matching traffic", exact=True).check()
    with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == "GET") as reread:
        with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save rules", exact=True).click()
    assert saved.value.status == 200 and reread.value.json()["data"]["rules"][1]["log"] is True
    xml = ElementTree.fromstring(docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup"))
    rules = xml.findall("./firewall/rule")
    assert len(rules) == 2 and rules[0].attrib == existing.attrib
    assert rules[1].get("family") == "inet6" and rules[1].get("icmp_types") == "128,129" and rules[1].get("log") == "true"
    if evidence:
        rule.screenshot(path=evidence / "firewall-ipv6-rule.png")


def exercise(page, url, key, name, evidence):
    page.goto(url + "/firewall")
    fill_api_key(page, key)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("combobox", name="NAT mode").select_option("automatic")
    page.get_by_role("button", name="Save NAT mode").click()
    expect(page.get_by_text("Draft saved.", exact=True)).to_be_visible()
    rule = {"enabled": True, "action": "pass", "source": "192.0.2.0/24", "destination": "198.51.100.10",
            "protocol": "tcp", "destination_port": 443}
    page.get_by_label("JSON rules file").set_input_files(
        {"name": "rules.json", "mimeType": "application/json", "buffer": json.dumps([rule]).encode()})
    with page.expect_response(lambda r: r.url.endswith("/firewall/rules/import") and r.request.method == "PUT") as call:
        page.get_by_role("button", name="Import JSON").click()
    assert call.value.status == 200, "Actual PHP multipart import failed"
    row = page.get_by_role("group", name="Rule 1", exact=True)
    expect(row.get_by_label("Destination", exact=True)).to_have_value("198.51.100.10")
    row.get_by_label("Source", exact=True).fill("192.0.2.12")
    with page.expect_response(lambda r: r.url.endswith("/firewall/rules") and r.request.method == "PUT") as call:
        page.get_by_role("button", name="Save rules").click()
    assert call.value.status == 200, "Edited rule did not commit"
    expect(page.get_by_text("Draft saved.", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="NAT mode")).to_have_value("automatic")
    page.get_by_text("Port range", exact=True).click()
    if evidence:
        page.screenshot(path=evidence/"firewall-editor.png", full_page=True)
    snapshot = ElementTree.fromstring(docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup"))
    saved = snapshot.find("./firewall/rule")
    assert saved is not None and saved.get("source") == "192.0.2.12" and saved.get("destination_port") == "443"
    exercise_icmp(page, name, saved, evidence)
    # The Package Manager pages have their own browser tests (tests/web/test_packages.py); reach the package page by the app router.
    page.evaluate("() => { history.pushState(null, '', '/services/tftp/files'); dispatchEvent(new PopStateEvent('popstate')); }")
    expect(page.get_by_text("image.txt", exact=True).first).to_be_visible()
    assert page.url == url + "/services/tftp/files"
    upload = page.locator("form").filter(has=page.get_by_role("button", name="Upload file", exact=True))
    file = upload.locator('input[type=file]')
    submit = upload.get_by_role("button", name="Upload file")
    raw = bytes(range(256)) * 384
    def choose(*items):
        file.set_input_files([{"name": n, "mimeType": "application/octet-stream", "buffer": contents} for n, contents in items])
    requests = []
    page.on("request", lambda request: requests.append(True) if request.method == "PUT" and request.url.endswith("/tftp/files/upload") else None)
    cases = [
        (["bad name.bin"], [4], "Invalid filename"),
        (["a" * 129], [1], "filename is too long"),
        (["huge.bin"], [67108865], "file exceeds the size limit"),
        (["batch-a.bin", "batch-b.bin"], [33554432, 33554433], "total size limit"),
        (["duplicate.bin", "duplicate.bin"], [1, 2], "Duplicate filename"),
        (["member-" + str(i) + ".bin" for i in range(9)], [1] * 9, "exceeds the limit of 8")
    ]
    for names, sizes, error in cases:
        file.evaluate("""(control, {names,sizes}) => {
            const selections = new DataTransfer();
            names.forEach((name,i) => selections.items.add(new File([new Uint8Array(sizes[i])],name)));
            control.files = selections.files;
        }""", {"names": names, "sizes": sizes})
        submit.click()
        expect(upload.locator('[data-error-for="file"]')).to_contain_text(error)
    assert not requests, "Rejected files must never be posted"
    choose(("asset.bin", raw), ("second.bin", b"\xff\x04\x00\x82"))
    # A real package-directory mutation invalidates the open form's revision.
    docker("exec", name, "touch", "/tftpboot/revision.txt")
    upload_result = lambda r: r.url.endswith("/tftp/files/upload") and r.request.method == "PUT"
    with page.expect_response(upload_result) as call:
        submit.click()
    assert call.value.status == 409, "Stale package upload was accepted"
    docker("exec", name, "sh", "-c", "test ! -e /tftpboot/asset.bin && test ! -e /tftpboot/second.bin")
    page.get_by_role("button", name="Refresh").click()
    expect(page.locator("li").get_by_text("revision.txt", exact=True)).to_be_visible()
    docker("exec", name, "mkdir", "/tftpboot/reserved.bin")
    choose(("would-write.bin", raw), ("reserved.bin", raw))
    with page.expect_response(upload_result) as call:
        submit.click()
    assert call.value.status in (409, 422), "A reserved upload destination must fail the entire selection"
    docker("exec", name, "sh", "-c", "test ! -e /tftpboot/would-write.bin && test -d /tftpboot/reserved.bin")
    choose(("asset.bin", raw), ("second.bin", b"\xff\x04\x00\x82"))
    with page.expect_response(upload_result) as call:
        submit.click()
    assert call.value.status == 200 and docker("exec", name, "cat", "/tftpboot/asset.bin") == raw
    assert docker("exec", name, "cat", "/tftpboot/second.bin") == b"\xff\x04\x00\x82"
    expect(page.locator("li").get_by_text("asset.bin", exact=True)).to_be_visible()
    choose(("asset.bin", b""))
    page.once("dialog", lambda dialog: dialog.dismiss())
    submit.click()
    assert docker("exec", name, "cat", "/tftpboot/asset.bin") == raw
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(upload_result) as call:
        submit.click()
    assert call.value.status == 200 and docker("exec", name, "cat", "/tftpboot/asset.bin") == b""
    # A syntactically valid filename can still fail the provider's current-file check.
    remove = page.get_by_role("button", name="Remove file")
    filename = page.get_by_label("Filename", exact=True)
    if evidence:
        filename.fill("image.txt")
        page.screenshot(path=evidence/"optional-tftp.png", full_page=True)
    filename.fill("unlisted.bin")
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda r: r.url.endswith("/tftp/files/remove") and r.request.method == "PUT") as call:
        remove.click()
    assert call.value.status == 422 and set(call.value.json()["error"]["fields"]) == {"filename"}
    expect(filename).to_have_attribute("aria-invalid", "true")
    expect(page.get_by_text("Select a current TFTP file.", exact=True)).to_be_visible()
    filename.fill("image.txt")
    page.once("dialog", lambda dialog: dialog.dismiss())
    remove.click()
    docker("exec", name, "test", "-f", "/tftpboot/image.txt")
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda r: r.url.endswith("/tftp/files/remove") and r.request.method == "PUT") as call:
        remove.click()
    assert call.value.status == 200, "Confirmed package removal failed"
    expect(page.get_by_text("No files available.", exact=True)).to_have_count(0)
    expect(page.locator("li").get_by_text("image.txt", exact=True)).to_have_count(0)
    assert docker("exec", name, "cat", "/tftpboot/keep.txt") == b"retained-file"
    docker("exec", name, "test", "!", "-e", "/tftpboot/image.txt")
    nav_link(page, "/packages/node-exporter").click()
    expect(page.get_by_role("heading", name="Prometheus node exporter")).to_be_visible()
    page.get_by_label("Enable loopback metrics").check()
    page.get_by_role("spinbutton", name="Listen port").fill("19123")
    page.get_by_role("combobox").first.select_option("boottime")
    with page.expect_response(lambda r: r.url.endswith("/node-exporter/settings") and r.request.method == "PUT") as call:
        page.get_by_role("button", name="Save settings").click()
    assert call.value.status == 200, "Package settings form did not save"
    expect(page.get_by_role("spinbutton", name="Listen port")).to_have_value("19123")
    xml = ElementTree.fromstring(docker("exec", name, "php", "/check/prefix/sbin/freesense-config", "backup"))
    settings = xml.find("./installedpackages/nodeexporter/config")
    assert settings is not None and settings.findtext("enable") == "on" and settings.findtext("listen_port") == "19123"
    assert settings.findtext("collectors") == "boottime,time"
    exercise_tftp_settings(page, url, key, name, docker, evidence)
    exercise_wan(page, name)
    exercise_ipv6_settings(page, url, key, name, docker, evidence)
    exercise_aliases(page, url, key, evidence)
    exercise_dhcp(page, name, evidence)
    exercise_dns(page, name, evidence)
    exercise_alias_authority(page, url, key, name, docker)
    exercise_port_forwards(page, url, key, name, docker, evidence)
    exercise_mdns_bridge(page, url, key, name, docker, evidence)
    exercise_local_rules(page, url, key, name, docker, evidence)
    exercise_ingress_rules(page, url, key, name, docker, evidence)
    exercise_wireguard(page, url, key, name, docker, evidence)
    sign_out(page)
    expect(page.get_by_role("heading", name="Appliance sign-in")).to_be_visible()


def main(root=ROOT, evidence=None, only=None):
    system = root/"freesense/src/usr/local"
    optional = root/"freesense-packages"
    if not (system/"FreeSense/web/index.php").is_file() or not all((optional/port/"share/FreeSense/web-packages"/manifest).is_file()
            for port, manifest in [(TFTP, "tftp.json"), (EXPORTER, "node-exporter.json")]):
        raise RuntimeError("System web controller and Optional file and metrics providers must be present for this integration check.")
    name = "freesense-browser-test-" + secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    with ThreadingHTTPServer(("127.0.0.1", 0), LoopbackRelay) as server:
        owned = start(name, system, optional)
        server.container = owned
        server.relays = Relays(owned, server.server_port)
        started = False
        try:
            prepare(owned, server.server_port, secret)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            started = True
            with sync_playwright() as engine:
                channel = os.environ.get("FREESENSE_BROWSER_CHANNEL", "chrome" if os.name == "nt" else None)
                browser = engine.chromium.launch(channel=channel)
                try:
                    page = browser.new_page(viewport={"width": 1320, "height": 920})
                    page.set_default_timeout(12000)
                    if evidence: evidence.mkdir(parents=True, exist_ok=True)
                    context = (page, f"http://127.0.0.1:{server.server_port}", "fsk_000000000001_" + secret, name)
                    if only:
                        focused = {"ups-widget":exercise_ups_widget, "tftp-backup": exercise_tftp_backup, "configuration-history": exercise_config_history, "dhcp-leases": exercise_dhcp_leases, "fleet-controller": exercise_fleet_controller, "fleet": exercise_fleet, "dns-forwarders": focused_dns_forwarders, "one-to-one": exercise_one_to_one, "outbound": exercise_outbound, "dns-hosts": exercise_dns_hosts, "traffic-totals": exercise_traffic_totals, "softflowd": exercise_softflowd, "andwatch": exercise_andwatch, "services": exercise_services, "net-snmp": exercise_net_snmp, "bind": exercise_bind, "bind-widget": exercise_bind_widget, "telegraf": exercise_telegraf, "zabbix-agent": exercise_zabbix_agent, "avahi": exercise_avahi, "multicast-bridge": exercise_multicast_bridge, "mdns-bridge": exercise_mdns_bridge, "udp-relay": exercise_udp_relay, "port-forwards": exercise_port_forwards,
                                   "admin-key-issuance": exercise_admin_key_issuance, "personal-key-issuance": exercise_personal_key_issuance, "system-api-keys": exercise_system_api_keys, "account-security": exercise_account_security, "account-language": exercise_account_language, "dashboard-preferences": exercise_dashboard_preferences, "account-profile": exercise_account_profile, "account-menu": exercise_account_menu, "menu-search": exercise_menu_search, "port-aliases": exercise_port_aliases, "local-rules": exercise_local_rules, "ping": exercise_ping, "traceroute": exercise_traceroute, "dns-lookup": exercise_dns_lookup, "ingress-rules": exercise_ingress_rules,
                                   "interface-groups": exercise_interface_groups, "group-rules": exercise_group_rules, "floating-rules": exercise_floating_rules,
                                   "opt-assignments": exercise_opt_assignments, "virtual-ips": exercise_virtual_ips, "dns-access": exercise_dns_access, "dashboard": exercise_dashboard, "dashboard-system": exercise_dashboard_system, "admin-access": exercise_admin_access, "appearance": exercise_appearance, "dnssec": exercise_dnssec, "static-routing": exercise_static_routing,
                                   "router-advertisement": exercise_ra, "ntp": exercise_ntp, "ppp": exercise_ppp, "dhcp-mappings":exercise_dhcp_mappings, "nut":exercise_nut,
                                   "states": exercise_states, "kernel-routes": exercise_kernel_routes, "gateway-status": exercise_gateway_status, "gateway-groups": exercise_gateway_groups, "log-storage": exercise_log_storage, "default-rule-log": exercise_default_rule_log, "log-display": exercise_log_display, "lldp": exercise_lldp, "open-vm-tools": exercise_open_vm_tools, "filter-log": exercise_filter_log, "filter-log-summary": exercise_filter_log_summary, "remote-syslog": exercise_remote_syslog, "wireguard": exercise_wireguard}
                        focused[only](*context, docker, evidence)
                    else:
                        exercise(*context, evidence)
                finally:
                    browser.close()
        finally:
            if started:
                server.shutdown()
                thread.join()
            server.relays.close()
            docker("stop", "--timeout", "2", owned)
    if not only:
        print("Real Chromium and PHP verified IPv4 and IPv6 firewall drafts, ICMP types, rule import, TFTP uploads and removal, exporter settings, WAN drafts, LAN DHCP settings and DNS forwarder edits.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="save credential-free browser screenshots in this lab directory")
    focus = parser.add_mutually_exclusive_group()
    focus.add_argument("--ppp", dest="only", action="store_const", const="ppp", help="exercise the PPP, PPPoE server, L2TP and PPPoE/L2TP log pages")
    focus.add_argument("--ntp", dest="only", action="store_const", const="ntp", help="exercise Services > NTP (four tabs), Status > NTP and Status > DNS Resolver")
    focus.add_argument("--router-advertisement", dest="only", action="store_const", const="router-advertisement", help="exercise the LAN Router Advertisement page and separate read, edit and apply grants")
    focus.add_argument("--wireguard", dest="only", action="store_const", const="wireguard", help="exercise WireGuard drafts, identity and independent status access")
    focus.add_argument("--one-to-one", dest="only", action="store_const", const="one-to-one", help="exercise static WAN bidirectional address mappings")
    focus.add_argument("--outbound", dest="only", action="store_const", const="outbound", help="exercise ordered IPv4 WAN mappings and all four NAT modes")
    focus.add_argument("--port-forwards", dest="only", action="store_const", const="port-forwards", help="exercise only the static IPv4 port-forward editor and API")
    focus.add_argument("--multicast-bridge", dest="only", action="store_const", const="multicast-bridge", help="exercise the multicast channel editor and API")
    focus.add_argument("--softflowd", dest="only", action="store_const", const="softflowd")
    focus.add_argument("--andwatch", dest="only", action="store_const", const="andwatch")
    focus.add_argument("--services", dest="only", action="store_const", const="services")
    focus.add_argument("--net-snmp", dest="only", action="store_const", const="net-snmp")
    focus.add_argument("--bind", dest="only", action="store_const", const="bind")
    focus.add_argument("--bind-widget", dest="only", action="store_const", const="bind-widget")
    focus.add_argument("--telegraf", dest="only", action="store_const", const="telegraf")
    focus.add_argument("--zabbix-agent", dest="only", action="store_const", const="zabbix-agent")
    focus.add_argument("--avahi", dest="only", action="store_const", const="avahi", help="exercise Services > Avahi, reflection, publishing and scoped grants")
    focus.add_argument("--static-routing", dest="only", action="store_const", const="static-routing", help="exercise gateway and static-route grants and forms")
    focus.add_argument("--dnssec", dest="only", action="store_const", const="dnssec", help="exercise DNSSEC and advanced validation controls")
    focus.add_argument("--appearance", dest="only", action="store_const", const="appearance", help="exercise Account Appearance and both shared layouts")
    focus.add_argument("--menu-search", dest="only", action="store_const", const="menu-search", help="exercise authorized menu search across desktop and mobile layouts")
    focus.add_argument("--account-menu", dest="only", action="store_const", const="account-menu", help="exercise account utilities, authorized start pages, sign-out and identity boundaries")
    focus.add_argument("--account-profile", dest="only", action="store_const", const="account-profile", help="exercise local Account Profile editing, revision conflicts and managed identities")
    focus.add_argument("--account-language", dest="only", action="store_const", const="account-language", help="exercise scoped language selection, German Account pages and English fallback")
    focus.add_argument("--admin-key-issuance", dest="only", action="store_const", const="admin-key-issuance", help="exercise delegated issuance for selected local users")
    focus.add_argument("--personal-key-issuance", dest="only", action="store_const", const="personal-key-issuance", help="exercise scoped personal issuance and one-time delivery")
    focus.add_argument("--system-api-keys", dest="only", action="store_const", const="system-api-keys", help="exercise individual API key inventories and revocation")
    focus.add_argument("--account-security", dest="only", action="store_const", const="account-security", help="exercise personal API-key inventory and confirmed owner-only revocation")
    focus.add_argument("--admin-access", dest="only", action="store_const", const="admin-access", help="exercise webConfigurator login and tab presentation")
    focus.add_argument("--dashboard-system", dest="only", action="store_const", const="dashboard-system", help="exercise live system information and dashboard resource meters")
    focus.add_argument("--dashboard-preferences", dest="only", action="store_const", const="dashboard-preferences", help="exercise personal dashboard polling defaults and scoped layout reset")
    focus.add_argument("--dashboard", dest="only", action="store_const", const="dashboard", help="exercise per-operator dashboard layouts")
    focus.add_argument("--dns-access", dest="only", action="store_const", const="dns-access", help="exercise DNS Resolver Access Lists")
    focus.add_argument("--virtual-ips", dest="only", action="store_const", const="virtual-ips", help="exercise Virtual IP grants, aliases and NAT destinations")
    focus.add_argument("--dhcp-leases", dest="only", action="store_const", const="dhcp-leases", help="exercise typed DHCP lease status reads")
    focus.add_argument("--configuration-history", dest="only", action="store_const", const="configuration-history", help="exercise configuration history comparison")
    focus.add_argument("--nut", dest="only", action="store_const", const="nut", help="exercise UPS status, monitor settings and scoped grants")
    focus.add_argument("--fleet-controller", dest="only", action="store_const", const="fleet-controller", help="exercise controller approval and dashboard grants")
    focus.add_argument("--fleet", dest="only", action="store_const", const="fleet", help="exercise Fleet monitor status and package grants")
    focus.add_argument("--traffic-totals", dest="only", action="store_const", const="traffic-totals", help="exercise historical traffic views and collector settings")
    focus.add_argument("--dns-forwarders", dest="only", action="store_const", const="dns-forwarders", help="exercise upstream validation and configuration persistence")
    focus.add_argument("--dns-hosts", dest="only", action="store_const", const="dns-hosts", help="exercise resolver host overrides, structured aliases and independent grants")
    focus.add_argument("--mdns-bridge", dest="only", action="store_const", const="mdns-bridge", help="exercise only the mDNS Bridge package editor and API")
    focus.add_argument("--udp-relay", dest="only", action="store_const", const="udp-relay", help="exercise only the UDP Broadcast Relay package editor and API")
    focus.add_argument("--local-rules", dest="only", action="store_const", const="local-rules", help="exercise only ordered firewall rules for local multicast")
    focus.add_argument("--ingress-rules", dest="only", action="store_const", const="ingress-rules", help="exercise assigned-interface policy editing and scoped replacement")
    focus.add_argument("--interface-groups", dest="only", action="store_const", const="interface-groups", help="exercise interface group list, editor and independent grants")
    focus.add_argument("--port-aliases", dest="only", action="store_const", const="port-aliases", help="exercise nested port aliases and firewall selectors")
    focus.add_argument("--floating-rules", dest="only", action="store_const", const="floating-rules", help="exercise floating rule selection and AJAX validation")
    focus.add_argument("--group-rules", dest="only", action="store_const", const="group-rules", help="exercise unrestricted group targets and per-interface delegation")
    focus.add_argument("--opt-assignments", dest="only", action="store_const", const="opt-assignments", help="exercise the additive OPT assignment editor and API")
    focus.add_argument("--dns-lookup", dest="only", action="store_const", const="dns-lookup")
    focus.add_argument("--traceroute", dest="only", action="store_const", const="traceroute", help="exercise authorized Diagnostics Traceroute")
    focus.add_argument("--ping", dest="only", action="store_const", const="ping", help="exercise authorized Diagnostics Ping")
    focus.add_argument("--kernel-routes", dest="only", action="store_const", const="kernel-routes", help="exercise read-only kernel route diagnostics")
    focus.add_argument("--gateway-groups", dest="only", action="store_const", const="gateway-groups", help="exercise draft Gateway Groups management")
    focus.add_argument("--gateway-status", dest="only", action="store_const", const="gateway-status", help="exercise scoped gateway observations and monitor settings")
    focus.add_argument("--states", dest="only", action="store_const", const="states", help="exercise the local diagnostics state table, filters and authorization")
    focus.add_argument("--remote-syslog", dest="only", action="store_const", const="remote-syslog")
    focus.add_argument("--filter-log-summary", dest="only", action="store_const", const="filter-log-summary")
    focus.add_argument("--log-storage", dest="only", action="store_const", const="log-storage")
    focus.add_argument("--open-vm-tools", dest="only", action="store_const", const="open-vm-tools")
    focus.add_argument("--default-rule-log", dest="only", action="store_const", const="default-rule-log")
    focus.add_argument("--lldp", dest="only", action="store_const", const="lldp")
    focus.add_argument("--log-display", dest="only", action="store_const", const="log-display")
    focus.add_argument("--filter-log", dest="only", action="store_const", const="filter-log", help="exercise firewall log capture status, rows, filters and grants")
    focus.add_argument("--dhcp-mappings", dest="only", action="store_const", const="dhcp-mappings", help="exercise LAN DHCP static mappings and scoped grants")
    focus.add_argument("--tftp-backup", dest="only", action="store_const", const="tftp-backup", help="exercise local TFTP backup and restore")
    focus.add_argument("--ups-widget", dest="only", action="store_const", const="ups-widget", help="exercise installed UPS dashboard widget and permissions")
    arguments = parser.parse_args()
    main(evidence=arguments.evidence, only=arguments.only)
