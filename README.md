# FreeSense OS build control plane

This repository pins FreeBSD 16 and defines the complete FreeSense build. GitHub
Actions only plans and publishes; all FreeBSD work runs in a fresh KVM guest on
the single 16-thread, 32-GiB Ryzen runner.

There are two independently invalidated package repositories:

- `system.yml` builds the pinned FreeBSD world/kernel plus the FreeSense system.
- `packages.yml` checks the optional package sources daily and rebuilds only that
  repository when its canonical input fingerprint changes.

Both publish immutable objects below `https://pkg.freesense.org/v1/artifacts/`.
The only mutable object is `v1/repos.manifest.json`: one RSA-signed document that
maps `devel` and `stable` to exact repository URLs. The appliance verifies that
signature before changing its pkg configuration.

`release.yml` verifies current development repositories, promotes verified
components after a seven-day soak, and assembles an ISO from an exact selected
system repository. `pin.yml` checks the official FreeBSD snapshot weekly and
updates a single reusable pull-request branch.

Retries reserve one generation per content fingerprint, reuse a valid
`complete.json`, keep existing identical objects, and write the completion marker
last. Failed jobs therefore neither invent a new package version nor upload the
same successful output again.

See [credential broker operations](docs/credential-broker.md) for the small set
of GitHub variables, environments, and secrets.
