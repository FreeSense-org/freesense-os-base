#!/bin/sh
# On the builder: serve the signed repository over TLS with a throwaway certificate and install from it with the real installer.
set -u
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
pkill -f "s_server -WWW" 2>/dev/null; sleep 1
rm -rf /build/tls; mkdir -p /build/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -keyout /build/tls/key.pem -out /build/tls/cert.pem -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1" 2>/dev/null
(cd /build/repo-signed && nohup openssl s_server -WWW -accept 8443 -cert /build/tls/cert.pem -key /build/tls/key.pem -quiet > /build/tls/server.log 2>&1 &)
sleep 2
FP=$(sha256 -q /build/sign/repo.pub)
sed -e "s#\"repository\":\"[^\"]*\"#\"repository\":\"https://127.0.0.1:8443\",\"fingerprint\":\"$FP\"#" /build/cfg-restore.json > /build/cfg-https-good.json
sed -e "s#\"repository\":\"[^\"]*\"#\"repository\":\"https://127.0.0.1:8443\",\"fingerprint\":\"$(printf x | sha256 -q)\"#" /build/cfg-restore.json > /build/cfg-https-wrong.json
echo "--- https, certificate not trusted"
SSL_CA_CERT_FILE=/etc/ssl/does-not-exist sh /build/fullinst.sh httpsnotrust /build/cfg-https-good.json 1 2>&1 | grep -E "install rc=|Installer failed" | head -2
echo "--- https, certificate trusted, right fingerprint"
SSL_CA_CERT_FILE=/build/tls/cert.pem sh /build/fullinst.sh httpsgood /build/cfg-https-good.json 1 2>&1 | grep -E "install rc=|Installer failed" | head -2
echo "--- https, certificate trusted, wrong fingerprint"
SSL_CA_CERT_FILE=/build/tls/cert.pem sh /build/fullinst.sh httpswrong /build/cfg-https-wrong.json 1 2>&1 | grep -E "install rc=|Installer failed" | head -2
pkill -f "s_server -WWW" 2>/dev/null
echo https-done
