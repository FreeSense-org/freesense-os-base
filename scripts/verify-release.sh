#!/usr/bin/env bash
set -euo pipefail

if (($# != 3)); then
  echo "usage: verify-release.sh CHANNEL_PUBLIC_KEY CHANNEL_PRIVATE_KEY FSBUILD" >&2
  exit 2
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
public_key=$1
private_key=$2
fsbuild=$3

rm -f "${RUNNER_TEMP}/system.out" "${RUNNER_TEMP}/packages.out"
for component in system packages; do
  python3 "${root}/scripts/channel.py" --public-key "${public_key}" \
    --channel devel --component "${component}" \
    --github-output "${RUNNER_TEMP}/${component}.out"
done

system_fingerprint=$(sed -n 's/^fingerprint=//p' "${RUNNER_TEMP}/system.out")
packages_system=$(sed -n 's/^system_fingerprint=//p' "${RUNNER_TEMP}/packages.out")
test -n "${system_fingerprint}"
test "${packages_system}" = "${system_fingerprint}"

for component in system packages; do
  output="${RUNNER_TEMP}/${component}.out"
  fingerprint=$(sed -n 's/^fingerprint=//p' "${output}")
  url=$(sed -n 's/^url=//p' "${output}")
  marker=$(curl -fsS --retry 5 --retry-all-errors --proto '=https' \
    --user-agent 'FreeSense-build/1' "${url%/amd64}/complete.json")
  jq -e --arg component "${component}" --arg fingerprint "${fingerprint}" \
    --arg system "${system_fingerprint}" \
    '.schema_version == "freesense.artifact/v1" and
     .stage == $component and .fingerprint == $fingerprint and
     ($component != "packages" or .inputs.system == $system)' \
    <<<"${marker}" >/dev/null
  for catalog in meta.conf packagesite.pkg; do
    curl -fsS --retry 5 --retry-all-errors --proto '=https' \
      --user-agent 'FreeSense-build/1' --range 0-0 --output /dev/null \
      "${url}/${catalog}"
  done
  "${fsbuild}" channel verify --component "${component}" \
    --fingerprint "${fingerprint}" --private-key "${private_key}"
done
