# FreeSense OS build control plane

This repository pins FreeBSD 16 and defines the complete FreeSense build. GitHub
Actions only plans and publishes; all FreeBSD work runs in a fresh KVM guest on
the single 16-thread, 32-GiB build runner.

There are two independently invalidated package repositories for rolling 1.1:

- `system.yml` builds the pinned FreeBSD world/kernel plus the complete
  FreeSense system closure from the pinned ports tree.
- `packages.yml` runs after the daily System check and rebuilds only when the
  optional-package commit/build recipe changes or the 14-day FreeBSD pin moves.
  A System-only source change keeps the existing optional repository. It seeds
  Poudriere from a compatible System repository without rebuilding System
  packages.

`system.yml` starts every day at 06:00 UTC. The shared KVM concurrency group
queues all actual builds, so the optional job cannot race a System build. A
successful new System also produces one development ISO for that System
identity.

Both publish immutable objects below `https://pkg.freesense.org/v1/artifacts/`.
The only mutable object is `v1/repos.manifest.json`: one RSA-signed document that
maps `devel` and `stable` to exact repository URLs. The appliance verifies that
signature before changing its pkg configuration.

`stable-1.0.yml` manually publishes an exact checked 1.0.x lock such as
`config/releases/1.0.4.json`. Each patch is immutable once published. The
`stable` pointer may move only to a higher 1.0 patch and always moves as one
verified System/Packages pair. Rolling workflows never promote 1.1 into stable.

`release.yml` verifies a complete development pair and assembles an ISO from an
exact selected System repository. During a FreeBSD pin rollover, a newly
published System remains pending until the new compatible Packages repository
arrives. `pin.yml` checks daily at 02:00 UTC, performs the expensive validation
only near the end of the active window, and advances the pin by exactly 14 days
through its single reusable pull-request branch.

Retries reserve one generation per content fingerprint, reuse a valid
`complete.json`, keep existing identical objects, and write the completion marker
last. Failed jobs therefore neither invent a new package version nor upload the
same successful output again.

`retention.yml` inventories R2 daily at 04:30 UTC and applies a reference-aware
retention plan only after the exact candidate set appears in two observations
at least 20 hours apart. Every Stable 1.0.x artifact remains permanent.
Development keeps the latest four completed System and ISO artifacts. Optional
Packages retention follows the active signed channel and the exact Packages
fingerprints recorded by those retained ISOs. Legacy ISOs without that binding
conservatively protect all Development Packages until they rotate out.
Incomplete and unreferenced data receives a seven-day grace period, and each
bucket keeps its newest broker smoke marker.

See [credential broker operations](docs/credential-broker.md) for the small set
of GitHub variables, environments, and secrets.

The dedicated host is provisioned once with KVM/QEMU, OVMF, cloud-image-utils,
xz, zstd, jq, GitHub CLI, Go, and the Actions runner. Build workflows never
mutate the persistent host with `apt`; missing prerequisites fail before a VM is
started. Every VM overlay and transient credential/script file is removed by an
always-run cleanup path.
