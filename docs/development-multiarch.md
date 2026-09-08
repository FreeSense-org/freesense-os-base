# Development multiarch implementation status

The unified pipeline is under implementation. It is not the active Development
publisher, and its full execution has not been verified. Stable keeps its
existing workflow and checked release locks.

## Implemented locally

`development-multiarch.yml` freezes source revisions, probes the pinned native
ARM worker, selects its executor before generation reservation, and invokes
immutable System, Optional Packages and release subworkflows for both targets.
System has one core job and four package shards per target. Optional Packages
has four shards per target. Each component has an authoritative finalizer.
The Optional fingerprint remains independent of System-only source changes.

The native ARM probe needs KVM, sufficient memory and disk, QEMU, AAVMF and a
successful pinned-image boot. A failed probe selects the dedicated executor;
a later native build failure fails the run. Executor and worker identities are
included in component fingerprints and target-qualified checkpoints.

The v4 pin validator requires both native worker images/tool bundles, both jail
archives, verified signed catalogues, and compatible official binary seeds.
Seeds cover System and Optional requirements together and require official Rust
for both targets. The collector rejects unaudited make.conf assignments, and the
selector excludes overlays, custom patches, kernel-sensitive packages and PHP85.
The v4 pin orchestrator in `pin.yml` resolves common source revisions, runs `pin-target.yml` in parallel on native amd64 and native ARM64 runners, downloads target reports, verifies identical revision and builddate, checks mirrored R2 inputs, assembles the pin via `assemble_multiarch_pin.py`, updates `config/freebsd-16.json` only when both targets pass, and opens or updates the automated pin pull request without force-pushing shared history.

Delta finalizers discard conflicting package variants from their disposable
seed, remembering discarded identities across later shards. Poudriere then runs
the complete root list. The final repository merge remains strict; this repair
path still requires a native injected-incompatibility canary.

The final canary verifier checks both release documents against the reserved
pair generation, every required image, component/pin bindings and upstream
provenance. It independently verifies final catalogue signatures with the frozen
key, checks the combined dependency closure, and checks reused package bytes
against both provenance SHA-256 and signed catalogue checksums. It also checks
recorded job durations and standard-runner concurrency. Pi images retain their
structural-only verification label. Reused images can retain their original
build generation while the release documents identify the shared pair generation.

`fsbuild multiarch prepare` validates the trusted same-run canary report against
both signed repository manifests and exact release-document bytes, then writes
a local RSA-signed `freesense.multiarch-release/v1` completion document. It has
no store access and cannot change channel pointers. The downstream publication
workflow `development-multiarch-publish.yml` executes once the multiarch run
succeeds on `main`: it verifies local documents, publishes immutable download
artifacts, publishes architecture-qualified manifests (`repos.amd64.manifest.json`,
`repos.aarch64.manifest.json`, `devel.amd64.json`, `devel.arm64.json`), verifies
their public visibility via `verify_multiarch_publication.py`, commits
`releases/devel.multiarch.json` atomically via `fsbuild multiarch commit` with
compare-and-swap semantics, and refreshes legacy amd64 aliases (`repos.manifest.json`,
`releases/devel.json`). Retention planning in `r2_retention.py` binds qualified release
documents and referenced image artifact families (`installer`, `cloud`, `appliance`)
to the authoritative completion document.

## Rollout boundaries

- The new daily 06:00 UTC schedule is gated by
  `MULTIARCH_DEVELOPMENT_ENABLED == 'true'`. Its current code is still a
  publication-disabled canary. Do not enable the variable as a production cutover.
- The existing System schedule and legacy workflow chains remain active in code.
  The cutover must disable those Development entry points to prevent duplicate
  work. Stable must remain unchanged.
- Manual dispatch starts a full canary with an optional forced dedicated ARM
  executor. It requires explicit build authorization; none has been dispatched
  during this implementation.
- The broker adds an input-reader role for the probe and permits protected
  multiarch component calls. Its matching broker change must be deployed before
  a canary can acquire credentials. Nested builders receive no channel-write role.
- Pull requests run local-equivalent Python, Go, broker, Actionlint and shell
  checks. A merge to main runs CI; it does not by itself activate the new schedule.
  Existing scheduled planning will consume changed build recipes as applicable.

## Work required before activation

1. Native candidate generation: The v4 pin workflow is fully wired into `pin.yml`.
   Running it will produce the first complete dual-architecture v4 pin with real
   verified inputs mirrored to R2.
2. Signed publication and retention: Architecture-qualified manifests, atomic
   commit via `fsbuild multiarch commit`, and retention planning binding release
   documents to the completion hash are implemented and verified with exhaustive
   failure-path test suites.
3. Verify native requirements collection, architecture exclusions, shard balance,
   empty shards, OPTIONS mismatch repair, repository signatures and Optional reuse
   on actual FreeBSD. Exercise the job timing gate against the 5.5-hour watchdog.
4. After explicit authorization, run a publication-disabled dual-architecture
   canary, boot all required generic images, measure upstream reuse and inspect
   failures using their exact job logs. Only then prepare the Development cutover.

Workspace root `plan.md` preserves the original plan and a fuller continuation
handoff. Neither local unit tests nor the presence of workflows establishes
native build readiness or successful publication.
