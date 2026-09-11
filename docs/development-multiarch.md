# Development multiarch implementation status

The resumable unified pipeline is implemented behind rollout gates. It is not
the active Development publisher until its publication-disabled native canary
passes. Stable keeps its existing workflow, schemas, and checked release locks.

## Implemented locally

`development-multiarch.yml` freezes source revisions, probes the pinned native
ARM worker, selects its executor before generation reservation, and invokes
immutable System, Optional Packages and release subworkflows for both targets.
System has one core job and eight dependency/cost-aware package shards per target.
Optional Packages has eight shards per target. Each component has an authoritative
finalizer. Shards are divided into cumulative three-hour batches whose immutable
checkpoint identity binds architecture, pin, component, policy, shard count,
shard, batch, and previous qualified repository. Unknown timings receive a
conservative estimate instead of being treated as free work.
The Optional fingerprint remains independent of System-only source changes.

Each 14-day pin evaluates the same bounded ports-candidate window against both
signed FreeBSD catalogues. It selects the highest minimum accepted count, then
the highest combined count, then the newest commit. The final pin records
accepted and rejected counts and reasons for both architectures and refers to
separate content-addressed binary seeds.

The official signed seed is Tier 1. When the pin is unchanged, Tier 2 may import
packages from the previous signed architecture-qualified FreeSense repository.
Reuse requires exact ABI, OSVERSION, origin/version, options, recursive
dependency provenance, port and patch content, relevant Mk content, make
configuration, and architecture policy. Patched, kernel-sensitive,
changed, or transitively affected packages rebuild. The final Poudriere pass
remains authoritative.

`state/development-cycle.json` is the CAS-protected frozen-cycle resume point.
Reruns resume it before considering newer source heads. Immutable component
markers and cumulative checkpoints preserve completed AMD64 work and completed
ARM64 batches.

The native ARM probe needs KVM, sufficient memory and disk, QEMU, AAVMF and a
successful pinned-image boot. A failed probe selects the dedicated executor;
a later native build failure fails the run. Executor and worker identities are
included in component fingerprints and target-qualified checkpoints.

The v4 pin validator requires both native worker images/tool bundles, both jail
archives, verified signed catalogues, and compatible official binary seeds.
Seeds cover System and Optional requirements together and require official Rust
for both targets. The collector rejects unaudited make.conf assignments, and the
selector excludes overlays, custom patches and kernel-sensitive packages.
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

Each architecture independently publishes immutable downloads and then its
qualified release document, with the signed repository manifest last as its
commit point. CAS rejects rollback and conflicting same-generation bytes, so
AMD64 may advance while ARM64 remains at its previous qualified release.
`fsbuild multiarch prepare` still validates the eventual pair and creates the
signed `freesense.multiarch-release/v1` completion document. The downstream
`development-multiarch-publish.yml` runs only for the complete pair, verifies
both qualified publications, commits `releases/devel.multiarch.json`, and then
refreshes the legacy AMD64 aliases through monotonic CAS operations. Retention
protects qualified releases plus incomplete-cycle component and checkpoint
namespaces.

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
2. Confirm the deployed credential broker roles match the independently invoked
   coordinator, download-writer, and channel-writer jobs.
3. Verify native requirements collection, architecture exclusions, shard balance,
   empty shards, OPTIONS mismatch repair, repository signatures and Optional reuse
   on actual FreeBSD. Exercise the job timing gate against the 5.5-hour watchdog.
4. After explicit authorization, run a publication-disabled dual-architecture
   canary, boot all required generic images, measure upstream reuse and inspect
   failures using their exact job logs. Only then prepare the Development cutover.

Workspace root `plan.md` preserves the original plan and a fuller continuation
handoff. Neither local unit tests nor the presence of workflows establishes
native build readiness or successful publication.
