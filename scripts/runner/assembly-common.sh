# Shared release-root inputs. ISO and cloud stages consume the same sealed
# System repository and exact signed System/Optional Packages channel payload.
prepare_release_inputs() {
  fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
  configure_source
  fetch_repository system "${SYSTEM_ID}" /root/system-repo
}

verify_release_channel() {
  phase channel-fetch
  printf '%s' "${CHANNEL_PAYLOAD_B64}" | openssl base64 -d -A >/tmp/channel-payload.json
  printf '%s' "${CHANNEL_SIGNATURE_B64}" | openssl base64 -d -A >/tmp/channel-signature.bin
  openssl dgst -sha256 -verify /root/sign/channel-public.pem \
    -signature /tmp/channel-signature.bin /tmp/channel-payload.json >/dev/null
  test "$(sha256 -q /tmp/channel-payload.json)" = "${CHANNEL_PAYLOAD_SHA256}"
  jq -e --arg channel "${CHANNEL}" --arg system "${SYSTEM_ID}" \
    --arg packages "${PACKAGES_ID}" \
    --arg train "${PACKAGE_TRAIN}" --argjson system_generation "${SYSTEM_GENERATION}" \
    '.schema_version == "freesense.channels/v3" and
     .channels[$channel].package_train == $train and
     .channels[$channel].system.fingerprint == $system and
     .channels[$channel].system.generation == $system_generation and
     .channels[$channel].system.verified == true and
     (.channels[$channel].packages | type) == "object" and
     .channels[$channel].packages.fingerprint == $packages and
     .channels[$channel].packages.verified == true and
     .channels[$channel].packages.system_fingerprint == $system' \
    /tmp/channel-payload.json >/dev/null
  export FREESENSE_ASSEMBLY_CHANNEL="${CHANNEL}"
  export FREESENSE_ASSEMBLY_CHANNEL_PAYLOAD=/tmp/channel-payload.json
  phase channel-ready
}
