#!/usr/bin/env bash
set -euo pipefail

if (($# != 1)); then
  echo "usage: publish_iso.sh RELEASE_DOCUMENT" >&2
  exit 2
fi
for name in R2_BUCKET R2_ENDPOINT RUNNER_TEMP; do
  [[ -n "${!name:-}" ]] || { echo "${name} is required" >&2; exit 2; }
done

document=$1
jq -e '.schema_version == "freesense.download/v1"' "$document" >/dev/null
iso=$(jq -er '.iso' "$document")
expected_sha=$(jq -er '.sha256' "$document")
expected_size=$(jq -er '.size' "$document")
marker_url=$(jq -er '.marker_url' "$document")
public_url=$(jq -er '.url' "$document")
source_url="${marker_url%/complete.json}/${iso}"
((expected_size <= 5 * 1024 * 1024 * 1024)) || {
  echo "ISO exceeds the single immutable PutObject limit" >&2
  exit 1
}

case "$public_url" in
  https://downloads.freesense.org/v1/releases/*)
    object=${public_url#https://downloads.freesense.org/}
    ;;
  *)
    echo "release document has a non-canonical downloads URL" >&2
    exit 1
    ;;
esac
[[ "$object" != *..* && "$object" != *//* ]]

head_file="${RUNNER_TEMP}/download-head.json"
if aws s3api head-object --bucket "$R2_BUCKET" --key "$object" \
    --endpoint-url "$R2_ENDPOINT" >"$head_file" 2>/dev/null; then
  jq -e --arg sha "$expected_sha" --argjson size "$expected_size" \
    '.ContentLength == $size and .Metadata.sha256 == $sha' "$head_file" >/dev/null || {
      echo "refusing to overwrite a conflicting downloads object" >&2
      exit 1
    }
else
  iso_path="${RUNNER_TEMP}/${iso}"
  curl --fail --silent --show-error --location --retry 5 --retry-all-errors \
    --output "$iso_path" "$source_url"
  [[ "$(stat -c %s "$iso_path")" == "$expected_size" ]]
  printf '%s  %s\n' "$expected_sha" "$iso_path" | sha256sum --check --status
  aws s3api put-object --bucket "$R2_BUCKET" --key "$object" --body "$iso_path" \
    --endpoint-url "$R2_ENDPOINT" \
    --content-type application/x-iso9660-image \
    --cache-control 'public, max-age=31536000, immutable' \
    --metadata "sha256=${expected_sha}" >/dev/null
fi

for attempt in {1..12}; do
  headers=$(curl --silent --show-error --location --head \
    "${public_url}?published=${GITHUB_RUN_ID:-local}" || true)
  status=$(sed -nE 's/^HTTP\/[0-9.]+ ([0-9]{3}).*/\1/p' <<<"$headers" | tail -n1)
  length=$(sed -nE 's/^[Cc]ontent-[Ll]ength: *([0-9]+)\r?$/\1/p' <<<"$headers" | tail -n1)
  if [[ "$status" == 200 && "$length" == "$expected_size" ]]; then
    exit 0
  fi
  [[ "$attempt" == 12 ]] || sleep 5
done
echo "downloads object did not become publicly readable" >&2
exit 1
