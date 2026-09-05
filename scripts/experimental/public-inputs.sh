# Transport adapters for the branch ISO experiment. All inputs are public;
# output stays in the disposable guest until copied to a GitHub artifact.
fetch_input() {
  object=$1 destination=$2
  fetch -qo "${destination}" "${PUBLIC_BASE_URL}/${object}"
  test "$(sha256 -q "${destination}")" = "${object##*/}"
}

fetch_repository() {
  kind=$1 id=$2 destination=$3
  test "${kind}" = system && test "${id}" = "${SYSTEM_ID}"
  url="${PUBLIC_BASE_URL}/artifacts/system/${id}/${PACKAGE_ARCH}"
  mkdir -p "${destination}/All"
  for metadata in meta.conf packagesite.pkg data.pkg; do
    fetch -qo "${destination}/${metadata}" "${url}/${metadata}"
  done
  catalog=$(mktemp -d /tmp/experiment-catalog.XXXXXX)
  tar -xf "${destination}/packagesite.pkg" -C "${catalog}" packagesite.yaml packagesite.yaml.sig
  digest=$(sha256 -q "${catalog}/packagesite.yaml")
  printf '%s' "${digest}" | openssl dgst -sha256 -verify /root/sign/repo.pub \
    -signature "${catalog}/packagesite.yaml.sig"
  jq -erR 'fromjson | .repopath | select(test("^All/[A-Za-z0-9_+.,~-]+[.]pkg$"))' \
    "${catalog}/packagesite.yaml" >"${catalog}/paths"
  while IFS= read -r path; do
    fetch -qo "${destination}/${path}" "${url}/${path}"
  done <"${catalog}/paths"
  verify_repository "${destination}"
  rm -rf "${catalog}"
}

upload_immutable() {
  # The production ISO stage calls this twice; reject any destination outside
  # the experiment directory, even if its recipe changes later.
  case "$2" in /root/experiment-output/*) : ;; *) return 1 ;; esac
  test "$(dirname "$2")" = /root/experiment-output
  cp "$1" "$2"
}
