# Bounded R2 Retention Batches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make guarded R2 cleanup converge under continuous daily builds while preserving exact two-observation confirmation and the existing per-bucket safety limits.

**Architecture:** Keep the existing reference-aware eligibility calculation, then select a deterministic oldest-first actionable batch independently for each bucket. Persist candidate-group identities so later runs reconstruct and confirm that exact batch before admitting newer candidates, while exposing complete eligible and deferred backlog totals in the report and workflow summary.

**Tech Stack:** Python 3 standard library, `unittest`, GitHub Actions YAML, Markdown.

## Global Constraints

- Stable artifacts remain permanent.
- Candidate groups remain atomic during planning.
- Each bucket is limited to 5,000 objects and 50 GiB per cleanup run.
- Exact candidate batches require two matching observations at least 20 hours apart.
- No manual build or retention workflow is triggered by implementation.

---

### Task 1: Reproduce bounded-batch behavior

**Files:**
- Modify: `tests/test_r2_retention.py:309`

**Interfaces:**
- Consumes: `plan_retention(...)` and `candidate_digest(report)`.
- Produces: Regression coverage for stable selection under candidate growth, backlog totals, and oversized groups.

- [ ] **Step 1: Add a failing stability and totals test**

Create two old 30-GiB unreferenced input candidates, run `plan_retention`, and assert that only the oldest fits the 50-GiB build-bucket batch. Assert literal selected, eligible, and deferred object/byte totals.

- [ ] **Step 2: Add a failing steady-state confirmation test**

Observe an under-cap batch, persist its confirmation state, add a newer
candidate, and assert that planning retains only the observed group and that a
second observation becomes ready after 24 hours.

- [ ] **Step 3: Add a failing oversized-group test**

Create one expired candidate group larger than 50 GiB and assert that planning exits with `retention candidate group exceeds the per-run safety cap`.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_r2_retention.RetentionPlanTests.test_oldest_bounded_batch_reports_deferred_candidates tests.test_r2_retention.RetentionPlanTests.test_observed_batch_is_pinned_when_new_candidates_arrive tests.test_r2_retention.RetentionPlanTests.test_candidate_group_larger_than_safety_cap_is_rejected -v
```

Expected: failures because the full candidate set is still returned and oversized groups are not rejected during planning.

### Task 2: Select bounded per-bucket batches

**Files:**
- Modify: `scripts/r2_retention.py:394`
- Modify: `scripts/r2_retention.py:736`
- Modify: `scripts/r2_retention.py:844`

**Interfaces:**
- Produces: `select_deletion_batch(candidates, max_objects, max_bytes) -> tuple[list[dict], list[dict]]`.
- Produces: `candidate_totals(candidates) -> dict[str, int]`.
- Produces: Candidate-group digests stored in `freesense.r2-retention-state/v1`.
- Preserves: `report["candidates"]` as the exact actionable deletion set.
- Adds: `report["eligible_totals"]` and `report["deferred_totals"]`.

- [ ] **Step 1: Add shared safety constants**

Define `MAX_DELETE_OBJECTS = 5000` and `MAX_DELETE_BYTES = 50 * 1024**3`, and use them from both planning and deletion validation.

- [ ] **Step 2: Implement oldest-first selection**

Sort candidates by parsed `last_modified`, bucket, and prefix. Track object and byte use independently per bucket. Stop selecting newer candidates for a bucket when its next complete group would exceed either cap. Reject any single group that exceeds a cap.

- [ ] **Step 3: Add selected, eligible, and deferred totals**

Keep `totals` scoped to actionable `candidates`, and calculate `eligible_totals` and `deferred_totals` from the complete eligible and deferred sets.

- [ ] **Step 4: Pin the observed batch**

Store candidate-group digests in retention state. When all stored groups remain
eligible, reconstruct that exact batch and defer every other candidate. Select
a fresh oldest-first batch if an observed group is no longer eligible.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_r2_retention.RetentionPlanTests.test_oldest_bounded_batch_reports_deferred_candidates tests.test_r2_retention.RetentionPlanTests.test_observed_batch_is_pinned_when_new_candidates_arrive tests.test_r2_retention.RetentionPlanTests.test_candidate_group_larger_than_safety_cap_is_rejected -v
```

Expected: both tests pass.

### Task 3: Expose operational backlog state

**Files:**
- Modify: `scripts/r2_retention.py:929`
- Modify: `README.md:50`
- Modify: `scripts/validate-build-pipeline.py:74`

**Interfaces:**
- Consumes: `totals`, `eligible_totals`, and `deferred_totals` from the retention report.
- Produces: Workflow summary lines showing actionable and deferred storage.

- [ ] **Step 1: Extend the Markdown summary**

Report actionable candidate prefixes/objects/storage and deferred prefixes/objects/storage using the new literal fields.
Show the 5,000-object/50-GiB cap and actionable totals independently for the
build and downloads buckets.

- [ ] **Step 2: Document bounded oldest-first batches**

Update the README retention paragraph to state that exact confirmation applies to a bounded oldest-first per-bucket batch and remaining eligible objects are deferred.

- [ ] **Step 3: Preserve static pipeline validation**

Add the selector name and deferred-total field to the retention source invariants checked by `scripts/validate-build-pipeline.py`.

- [ ] **Step 4: Run focused and static checks**

Run:

```powershell
python -m unittest tests.test_r2_retention -v
python scripts/validate-build-pipeline.py
```

Expected: all retention tests pass and pipeline validation exits successfully.

### Task 4: Verify the repository and inspect the patch

**Files:**
- Verify: all changed files.

**Interfaces:**
- Produces: Evidence that the implementation is ready for review.

- [ ] **Step 1: Run all Python tests**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 2: Run Go tests**

Run:

```powershell
go test ./...
```

Expected: all packages pass.

- [ ] **Step 3: Run whitespace and diff checks**

Run:

```powershell
git diff --check
git status --short
git diff --stat main...HEAD
git diff main...HEAD
```

Expected: no whitespace errors; only the focused R2 design, plan, implementation, tests, validation, and README changes are present.

- [ ] **Step 4: Commit the verified implementation**

Run:

```powershell
git add README.md scripts/r2_retention.py scripts/validate-build-pipeline.py tests/test_r2_retention.py docs/superpowers/plans/2026-08-01-r2-retention-batches.md
git commit -m "Fix guarded R2 retention cleanup"
```
