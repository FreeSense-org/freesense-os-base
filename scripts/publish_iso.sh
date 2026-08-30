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
jq -e '.schema_version == "freesense.download/v2" or .schema_version == "freesense.download/v3"' "$document" >/dev/null

count=$(jq '.artifacts | length' "$document")
((count == 1 || count == 3 || count == 5)) || {
  echo "release document must contain 1, 3, or 5 artifacts" >&2
  exit 1
}
for ((index=0; index<count; index++)); do
  file=$(jq -er ".artifacts[$index].file" "$document")
  expected_sha=$(jq -er ".artifacts[$index].sha256" "$document")
  expected_size=$(jq -er ".artifacts[$index].size" "$document")
  marker_url=$(jq -er ".artifacts[$index].marker_url" "$document")
  public_url=$(jq -er ".artifacts[$index].url" "$document")
  format=$(jq -er ".artifacts[$index].format" "$document")
  compression=$(jq -er ".artifacts[$index].compression" "$document")
  source_url="${marker_url%/complete.json}/${file}"
  ((expected_size <= 5 * 1024 * 1024 * 1024)) || {
    echo "release artifact exceeds the single immutable PutObject limit" >&2
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

  head_file="${RUNNER_TEMP}/download-head-${index}.json"
  if aws s3api head-object --bucket "$R2_BUCKET" --key "$object" \
    --endpoint-url "$R2_ENDPOINT" >"$head_file" 2>/dev/null; then
    jq -e --arg sha "$expected_sha" --argjson size "$expected_size" \
      '.ContentLength == $size and .Metadata.sha256 == $sha' "$head_file" >/dev/null || {
      echo "refusing to overwrite a conflicting downloads object" >&2
      exit 1
    }
  else
    artifact_path="${RUNNER_TEMP}/${file}"
    curl --fail --silent --show-error --location --retry 5 --retry-all-errors \
      --output "$artifact_path" "$source_url"
    [[ "$(stat -c %s "$artifact_path")" == "$expected_size" ]]
    printf '%s  %s\n' "$expected_sha" "$artifact_path" | sha256sum --check --status
    content_type=application/octet-stream
    [[ "$format" == iso ]] && content_type=application/x-iso9660-image
    [[ "$compression" == xz ]] && content_type=application/x-xz
    aws s3api put-object --bucket "$R2_BUCKET" --key "$object" --body "$artifact_path" \
      --endpoint-url "$R2_ENDPOINT" --content-type "$content_type" \
      --cache-control 'public, max-age=31536000, immutable' \
      --metadata "sha256=${expected_sha}" >/dev/null
  fi

  verified=false
  for attempt in {1..12}; do
    headers=$(curl --silent --show-error --location --head \
      "${public_url}?published=${GITHUB_RUN_ID:-local}" || true)
    status=$(sed -nE 's/^HTTP\/[0-9.]+ ([0-9]{3}).*/\1/p' <<<"$headers" | tail -n1)
    length=$(sed -nE 's/^[Cc]ontent-[Ll]ength: *([0-9]+)\r?$/\1/p' <<<"$headers" | tail -n1)
    if [[ "$status" == 200 && "$length" == "$expected_size" ]]; then
      verified=true
      break
    fi
    [[ "$attempt" == 12 ]] || sleep 5
  done
  if [[ "$verified" != true ]]; then
    echo "downloads object did not become publicly readable: ${file}" >&2
    exit 1
  fi
done
