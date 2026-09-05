# GitHub-hosted build experiment

Branch: `experiment/github-hosted-builds`.

The first milestone assembles an amd64 Development ISO from the existing signed
System/Optional Packages pair on `ubuntu-24.04`, then boots that artifact in a
separate job. This does not build new System or Optional Packages repositories.
The experiment has no signing key, OIDC permission, production environment,
credential-broker call, or release publication. ISO artifacts expire in three
days; diagnostics expire in seven. They are experimental test images.

Pushes changing the experimental scripts or workflow on this exact branch start
the trial. A draft PR runs normal CI. Production scheduled workflows continue
using main and the dedicated runner. The experimental workflow cannot run its
assembly job on main.

The VM uses four CPUs, 10 GiB RAM and a sparse 64 GiB disk. The host records disk
usage and the guest records usage at phase boundaries. No preinstalled runner
software is removed. If actual storage is insufficient, the failed run supplies
evidence for the next stage split rather than assuming spare disk capacity.
The first run confirmed KVM and booted FreeBSD but reached its five-minute SSH
deadline during network initialization. The experiment now allows 15 minutes
for nested-VM startup and reports serial progress once per minute.
The follow-up remained in the same `vtnet` IPv6 router-advertisement path for
ten minutes. QEMU user networking is therefore IPv4-only for this isolated
builder; public input downloads and the host-forwarded SSH channel use IPv4.
The SSH approach was removed after the IPv4-only guest also failed to finish
service startup. The worker now uses the same direct `nuageinit` execution
pattern as production. A separate FAT output disk returns the ISO and marker to
the host after the guest shuts down, so assembly does not depend on SSH.
That path launched successfully and exposed a package-manager ABI override that
guessed OSVERSION 1600000 for signed 1600020 worker tools. The experimental
renderer now passes the manifest's already-validated OSVERSION explicitly to
`pkg add`; the one-revision userland compatibility check remains in force.
The signed channel's verified System OSVERSION is exported for all package
operations because dependency verification uses the same ABI contract.
`ALTABI` is unset only inside the worker-tool installer subshell because pkg
2.x emits a deprecation diagnostic for that legacy override; the parent worker
retains it for the FreeSense build descriptor.

`prepare-iso.py` verifies the live signed Development channel through the existing
channel verifier. It reuses the production source configuration, repository
verifier, assembly helpers, installer patch and ISO stage. The experimental
transport downloads public inputs, verifies their hashes and the signed package
catalogue, and copies results locally. The production private-key setup is
replaced with the checked public key. The exact existing channel signature is
preserved. Production scripts and credential policy are unchanged.

## Subsequent milestones

1. Complete and measure this ISO assembly and separate smoke test.
2. Split kernel-toolchain and System core jobs, with immutable intermediate
   artifacts bound to source pins, architecture and compiler recipe.
3. Build System ports in dependency-ordered batches. Shared dependencies are
   built once; independent batches may run concurrently.
4. Apply the same batching to Optional Packages, preserving independent
   invalidation and its compatible System seed.
5. Separate cloud/appliance assembly and smoke jobs. Verify the complete pair
   and release bundle before any production publication.
6. Introduce explicit GitHub-first routing with dedicated-runner fallback for
   measured capacity failures. Compilation and integrity failures remain errors.

Target each batch below three hours to leave margin under GitHub's six-hour job
limit. Intermediate artifacts must not be mistaken for published complete
repositories. A single oversized port may require the dedicated runner.
