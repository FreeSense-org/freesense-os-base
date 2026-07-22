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
python3 "${root}/scripts/channel.py" --public-key "${public_key}" \
  --channel devel --component system \
  --github-output "${RUNNER_TEMP}/system.out"

system_fingerprint=$(sed -n 's/^fingerprint=//p' "${RUNNER_TEMP}/system.out")
packages_fingerprint=$(sed -n 's/^packages_fingerprint=//p' "${RUNNER_TEMP}/system.out")
if [[ -z "${packages_fingerprint}" ]]; then
  echo "devel Packages are pending for the selected System."
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "ready=false" >>"${GITHUB_OUTPUT}"
  fi
  exit 0
fi

python3 "${root}/scripts/channel.py" --public-key "${public_key}" \
  --channel devel --component packages \
  --github-output "${RUNNER_TEMP}/packages.out"

packages_system=$(sed -n 's/^system_fingerprint=//p' "${RUNNER_TEMP}/packages.out")
selected_packages=$(sed -n 's/^fingerprint=//p' "${RUNNER_TEMP}/packages.out")
test -n "${system_fingerprint}"
test "${packages_system}" = "${system_fingerprint}"
test "${selected_packages}" = "${packages_fingerprint}"

for component in system packages; do
  output="${RUNNER_TEMP}/${component}.out"
  url=$(sed -n 's/^url=//p' "${output}")
  for catalog in meta.conf packagesite.pkg; do
    curl -fsS --retry 5 --retry-all-errors --proto '=https' \
      --user-agent 'FreeSense-build/1' --range 0-0 --output /dev/null \
      "${url}/${catalog}"
  done
done

for component in system packages; do
  output="${RUNNER_TEMP}/${component}.out"
  fingerprint=$(sed -n 's/^fingerprint=//p' "${output}")
  "${fsbuild}" channel verify --component "${component}" \
    --fingerprint "${fingerprint}" --private-key "${private_key}"
done

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "ready=true" >>"${GITHUB_OUTPUT}"
fi
