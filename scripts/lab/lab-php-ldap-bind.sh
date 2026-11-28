#!/bin/sh
# Lab only: prove the PHP ldap extension and FreeSense\Auth\Servers\NativeLdapTransport against a throwaway OpenLDAP slapd on
# 127.0.0.1 (synthetic directory, random synthetic passwords, a private test CA). slapd is built from the same OpenLDAP source
# into a temporary directory and everything is removed on exit; the builder's installed state is not changed.
# usage: lab-php-ldap-bind.sh OPENLDAP_TARBALL FREESENSE_TREE [PHP]
#   FREESENSE_TREE holds the freesense src/ directory (a copy of the component); PHP defaults to /build/phpstage/usr/local/bin/php.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
tarball=$1; tree=$2; php=${3:-/build/phpstage/usr/local/bin/php}
test -f "$tree/src/usr/local/FreeSense/autoload.php"
t=$(mktemp -d /tmp/fs-slapd.XXXXXX)
pid=
# shellcheck disable=SC2329 # called by the trap below
cleanup() { if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; sleep 1; fi; rm -rf "$t"; }
trap cleanup EXIT INT TERM
port=38989; sport=38990

echo "[1] slapd for the test (temporary)"
mkdir -p "$t/src" "$t/data"
tar -C "$t/src" -xf "$tarball" --strip-components=1
( cd "$t/src" && LDFLAGS=-Wl,--undefined-version ./configure --prefix="$t/ol" --enable-slapd --enable-mdb --disable-modules \
	--without-cyrus-sasl --with-tls=openssl --without-fetch --disable-static > "$t/configure.log" 2>&1 \
	&& make depend > "$t/make.log" 2>&1 && make -j8 >> "$t/make.log" 2>&1 && make install > "$t/install.log" 2>&1 ) \
	|| { tail -20 "$t/make.log" 2>/dev/null; exit 1; }

echo "[2] test CA, server certificate, directory"
cd "$t"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout ca.key -out ca.pem -days 2 -subj '/CN=FreeSense LDAP test CA' 2>/dev/null
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout other.key -out other.pem -days 2 -subj '/CN=Unrelated CA' 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout server.key -out server.csr -subj '/CN=127.0.0.1' 2>/dev/null
printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\n' > san.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem -days 2 -extfile san.ext 2>/dev/null
root_pw=$(openssl rand -hex 16); svc_pw=$(openssl rand -hex 16); alice_pw=$(openssl rand -hex 16)
cat > slapd.conf <<EOF
include $t/ol/etc/openldap/schema/core.schema
include $t/ol/etc/openldap/schema/cosine.schema
include $t/ol/etc/openldap/schema/inetorgperson.schema
include $t/ol/etc/openldap/schema/nis.schema
pidfile $t/slapd.pid
TLSCACertificateFile $t/ca.pem
TLSCertificateFile $t/server.pem
TLSCertificateKeyFile $t/server.key
database mdb
maxsize 10485760
suffix "dc=example,dc=net"
rootdn "cn=admin,dc=example,dc=net"
rootpw $("$t/ol/sbin/slappasswd" -s "$root_pw")
directory $t/data
access to attrs=userPassword by self read by anonymous auth by * none
access to * by dn.exact="cn=svc,dc=example,dc=net" read by * none
EOF
chmod 0600 slapd.conf server.key ca.key other.key
"$t/ol/libexec/slapd" -f slapd.conf -h "ldap://127.0.0.1:$port/ ldaps://127.0.0.1:$sport/"
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s slapd.pid ] && break; sleep 1; done
pid=$(cat slapd.pid)
cat > data.ldif <<EOF
dn: dc=example,dc=net
objectClass: dcObject
objectClass: organization
o: Example
dc: example

dn: cn=svc,dc=example,dc=net
objectClass: person
cn: svc
sn: svc
userPassword: $("$t/ol/sbin/slappasswd" -s "$svc_pw")

dn: ou=Staff,dc=example,dc=net
objectClass: organizationalUnit
ou: Staff

dn: ou=Groups,dc=example,dc=net
objectClass: organizationalUnit
ou: Groups

dn: uid=alice,ou=Staff,dc=example,dc=net
objectClass: inetOrgPerson
uid: alice
cn: Alice
sn: Example
userPassword: $("$t/ol/sbin/slappasswd" -s "$alice_pw")

dn: cn=ops,ou=Groups,dc=example,dc=net
objectClass: posixGroup
cn: ops
gidNumber: 5000
memberUid: alice
EOF
( umask 077; printf '%s' "$root_pw" > root.pw )
"$t/ol/bin/ldapadd" -x -H "ldap://127.0.0.1:$port/" -D cn=admin,dc=example,dc=net -y root.pw -f data.ldif > ldapadd.log

echo "[3] NativeLdapTransport through $php"
cat > check.php <<'EOF'
<?php
declare(strict_types=1);
require getenv('FS_TREE') . '/src/usr/local/FreeSense/autoload.php';
use FreeSense\Auth\Servers\{AuthServer, LdapClient, LdapFailure, NativeLdapTransport};
$case = $argv[1];
$server = static fn(string $type): AuthServer => new AuthServer(['refid' => 'lab', 'name' => 'Lab', 'type' => 'ldap', 'host' => '127.0.0.1',
	'ldap_port' => getenv(str_starts_with($type, 'SSL') ? 'FS_LDAPS_PORT' : 'FS_PORT'), 'ldap_urltype' => $type, 'ldap_protver' => '3', 'ldap_timeout' => '5', 'ldap_scope' => 'subtree',
	'ldap_basedn' => 'dc=example,dc=net', 'ldap_authcn' => 'ou=Staff', 'ldap_attr_user' => 'uid', 'ldap_attr_member' => 'memberUid',
	'ldap_rfc2307' => '', 'ldap_rfc2307_basedn_groups' => '', 'ldap_attr_groupobj' => 'posixGroup', 'ldap_attr_group' => 'cn',
	'ldap_binddn' => 'cn=svc,dc=example,dc=net', 'ldap_bindpw' => getenv('FS_SVC_PW')]);
$client = new LdapClient(new NativeLdapTransport());
$ca = (string)file_get_contents(getenv('FS_CA'));
$other = (string)file_get_contents(getenv('FS_OTHER_CA'));
$ok = static function (bool $pass, string $what): void { echo ($pass ? 'PASS ' : 'FAIL ') . $what . PHP_EOL; if (!$pass) exit(1); };
$signIn = static function (string $type, ?string $pem, string $password) use ($client, $server) {
	try { return $client->authenticate($server($type), $pem, 'alice', $password); } catch (LdapFailure $f) { return $f; }
};
switch ($case) {
	case 'plain':
		$ok(NativeLdapTransport::available(), 'the ldap extension is loaded');
		$report = $client->check($server('Standard TCP'), null);
		$ok(array_column($report['steps'], 'ok') === [true, true, true] && $report['containers'] === ['ou=Groups,dc=example,dc=net', 'ou=Staff,dc=example,dc=net'], 'check: connect, service bind, containers');
		$id = $signIn('Standard TCP', null, getenv('FS_ALICE_PW'));
		$ok($id instanceof FreeSense\Auth\Servers\LdapIdentity && $id->dn === 'uid=alice,ou=Staff,dc=example,dc=net' && $id->groups === ['ops'], 'sign-in with RFC 2307 groups');
		$ok($signIn('Standard TCP', null, 'wrong') === null, 'a wrong password is refused');
		$ok($client->authenticate($server('Standard TCP'), null, 'nobody', 'x') === null, 'an unknown user is refused');
		$bad = new AuthServer(['ldap_bindpw' => 'wrong'] + $server('Standard TCP')->values);
		$ok($client->check($bad, null)['steps'][1]['ok'] === false, 'a wrong service password fails at the bind step');
		break;
	case 'starttls-good':
		$id = $signIn('STARTTLS Encrypted', $ca, getenv('FS_ALICE_PW'));
		$ok($id instanceof FreeSense\Auth\Servers\LdapIdentity && $id->groups === ['ops'], 'STARTTLS with the right authority signs in');
		break;
	case 'starttls-wrong':
		$f = $signIn('STARTTLS Encrypted', $other, getenv('FS_ALICE_PW'));
		$ok($f instanceof LdapFailure && $f->step === LdapFailure::TLS, 'STARTTLS with an unrelated authority is refused at the TLS step');
		break;
	case 'starttls-none':
		$f = $signIn('STARTTLS Encrypted', null, getenv('FS_ALICE_PW'));
		$ok($f instanceof LdapFailure && $f->step === LdapFailure::TLS, 'STARTTLS without an authority (no system trust for the test CA) is refused');
		break;
	case 'ldaps-good':
		$id = $signIn('SSL/TLS Encrypted', $ca, getenv('FS_ALICE_PW'));
		$ok($id instanceof FreeSense\Auth\Servers\LdapIdentity && $id->groups === ['ops'], 'LDAPS with the right authority signs in');
		break;
	case 'ldaps-wrong':
		$f = $signIn('SSL/TLS Encrypted', $other, getenv('FS_ALICE_PW'));
		$ok($f instanceof LdapFailure, 'LDAPS with an unrelated authority is refused');
		break;
	case 'starttls-good-then-wrong':
		// One process, two servers with different authorities: the second must not reuse the first authority.
		$first = $signIn('STARTTLS Encrypted', $ca, getenv('FS_ALICE_PW'));
		$second = $signIn('STARTTLS Encrypted', $other, getenv('FS_ALICE_PW'));
		$ok($first instanceof FreeSense\Auth\Servers\LdapIdentity, 'same process: right authority first');
		$ok($second instanceof LdapFailure && $second->step === LdapFailure::TLS, 'same process: an unrelated authority afterwards is refused');
		break;
	case 'starttls-wrong-then-good':
		$first = $signIn('STARTTLS Encrypted', $other, getenv('FS_ALICE_PW'));
		$second = $signIn('STARTTLS Encrypted', $ca, getenv('FS_ALICE_PW'));
		$ok($first instanceof LdapFailure, 'same process: unrelated authority first is refused');
		$ok($second instanceof FreeSense\Auth\Servers\LdapIdentity, 'same process: the right authority afterwards signs in');
		break;
}
EOF
export FS_TREE="$tree" FS_PORT=$port FS_LDAPS_PORT=$sport FS_SVC_PW="$svc_pw" FS_ALICE_PW="$alice_pw" FS_CA="$t/ca.pem" FS_OTHER_CA="$t/other.pem" TMPDIR=/tmp
rc=0
for c in plain starttls-good starttls-wrong starttls-none ldaps-good ldaps-wrong starttls-good-then-wrong starttls-wrong-then-good; do
	echo "-- $c"; "$php" check.php "$c" || rc=1
done
leftover=$(find /tmp -maxdepth 1 -name 'fs-ldap-ca-*' | wc -l | tr -d ' ')
echo "temporary CA files left behind: $leftover"
exit $rc
