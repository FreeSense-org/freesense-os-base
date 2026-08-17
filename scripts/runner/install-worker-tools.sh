# Install the exact worker tools selected by Pin FreeBSD. This function is shared
# by the pin smoke test and every real build worker.
install_worker_tools() (
  set -eu
  worker_tools=/tmp/freesense-worker-tools
  worker_tools_archive=${worker_tools}.tar
  cleanup_worker_tools() {
    rm -rf "${worker_tools}" "${worker_tools_archive}"
  }
  trap cleanup_worker_tools EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  phase tools-fetch
  cleanup_worker_tools
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if fetch -qo "${worker_tools_archive}" \
      "${PUBLIC_BASE_URL}/inputs/sha256/${WORKER_TOOLS_SHA256}"; then
      break
    fi
    sleep 2
  done
  test "$(sha256 -q "${worker_tools_archive}")" = "${WORKER_TOOLS_SHA256}"
  mkdir -p "${worker_tools}"
  tar -xpf "${worker_tools_archive}" -C "${worker_tools}"
  test -s "${worker_tools}/install-order"
  test -s "${worker_tools}/required-tools"
  test -s "${worker_tools}/required-osversion"

  required_osversion=$(cat "${worker_tools}/required-osversion")
  running_osversion=$(uname -U)
  case "${required_osversion}:${running_osversion}" in
    16[0-9][0-9][0-9][0-9][0-9]:16[0-9][0-9][0-9][0-9][0-9]) : ;;
    *) echo "invalid worker-tool OSVERSION binding" >&2; exit 1 ;;
  esac
  if [ "${required_osversion}" -eq "${running_osversion}" ]; then
    ignore_osversion=no
  elif [ "${required_osversion}" -eq $((running_osversion + 1)) ]; then
    ignore_osversion=yes
    echo "Allowing one-revision worker bootstrap: ${running_osversion} -> ${required_osversion}"
  else
    echo "worker-tool OSVERSION ${required_osversion} is incompatible with userland ${running_osversion}" >&2
    exit 1
  fi

  phase tools-install
  while IFS= read -r package; do
    case "${package}" in
      All/*.pkg) : ;;
      *) echo "invalid pinned worker package path: ${package}" >&2; exit 1 ;;
    esac
    case "${package#All/}" in
      ''|*/*|*[!A-Za-z0-9+,.@_~-]*)
        echo "invalid pinned worker package filename: ${package}" >&2
        exit 1
        ;;
    esac
    test -f "${worker_tools}/${package}" && test ! -L "${worker_tools}/${package}"
    package_metadata=$(pkg query -F "${worker_tools}/${package}" '%n|%v')
    package_name=${package_metadata%%|*}
    package_version=${package_metadata#*|}
    test -n "${package_name}" && test -n "${package_version}"
    installed_version=$(pkg query '%n|%v' | awk -F '|' -v name="${package_name}" \
      '$1 == name { print $2 }')
    if test -n "${installed_version}"; then
      test "${installed_version}" = "${package_version}" || {
        echo "pinned worker package conflicts with installed ${package_name}" >&2
        exit 1
      }
    else
      env ASSUME_ALWAYS_YES=no DEFAULT_ALWAYS_YES=no IGNORE_OSVERSION="${ignore_osversion}" \
        pkg add "${worker_tools}/${package}" </dev/null
    fi
  done <"${worker_tools}/install-order"
  dependency_issues=$(pkg check -d -n -q -a 2>&1) || {
    status=$?
    printf '%s\n' "${dependency_issues}" >&2
    exit "${status}"
  }
  test -z "${dependency_issues}" || {
    printf 'pinned worker package dependency check failed:\n%s\n' \
      "${dependency_issues}" >&2
    exit 1
  }
  while IFS= read -r tool; do
    case "${tool}" in
      ''|*[!A-Za-z0-9_.+-]*) echo "invalid required worker tool: ${tool}" >&2; exit 1 ;;
    esac
    command -v "${tool}" >/dev/null || {
      echo "worker tool installation did not provide ${tool}" >&2
      exit 1
    }
  done <"${worker_tools}/required-tools"
  phase tools-ready
)
