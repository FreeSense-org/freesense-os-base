# Assemble only from the exact signed repositories selected by the channel.
prepare_release_inputs
verify_release_channel
cd /root/freesense-src
grep -Fqx '# FREESENSE_ISO_ASSEMBLY_API=2' \
  tools/ci/freesense-assemble-iso.sh || {
  echo "unsupported ISO assembler source: ${SOURCE_SHA}" >&2
  exit 1
}
# The sealed System repository must remain the sole package input, but the
# installer boot environment belongs to the ISO recipe. Overlay dual-console
# settings here so headless KVM can observe the installer without hard-coding
# the BIOS or UEFI video-console implementation.
phase iso-console-overlay
assembler=tools/ci/freesense-assemble-iso.sh
overlay=/tmp/freesense-iso-console-overlay
transformed=/tmp/freesense-assemble-iso.sh
test "$(grep -Ec '^[[:space:]]*install_assembly_channel$' "${assembler}")" = 1
cat >"${overlay}" <<'EOF'

	# The source repository follows the rolling version, but this image is bound
	# to the explicit checked release input. The sealed 1.0.0 System package also
	# predates lifecycle-suffix normalization, so patch both image roots before
	# validating the baked channel. Future fixed scripts remain unchanged.
	for _repoc_root in "${FINAL_CHROOT_DIR}" "${INSTALLER_CHROOT_DIR}"; do
		printf '%s\n' "${PRODUCT_VERSION}" >"${_repoc_root}/etc/version"
		chmod 0644 "${_repoc_root}/etc/version"
		for _repoc_name in "${PRODUCT_NAME}-repoc" "${PRODUCT_NAME}-repoc-static"; do
			_repoc="${_repoc_root}/usr/local/sbin/${_repoc_name}"
			test -x "${_repoc}" || {
				echo ">>> ERROR: ISO compatibility overlay is missing ${_repoc_name}" >&2
				return 1
			}
			if grep -Fq 'INSTALLED_VERSION="${INSTALLED_VERSION%%-*}"' "${_repoc}"; then
				continue
			fi
			_repoc_tmp="${_repoc}.iso.$$"
			awk '
				{ print }
				$0 == "[ -n \"${INSTALLED_VERSION}\" ] || INSTALLED_VERSION=\"0.0.0\"" {
					print "INSTALLED_VERSION=\"${INSTALLED_VERSION%%-*}\""
					found = 1
				}
				END { if (!found) exit 42 }
			' "${_repoc}" >"${_repoc_tmp}" || {
				rm -f "${_repoc_tmp}"
				echo ">>> ERROR: could not apply the v1.0 repoc compatibility overlay" >&2
				return 1
			}
			install -o root -g wheel -m 0555 "${_repoc_tmp}" "${_repoc}"
			rm -f "${_repoc_tmp}"
		done
	done

	: "${FREESENSE_ASSEMBLY_INSTALLER_OVERLAY:?installer overlay is required}"
	_installer_overlay="${FREESENSE_ASSEMBLY_INSTALLER_OVERLAY}"
	mkdir -p "${INSTALLER_CHROOT_DIR}/usr/libexec/bsdinstall"
	for _installer_script in auto config zfsboot copy_configxml_from_usb fix_fstab; do
		test -s "${_installer_overlay}/scripts/${_installer_script}" || {
			echo ">>> ERROR: installer overlay is missing ${_installer_script}" >&2
			return 1
		}
		install -o root -g wheel -m 0555 \
			"${_installer_overlay}/scripts/${_installer_script}" \
			"${INSTALLER_CHROOT_DIR}/usr/libexec/bsdinstall/${_installer_script}"
	done
	test -s "${_installer_overlay}/startbsdinstall" || {
		echo ">>> ERROR: installer overlay is missing startbsdinstall" >&2
		return 1
	}
	install -o root -g wheel -m 0555 "${_installer_overlay}/startbsdinstall" \
		"${INSTALLER_CHROOT_DIR}/usr/sbin/startbsdinstall"
	grep -Fq 'FreeSense - Copyright and License Notice' \
		"${INSTALLER_CHROOT_DIR}/usr/sbin/startbsdinstall" || {
		echo ">>> ERROR: startbsdinstall was not FreeSense-branded" >&2
		return 1
	}

	# The exact-repository assembler seeds a clean installer root and therefore
	# does not pass through builder_common's normal installer-helper staging.
	# Install both recovery entry points explicitly. startbsdinstall deliberately
	# hides their menu items unless these files are executable, so missing helpers
	# must fail the image instead of producing an apparently valid ISO.
	mkdir -p "${INSTALLER_CHROOT_DIR}/root"
	for _recovery_helper in recover_configxml.sh import_foreign_config.sh; do
		test -s "${BUILDER_TOOLS}/installer/${_recovery_helper}" || {
			echo ">>> ERROR: installer recovery helper is missing: ${_recovery_helper}" >&2
			return 1
		}
		install -o root -g wheel -m 0555 \
			"${BUILDER_TOOLS}/installer/${_recovery_helper}" \
			"${INSTALLER_CHROOT_DIR}/root/${_recovery_helper}"
	done
	test -s "${PRODUCT_SRC}/etc/config_import_pkgmap.map" || {
		echo ">>> ERROR: installer foreign-config package map is missing" >&2
		return 1
	}
	install -o root -g wheel -m 0444 \
		"${PRODUCT_SRC}/etc/config_import_pkgmap.map" \
		"${INSTALLER_CHROOT_DIR}/root/config_import_pkgmap.map"
	for _recovery_helper in recover_configxml.sh import_foreign_config.sh; do
		test -x "${INSTALLER_CHROOT_DIR}/root/${_recovery_helper}" || {
			echo ">>> ERROR: installer recovery helper was not installed: ${_recovery_helper}" >&2
			return 1
		}
	done
	test -r "${INSTALLER_CHROOT_DIR}/root/config_import_pkgmap.map" || {
		echo ">>> ERROR: installer foreign-config package map was not installed" >&2
		return 1
	}

	# Keep the graphical installer while also exposing its deterministic boot
	# readiness marker to headless release smoke tests. Let each loader choose
	# its native video console (efi for UEFI, vidconsole for BIOS); -Dh enables
	# both video and serial with serial primary without naming the wrong one.
	cat > "${INSTALLER_CHROOT_DIR}/boot.config" <<'CONSOLE_EOF'
-S115200 -Dh
CONSOLE_EOF
	cat > "${INSTALLER_CHROOT_DIR}/boot/loader.conf" <<'CONSOLE_EOF'
autoboot_delay="3"
kern.cam.boot_delay=10000
boot_multicons="YES"
boot_serial="YES"
comconsole_speed="115200"
CONSOLE_EOF
EOF
awk -v overlay="${overlay}" '
  /^[[:space:]]*install_assembly_channel$/ {
    while ((getline line < overlay) > 0) print line
    close(overlay)
  }
  { print }
' "${assembler}" >"${transformed}"
cat "${transformed}" >"${assembler}"
rm -f "${overlay}" "${transformed}"
grep -Fqx -- '-S115200 -Dh' \
  "${assembler}" || {
  echo "ISO console overlay was not applied" >&2
  exit 1
}
if grep -Fq 'console="comconsole,vidconsole"' "${assembler}"; then
  echo "ISO console overlay hard-codes the BIOS-only video console" >&2
  exit 1
fi
grep -Fq 'INSTALLED_VERSION="${INSTALLED_VERSION%%-*}"' \
  "${assembler}" || {
  echo "ISO v1.0 repoc compatibility overlay was not applied" >&2
  exit 1
}
grep -Fq '"${_repoc_root}/etc/version"' \
  "${assembler}" || {
  echo "ISO release version stamp was not applied" >&2
  exit 1
}
phase iso-tools-fetch
mkdir -p "/root/freebsd-tools/release/${FREEBSD_TARGET}" /root/freebsd-tools/release/scripts
if [ "${ARCHITECTURE}" = arm64 ]; then
  fetch -qo /root/freebsd-tools/release/arm64/make-memstick.sh \
    "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/arm64/make-memstick.sh"
else
  fetch -qo /root/freebsd-tools/release/amd64/mkisoimages.sh \
    "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/amd64/mkisoimages.sh"
fi
fetch -qo /root/freebsd-tools/release/scripts/make-manifest.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/scripts/make-manifest.sh"
fetch -qo /root/freebsd-tools/release/scripts/tools.subr \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/scripts/tools.subr"
mkdir -p /root/freebsd-tools/tools/boot
fetch -qo /root/freebsd-tools/tools/boot/install-boot.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/tools/boot/install-boot.sh"
if [ "${ARCHITECTURE}" = arm64 ]; then
  test -s /root/freebsd-tools/release/arm64/make-memstick.sh
else
  test -s /root/freebsd-tools/release/amd64/mkisoimages.sh
fi
test -s /root/freebsd-tools/release/scripts/make-manifest.sh
test -s /root/freebsd-tools/release/scripts/tools.subr
test -s /root/freebsd-tools/tools/boot/install-boot.sh

# Materialize only the installer shell sources touched by the canonical
# FreeSense patch.  The base files come from the pinned FreeBSD commit and the
# patch bytes are embedded in (and fingerprinted with) this ISO recipe.
phase iso-installer-overlay
installer_source=/root/freebsd-installer
installer_patch=/tmp/freesense-installer.patch
mkdir -p "${installer_source}/usr.sbin/bsdinstall/scripts"
for installer_path in \
  usr.sbin/bsdinstall/scripts/Makefile \
  usr.sbin/bsdinstall/scripts/auto \
  usr.sbin/bsdinstall/scripts/config \
  usr.sbin/bsdinstall/scripts/zfsboot \
  usr.sbin/bsdinstall/startbsdinstall; do
  fetch -qo "${installer_source}/${installer_path}" \
    "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/${installer_path}"
  test -s "${installer_source}/${installer_path}"
done
printf '%s' "${FREESENSE_INSTALLER_PATCH_B64}" | \
  openssl base64 -d -A >"${installer_patch}"
test -s "${installer_patch}"
(cd "${installer_source}" && \
  git apply --check "${installer_patch}" && \
  git apply "${installer_patch}")
sh tools/ci/fs-rebrand-installer.sh \
  "${installer_source}/usr.sbin/bsdinstall/startbsdinstall"
for installer_path in \
  scripts/auto scripts/config scripts/zfsboot \
  scripts/copy_configxml_from_usb scripts/fix_fstab startbsdinstall; do
  test -s "${installer_source}/usr.sbin/bsdinstall/${installer_path}"
done
grep -Fq 'FreeSense - Copyright and License Notice' \
  "${installer_source}/usr.sbin/bsdinstall/startbsdinstall"
rm -f "${installer_patch}"
export FREESENSE_ASSEMBLY_INSTALLER_OVERLAY="${installer_source}/usr.sbin/bsdinstall"
export FREESENSE_ASSEMBLY_SYSTEM_REPO=/root/system-repo
export FREESENSE_ASSEMBLY_FREEBSD_SRC=/root/freebsd-tools

# Pin both filesystem timestamps and hybrid GPT identifiers to the source
# commit. mkisoimages invokes makefs directly for its EFI image, so PATH
# wrappers cover that call as well as the final makefs and mkimg calls.
real_makefs=$(command -v makefs)
real_mkimg=$(command -v mkimg)
mkdir -p /root/deterministic-tools
cat >/root/deterministic-tools/makefs <<EOF
#!/bin/sh
exec "${real_makefs}" -T "${FREESENSE_SOURCE_COMMIT_TIME}" "\$@"
EOF
cat >/root/deterministic-tools/mkimg <<EOF
#!/bin/sh
exec "${real_mkimg}" -t "${FREESENSE_SOURCE_COMMIT_TIME}" "\$@"
EOF
chmod 555 /root/deterministic-tools/makefs /root/deterministic-tools/mkimg
PATH="/root/deterministic-tools:${PATH}"
export PATH
phase iso-assemble
./build.sh --assemble-iso
phase iso-ready
image=$(find tmp -type f -name "*.${INSTALLER_FORMAT}" -print -quit)
test -n "${image}" && test -s "${image}"
release_version=${PRODUCT_VERSION%%-*}
if [ "${CHANNEL}" = stable ]; then
  prefix="FreeSense-${release_version}"
else
  prefix="FreeSense-${release_version}-g${GENERATION}"
fi
if [ "${INSTALLER_FORMAT}" = img ]; then
  name="${prefix}-${ARCHITECTURE}-installer.img.xz"
  xz -T0 -9 -c "${image}" >"/root/${name}"
  image="/root/${name}"
else
  name="${prefix}-${ARCHITECTURE}.iso"
fi
sha=$(sha256 -q "${image}")
size=$(stat -f %z "${image}")
phase iso-publish
upload_immutable "${image}" "${RESULT}/${name}"
jq -n --arg fingerprint "${FINGERPRINT}" --arg sha256 "${sha}" --arg file "${name}" \
  --arg bundle "${BUNDLE_ID}" \
  --arg system "${SYSTEM_ID}" --arg platform "${PLATFORM_ID}" --arg source "${SOURCE_SHA}" \
  --arg freebsd "${FREEBSD_SHA}" --arg worker_image "${IMAGE_SHA256}" \
  --arg worker_tools "${WORKER_TOOLS_SHA256}" \
  --arg package_train "${PACKAGE_TRAIN}" --arg channel "${CHANNEL}" \
  --arg packages "${PACKAGES_ID}" \
  --arg architecture "${ARCHITECTURE}" --arg package_arch "${PACKAGE_ARCH}" \
  --arg image_profile "${IMAGE_PROFILE}" --arg firmware "${FIRMWARE}" \
  --argjson capabilities "${IMAGE_CAPABILITIES}" \
  --arg channel_payload "${CHANNEL_PAYLOAD_SHA256}" --argjson size "${size}" \
  --argjson generation "${GENERATION}" \
  '{schema_version:(if $architecture == "arm64" then "freesense.installer/v1" else "freesense.iso/v2" end),fingerprint:$fingerprint,bundle_fingerprint:$bundle,sha256:$sha256,size:$size,file:$file,system:$system,generation:$generation,architecture:$architecture,package_arch:$package_arch,platform:$image_profile,firmware:($firmware|split(",")),capabilities:$capabilities,inputs:{platform:$platform,source:$source,freebsd:$freebsd,worker_image:$worker_image,worker_tools:$worker_tools,package_train:$package_train,packages:$packages,channel:$channel,channel_payload:$channel_payload}}' \
  >/tmp/complete.json
upload_immutable /tmp/complete.json "${RESULT}/complete.json"
phase iso-complete
