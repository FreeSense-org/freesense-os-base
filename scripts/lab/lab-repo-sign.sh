#!/bin/sh
# Lab only: signing command for `pkg repo` that the installer's pinned-key check accepts (signature_type "fingerprints").
#   pkg-static repo /path/to/repo signing_command: /path/to/lab-repo-sign.sh
#   fingerprint for freesense-install:  sha256 -q "$FS_REPO_PUBLIC_KEY"
# pkg passes the hex SHA-256 of the catalogue on standard input and expects the signature and the public key back. The key text is
# printed exactly as the file holds it, because pkg fingerprints that text as it comes (an extra blank line changes the fingerprint).
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
: "${FS_REPO_PRIVATE_KEY:?path of the RSA private key}"
: "${FS_REPO_PUBLIC_KEY:?path of the matching public key}"
IFS= read -r sum
[ -n "$sum" ] || exit 1
echo SIGNATURE
printf '%s' "$sum" | openssl dgst -sign "$FS_REPO_PRIVATE_KEY" -sha256 -binary
echo
echo CERT
cat "$FS_REPO_PUBLIC_KEY"
echo END
