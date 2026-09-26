# Publish a frozen subset of FreeBSD's signed catalogue as a FreeSense-signed
# repository: the bottom layer of the layered repository stack.
#
# This stage builds nothing. It materialises exactly the packages the sealed
# mirror plan names, verifies each against the checksum recorded when FreeBSD's
# signature over the catalogue was checked at pin time, and lets pkg generate and
# sign the catalogue. A subset cannot keep upstream's signature -- that covers
# packagesite.yaml whole -- so the mirror is re-signed with the FreeSense key and
# the chain back to FreeBSD is carried in mirror-provenance.json instead.
phase mirror-source
clone_exact https://github.com/FreeSense-org/freesense.git /root/freesense-src "${SOURCE_SHA}"
clone_exact https://github.com/FreeSense-org/freesense-os-base.git /root/os-definition "${OS_BASE_SHA}"
configure_signing

phase mirror-plan
case "${MIRROR_PLAN_OBJECT}" in
  inputs/sha256/*) : ;;
  *) echo "the mirror requires a pinned plan object" >&2; exit 1 ;;
esac
fetch_input "${MIRROR_PLAN_OBJECT}" /root/mirror-plan.json
jq -e --arg abi "${ABI}" --arg architecture "${ARCHITECTURE}" '
  .schema_version == "freesense.mirror-plan/v1" and .abi == $abi and
  .architecture == $architecture and (.packages | type == "array" and length > 0)
' /root/mirror-plan.json >/dev/null || {
  echo "the mirror plan does not describe this target" >&2
  exit 1
}

mirror=/root/work/mirror
rm -rf "${mirror}"
mkdir -p "${mirror}"
phase mirror-fetch
python3 /root/os-definition/scripts/mirror_fetch.py \
  --plan /root/mirror-plan.json --destination "${mirror}" \
  --provenance "${mirror}/mirror-provenance.json"
phase mirror-fetched

# pkg generates the catalogue from the packages themselves and signs it through
# the same hook the System and Optional repositories use, so a client that
# trusts those trusts this one on exactly the same terms.
sign_repository "${mirror}"
verify_repository "${mirror}"
publish_mirror "${mirror}"
