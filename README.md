# FreeSense OS build control plane

This repository pins FreeBSD 16 and defines the complete FreeSense build. GitHub
Actions only plans and publishes; all FreeBSD work runs in a fresh KVM guest on
the single 16-thread, 32-GiB build runner.

There are two independently invalidated package repositories:

- `system.yml` builds the pinned FreeBSD world/kernel plus the complete
  FreeSense system closure from the pinned ports tree.
- `packages.yml` checks the optional package sources daily and rebuilds only that
  repository when its canonical input fingerprint changes. It seeds Poudriere
  from the exact System repository, so System packages are not rebuilt there.

Both publish immutable objects below `https://pkg.freesense.org/v1/artifacts/`.
The only mutable object is `v1/repos.manifest.json`: one RSA-signed document that
maps `devel` and `stable` to exact repository URLs. The appliance verifies that
signature before changing its pkg configuration.

`release.yml` verifies a complete development pair, promotes it after a
seven-day soak, and assembles an ISO from an exact selected system repository.
A newly published System remains cleanly pending until its matching Packages
repository arrives. `pin.yml` checks the official FreeBSD snapshot weekly and
updates a single reusable pull-request branch.

Retries reserve one generation per content fingerprint, reuse a valid
`complete.json`, keep existing identical objects, and write the completion marker
last. Failed jobs therefore neither invent a new package version nor upload the
same successful output again.

See [credential broker operations](docs/credential-broker.md) for the small set
of GitHub variables, environments, and secrets.

The dedicated host is provisioned once with KVM/QEMU, OVMF, cloud-image-utils,
xz, zstd, jq, GitHub CLI, Go, and the Actions runner. Build workflows never
mutate the persistent host with `apt`; missing prerequisites fail before a VM is
started. Every VM overlay and transient credential/script file is removed by an
always-run cleanup path.
