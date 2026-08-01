# Bounded R2 Retention Batches Design

## Problem

The retention workflow currently confirms the SHA-256 digest of the complete
R2 deletion candidate set. Development artifacts become eligible every day,
so that set changes before the next scheduled observation and confirmation
returns to observation one. Live workflow history from July 27 through August
1 shows this reset on every run.

The August 1 candidate set also contains 3,611 objects totaling 94,500,505,792
bytes. Even if that set received two matching observations, the deletion
command would reject the build-bucket candidates because its safety boundary
is 5,000 objects and 50 GiB per run.

## Goals

- Preserve Stable artifacts and all existing reference-aware protections.
- Preserve exact-set confirmation at least 20 hours apart.
- Ensure the confirmed deletion set cannot exceed the existing per-bucket
  deletion boundary.
- Let an old backlog converge even while newer candidates are added daily.
- Make deferred backlog visible in workflow output and summaries.

## Design

Retention planning will continue to compute the complete eligible candidate
universe. Before producing the actionable report, it will select one bounded
batch independently for each bucket.

Candidates will be ordered by `last_modified`, then bucket and prefix, from
oldest to newest. The selector will add complete candidate groups until the
next group would exceed either 5,000 objects or 50 GiB for that bucket. It will
stop at that boundary rather than skipping ahead. Keeping whole groups avoids
deliberately planning a partial immutable artifact deletion. A single group
that exceeds either limit will fail planning with a clear error because it
cannot be deleted safely under the configured boundary.

The existing `candidates` field will contain only the selected actionable
batch. The first observation records a digest for every selected candidate
group in retention state. On the next run, planning reconstructs that same
batch from the still-eligible groups before considering newer candidates. If
every observed group remains eligible, later candidates are deferred even when
the observed batch is below the size cap. If any observed group disappears or
changes, planning selects a fresh oldest-first batch and confirmation resets.
The report will retain totals for the selected batch and add eligible and
deferred totals so operators can see the complete backlog.

The confirmation state and delete commands remain exact-set based. On the
first observation of a selected batch they record observation one. A matching
observation at least 20 hours later authorizes deletion. After deletion, the
next daily plan selects the next oldest batch and begins its own confirmation
cycle.

## Failure Handling

- Stable, state, manifest, and out-of-boundary keys remain rejected by the
  existing deletion checks.
- A candidate group larger than the safety boundary aborts planning rather
  than being partially selected or silently deferred forever.
- The deletion layer retains the same independent 5,000-object and 50-GiB
  validation as defense in depth.
- Failed or incomplete deletions do not record successful application because
  the observation-write step runs only after the preceding workflow steps
  succeed.

## Tests

Tests will reproduce the production failure by creating an oldest backlog
larger than one safe batch and verifying its deferred totals. A separate
steady-state regression will observe an under-cap batch, add a newer candidate,
and assert that persisted candidate-group identities keep the selected digest
unchanged and allow confirmation. Additional tests will verify per-bucket
limits, oldest-first selection, and the oversized-single-group failure.
Existing retention tests will continue to verify Stable protection, reference
closure, confirmation timing, and deletion boundaries.

## Operational Outcome

The first scheduled run after deployment will observe the new bounded batch.
The next matching daily run, at least 20 hours later, can delete it. Subsequent
batches drain on the same guarded cadence. This change does not trigger a
manual System, Optional Packages, ISO, or retention workflow.
