# FreeSense 2.0 branch

This is one repository of the experimental FreeSense 2.0 line. It builds and checks; it publishes nothing.

- The checks and the build need all repositories laid out as one workspace. Put checkouts of `freesense`,
  `freesense-os-base`, `freesense-system-ports` and `freesense-packages` (branch `2.0`) in one folder and run
  `python3 freesense-os-base/scripts/assemble-workspace.py --repos . --dest ../ws`.
- The workspace guidance is `workspace/AGENTS.md` on the `2.0` branch of `freesense-os-base`
  (in the assembled workspace: `AGENTS.md` at the top).
- Never open a pull request from `2.0` to `main` and never merge one line into the other. A fix both need is
  cherry-picked.
- First line of every commit: `Area: what changed`.
- No workflow here may publish, deploy or use a credential; `scripts/ci/workflow_lint.py` of `freesense-os-base`
  enforces it.
