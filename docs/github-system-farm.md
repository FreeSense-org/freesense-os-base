# GitHub-hosted System build farm

AMD64 Development System builds fan out across the standard GitHub-hosted
runner concurrency available to the public repository. Stable and ARM64 keep
the original single-guest dedicated path.

## Execution graph

The planner starts one `core` worker, one `bootstrap` worker for `lang/rust`,
and 18 general package shards. Those 20 jobs run concurrently. The two
cloud-init roots that pulled Rust into separate five-hour closures are excluded
from the general shards. A nested `dependent` worker starts after the bootstrap
checkpoint exists, seeds Poudriere from it, and builds both cloud-init roots
without recompiling Rust. The finalizer starts only after every package
checkpoint has completed successfully.

```text
plan -> core -------------------------------\
     -> general shards 0..17 ----------------> finalize -> publish channel
     -> Rust bootstrap -> cloud-init shard --/
```

The core job builds and validates world/kernel packages. Package shards create
the same pinned jail and ports tree, expand the large System metaports into
smaller roots, and select a deterministic modulo partition. Rust has its own
bootstrap checkpoint because the first farm run showed that compiling it inside
both cloud-init closures exhausted the per-job watchdog. Each shard lets
Poudriere complete normally; no live VM or partially written repository is
snapshotted.

## Immutable checkpoints

Checkpoints live below the incomplete final System artifact:

```text
artifacts/system/<fingerprint>/checkpoints/farm-19/core/
artifacts/system/<fingerprint>/checkpoints/farm-19/bootstrap/
artifacts/system/<fingerprint>/checkpoints/farm-19/shards/<0..18>/
```

Each contains package payloads plus a `freesense.system-checkpoint/v1` marker.
The marker binds the package hashes and metadata to the System fingerprint,
generation, target, source commits, FreeBSD pin, package train, and trusted
signing public key. Package payloads are written first and `complete.json` is
written last.

Core, bootstrap, dependent, and general shard jobs do not receive the
package-repository private key. The finalizer receives it only after all
unsigned checkpoints exist. It downloads
and validates every marker and payload, permits duplicated dependencies only
when package identity and bytes are identical, and seeds a fresh Poudriere
repository with the merged packages.

## Authoritative repair and publication

The finalizer runs the complete unsharded System root list. Poudriere checks the
seed against the exact jail, ports tree, options, and dependency metadata,
rebuilds anything missing or invalid, and removes anything outside the desired
closure. The finalizer then combines the result with the core packages, checks
that every declared package dependency is present, signs the repository, and
writes the normal System `complete.json` last.

ISO and Optional Packages workflows consume only that final signed repository.
They never consume checkpoints directly.

## Retry behavior

Before starting a core, bootstrap, dependent, or general shard VM, the reusable
runner checks for that part's completion marker. A retry skips completed parts
and runs only missing ones.
The finalizer always performs full marker and payload validation. A mismatched
or corrupt immutable checkpoint therefore fails closed instead of being
silently replaced.

The farm version includes its shard count in the checkpoint path. Changing the
count cannot accidentally reuse checkpoints created by a different layout.
