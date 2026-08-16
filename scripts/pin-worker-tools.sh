#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: pin-worker-tools.sh --catalog FILE --freebsd-sha SHA --build-date YYYYMMDD --output FILE --report FILE" >&2
  exit 2
}

catalog=""
freebsd_sha=""
build_date=""
output=""
report=""
while (($#)); do
  case "$1" in
    --catalog) catalog=${2:-}; shift 2 ;;
    --freebsd-sha) freebsd_sha=${2:-}; shift 2 ;;
    --build-date) build_date=${2:-}; shift 2 ;;
    --output) output=${2:-}; shift 2 ;;
    --report) report=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -s $catalog ]] || usage
[[ $freebsd_sha =~ ^[0-9a-f]{40}$ ]] || usage
[[ $build_date =~ ^[0-9]{8}$ ]] || usage
[[ -n $output && -n $report ]] || usage
bundle_mtime="${build_date:0:4}-${build_date:4:2}-${build_date:6:2} 00:00:00 UTC"
[[ $(date -u --date="$bundle_mtime" +%Y%m%d) == "$build_date" ]] || usage

repository=https://pkg.freebsd.org/FreeBSD:16:amd64/latest
work=$(mktemp -d "${TMPDIR:-/tmp}/freesense-worker-tools.XXXXXX")
output_part=${output}.part
report_part=${report}.part
cleanup() {
  status=$?
  trap - EXIT
  rm -rf -- "$work"
  rm -f -- "$output_part" "$report_part"
  if ((status != 0)); then
    rm -f -- "$output" "$report"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rm -f -- "$output" "$report" "$output_part" "$report_part"
mkdir -p "$(dirname "$output")" "$(dirname "$report")"

tar --zstd -xf "$catalog" -C "$work" \
  packagesite.yaml packagesite.yaml.sig packagesite.yaml.pub
for file in packagesite.yaml packagesite.yaml.sig packagesite.yaml.pub; do
  [[ -f $work/$file && ! -L $work/$file ]] || {
    echo "official package catalogue member is invalid: $file" >&2
    exit 1
  }
done

retry=(--fail --location --silent --show-error --retry 5 --retry-all-errors --proto '=https')
curl "${retry[@]}" --output "$work/trusted" \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${freebsd_sha}/share/keys/pkg/trusted/pkg.freebsd.org.2013102301"
expected_key=$(sed -nE 's/^fingerprint: "([0-9a-f]{64})"$/\1/p' "$work/trusted")
[[ $expected_key =~ ^[0-9a-f]{64}$ ]]
catalog_key=$(sha256sum "$work/packagesite.yaml.pub" | awk '{print $1}')
[[ $catalog_key == "$expected_key" ]] || {
  echo "official package catalogue key differs from the pinned FreeBSD trust root" >&2
  exit 1
}
catalog_digest=$(sha256sum "$work/packagesite.yaml" | awk '{print $1}')
printf '%s' "$catalog_digest" | openssl dgst -sha256 \
  -verify "$work/packagesite.yaml.pub" \
  -signature "$work/packagesite.yaml.sig" >/dev/null

python3 scripts/resolve_worker_tools.py resolve \
  --catalog "$work/packagesite.yaml" --output "$work/manifest.json"
ports_sha=$(jq -er '.ports_sha | select(test("^[0-9a-f]{40}$"))' "$work/manifest.json")
osversion=$(jq -er '.osversion | select(type == "number")' "$work/manifest.json")

bundle_root=$work/bundle
mkdir -p "$bundle_root/All"
jq -er '.packages[] | [.remote_path, .local_file] | @tsv' \
  "$work/manifest.json" | while IFS=$'\t' read -r remote_path local_file; do
    [[ -n $remote_path && $local_file == All/*.pkg && $local_file != */*/*.pkg ]]
    curl "${retry[@]}" --output "$bundle_root/$local_file" \
      "$repository/$remote_path"
  done
python3 scripts/resolve_worker_tools.py verify \
  --manifest "$work/manifest.json" --directory "$bundle_root"
jq -er '.install_order[]' "$work/manifest.json" >"$bundle_root/install-order"
jq -er '.commands[]' "$work/manifest.json" >"$bundle_root/required-tools"
jq -er '.osversion | select(type == "number")' "$work/manifest.json" >"$bundle_root/required-osversion"
cp "$work/manifest.json" "$bundle_root/manifest.json"

LC_ALL=C tar --sort=name --format=pax --mtime="$bundle_mtime" \
  --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
  --pax-option=delete=atime,delete=ctime \
  -cf "$output_part" -C "$bundle_root" .
mv "$output_part" "$output"

jq -n \
  --arg ports_sha "$ports_sha" \
  --argjson osversion "$osversion" \
  '{schema_version:"freesense.worker-tools-pin/v1",ports_sha:$ports_sha,osversion:$osversion}' \
  >"$report_part"
mv "$report_part" "$report"
