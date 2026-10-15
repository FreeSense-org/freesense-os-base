# FreeSense release policy

This is the canonical rule set for versions, branches, tags and publication.
Workflows, CI checks, the workspace `AGENTS.md`, the `/release` skill and the
local release-guard hook all cite these rule numbers. When this file and an
older note disagree, this file wins; when this file and the code disagree,
fix one of them in the same pull request.

## Lines and version numbers

| Line | Product version (`/etc/version`) | Release ID (downloads, ISO name) | Built from |
|---|---|---|---|
| Stable | `1.1.N-RELEASE` | `1.1.N` | sealed lock `config/releases/1.1.N.json`, sources on `RELENG_1_1` |
| Stable candidate | `1.1.N-RELEASE` (the final version) | none: artifact repositories only | the same lock, `stable.yml` stage `candidate` |
| Development | `1.2.0-DEVELOPMENT` | `1.2.0-dev.YYYYMMDD.G` | `main` heads, nightly at 01:00 UTC |

- `YYYYMMDD` is the UTC date of the freesense source commit (the same instant
  the build stamps as `version.buildtime`); `G` is the build generation. The
  pair is unique and sorts by build. Releases published before this scheme
  keep their old `1.2.0-gG` / `1.1.0-gG` IDs.
- Package versions are not changed by this policy: System core is
  `1.1.N_G` on Stable and `1.2.0.a.YYYYMMDD.HHMM_G` on Development, so
  `pkg version -t` keeps ordering existing installations correctly.
- A Stable number is used once. It is never rebuilt with other inputs, and a
  release that is cancelled or broken is withdrawn, not reused (R2).

## Branches and tags

- `main` of `freesense`, `freesense-system-ports` and `freesense-packages` is
  Development (currently 1.2). Every feature lands there.
- `RELENG_1_1` in those three repositories is the Stable maintenance branch.
  It only takes fixes: cherry-pick from `main` with `git cherry-pick -x`
  through a pull request. A fix that exists only for 1.1 is made directly on
  the branch, and its pull request says why.
- `stable-refresh.yml` (07:30 UTC) reads the `RELENG_1_1` heads and opens the
  pull request for lock `1.1.N+1` when something changed.
- Every sealed Stable release is tagged `v1.1.N` (annotated) at the lock's
  commits in all three source repositories, and in `freesense-os-base` at the
  commit that added the lock. Tags are never moved or deleted.
- Development builds are not tagged: the release ID and the signed channel
  manifest record their exact sources.
- When 1.2 becomes Stable: create `RELENG_1_2` from `main`, set
  `stable_train` to `1.2` and `development_train` to `1.3`; `RELENG_1_1` is
  then frozen.

## Rules

- **R1 Locked sources.** Stable is built only from a sealed lock whose three
  source commits are reachable from `RELENG_<train>`. Merged locks are
  append-only: never edit or delete `config/releases/<version>.json`.
- **R2 No reuse.** A version number is used once. A cancelled or broken
  release is recorded in `config/releases/withdrawn.json` with a reason and
  the release that supersedes it; the fix ships as the next number.
- **R3 Candidate, test, seal.** A Stable release is first built as a
  candidate (`stable.yml`, `stage: candidate`), upgrade-tested on a VM from
  the current Stable release, and only then sealed (`stage: seal`). The seal
  job runs in the `stable-publisher` environment, which needs a maintainer's
  approval. The seal makes the release live on installed devices.
- **R4 Soak time.** Two Stable seals are at least `stable_soak_hours`
  (config/build-policy.json) apart. A security or broken-update fix may
  override this with `"soak_override": "<reason>"` in its lock.
- **R5 Tags.** Every sealed Stable release is tagged `v<release>` in the four
  repositories. Tags are immutable.
- **R6 Development.** Development publishes from `main` on the nightly
  schedule. A manual Development dispatch needs an explicit request.
- **R7 Hands off publication data.** Channel manifests, R2 objects and
  download documents change only through workflows. Manual changes need an
  explicit, exact, authorized target.
- **R8 Batch Stable patches.** A Stable patch is cut for a security fix, a
  regression or broken-update fix, or a reviewed batch of changes, not per
  package. A patch always rebuilds System (its version is part of the System
  fingerprint), so an Optional-Packages-only change waits for the next patch
  unless it is a security fix.
- **R9 Release notes.** The notes of a release say which releases it
  supersedes or withdraws, and any action a user must take before updating.

## Procedures

### Stable patch (hotfix)

1. Fix on `main` (pull request, CI green).
2. Cherry-pick to `RELENG_1_1` with `-x` (pull request, CI green).
3. Merge the `stable-refresh` lock pull request, or write the lock by hand
   from the `RELENG_1_1` heads (copy the previous lock, change `release`,
   `product_version`, the three source SHAs and `notes`).
4. Dispatch `stable.yml` with `release=1.1.N`, `stage=candidate`.
5. Upgrade-test the previous Stable release to the candidate on the release
   test VM (the run summary prints the candidate repository URLs).
6. Dispatch `stable.yml` with `release=1.1.N`, `stage=seal` and approve the
   `stable-publisher` deployment. The seal checks R1, R2 and R4, signs the
   Stable channel, tags `v1.1.N` and builds and publishes the images.

### Withdraw a release

1. Add an entry to `config/releases/withdrawn.json` (release, date, reason,
   `superseded_by`).
2. Ship the fix as the next number; its notes name the withdrawn release (R9).
3. Leftover downloads are removed only with an explicit, exact target (R7).

## Enforcement

| Layer | Where | What it stops |
|---|---|---|
| CI | `scripts/check_release_locks.py` in `ci.yml` | edited or deleted locks, gaps in numbering, invalid `withdrawn.json` |
| Workflow | `scripts/stable_gate.py` in `stable.yml` | building from commits outside `RELENG_<train>`, withdrawn numbers, seals inside the soak time, sealing without a finished candidate |
| GitHub | `stable-publisher` environment, tag rulesets, `RELENG_*` branch protection | unapproved seals, moved or deleted tags, unreviewed maintenance commits |
| Agents | workspace `AGENTS.md`, `/release` skill, release-guard hook | an AI dispatching, tagging, pushing or touching publication data without asking |
