# freesense-os-base agent rules

Release planning, build orchestration, signing and publication. The rules for
versions, branches, tags and publishing are in `docs/release-policy.md`
(R1–R9); cite the rule number when an action touches a release.

- `config/releases/<version>.json` is append-only (R1). Never edit, rename or
  delete a merged lock; withdraw it in `config/releases/withdrawn.json` and
  ship the next patch number (R2). CI (`scripts/check_release_locks.py`)
  rejects anything else.
- A Stable release is two dispatches of `stable.yml`: `stage=candidate`, an
  upgrade test, then `stage=seal` (R3). Dispatch either only when the user
  explicitly asks for that release and stage; the seal makes it live on
  installed devices and cannot be undone.
- Never move or delete `v*` tags (R5). `scripts/tag_release.py` creates
  missing ones and refuses to move existing ones.
- Changes under `broker/**` redeploy the live credential broker when merged.
- Run the suite in Linux before pushing: `python:3.12-alpine` with bash, jq,
  git and openssl, `python3 -m unittest discover -s tests -p 'test_*.py'`,
  `python3 scripts/validate-build-pipeline.py` and
  `python3 scripts/check_release_locks.py --base origin/main`.
- This repository only allows squash merges on GitHub; land a reviewed branch
  with a fast-forward push of `main` after CI is green.
