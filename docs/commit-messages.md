# Commit messages

Every commit that lands on `main` in `freesense`, `freesense-system-ports` and
`freesense-packages` becomes a line in the "What is new" list on System > Update
of a Development system. Pull requests are rebase-merged, so the first line of
each commit is that line. Write it for the person reading the update, not for the
reviewer.

## The rule

```
Area: what changed
```

- **Area**: the part of the product, capitalised, one to four words. Use the
  name people know: `WebUI`, `Firewall`, `Network`, `VPN`, `Security`,
  `Dashboard`, `REST API`, `Fleet`, `Docs`, or a package (`Telegraf`,
  `CrowdSec`, `ThreatShield`).
- **What changed**: a sentence that says what is different now, at least 12
  characters, no full stop. Good: `WebUI: the page width setting is per user and
  lives in Account`. Poor: `WebUI: fixes`.
- The whole first line stays under 120 characters. Put the detail in the body.
- No drafts: `WIP`, `fixup!`, `squash!`, `tmp` are rejected. Merge, revert and
  dependency-bot commits are exempt.

## Optional trailers

Put them at the end of the commit message, one per line.

| Trailer | Effect |
| --- | --- |
| `Release-Note: <one line>` | Replaces the subject in the notes (12 to 180 characters). Use it when the subject is for reviewers and the note should read better. |
| `Release-Note: skip` | The commit is not listed (internal work, refactors, tests). |
| `Release-Type: security\|fix\|feature\|ui\|package\|documentation\|build\|other` | Sets the type instead of the keyword guess. |

Without `Release-Type`, the publisher guesses from words in the title
(`security`, `fix`, `webui`, ...), as it always has.

## Where it is enforced

The `commit-messages` check runs on every pull request in the three product
repositories (the reusable workflow lives in `FreeSense-org/.github`; the rules
are `scripts/commit_messages.py` in this repository, so the check and the
publisher read messages the same way). It checks the commits of the pull request
only; nothing already merged is rewritten. Run it locally with:

```
python scripts/commit_messages.py --range origin/main..HEAD
```

Fix a message with `git commit --amend`, or an interactive rebase for an older
commit in your branch, and push again.

## How the notes are built

`scripts/publish_download.py` reads the commits between two published builds
(`github_compare`), applies the trailers, drops build-only items and keeps the
first 50. `scripts/publish_notes.py` does this on every Development
publication and keeps the last 30 builds in `releases/devel.notes.json`. Stable
releases add hand-written lock notes on top (R9).
