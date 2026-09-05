# Credential broker operations

The Cloudflare Worker at `https://r2-credentials.freesense.org/v1/credentials`
exchanges a protected-main GitHub OIDC identity for a short-lived R2 session.
It binds the organization ID, repository ID, environment, workflow, runner type,
and exact `main` ref. No workflow receives a permanent R2 key.

Repository variables:

- `R2_ACCOUNT_ID`
- `R2_BUILD_BUCKET` (`freesense-pkg`)
- `R2_DOWNLOAD_BUCKET` (`freesense-downloads`)
- `R2_ENDPOINT`
- `R2_CREDENTIAL_BROKER_URL`
- `R2_CREDENTIAL_BROKER_AUDIENCE`

Environments:

- `broker`: Worker deployment and one smoke object
- `build-coordinator`: immutable generation reservations
- `build`: immutable inputs/artifacts from the reusable build-runner workflow;
  protected-main jobs may use either the GitHub-hosted Development route or the
  dedicated fallback
- `pin`: weekly input mirroring on the build runner
- `channel-publisher`: signed repository/release metadata and immutable public ISOs
- `retention`: daily inventories, exact-key retention, and one small observation record

The organization secret `FREESENSE_PKG_SIGNING_KEY` signs both pkg repository
catalogs and the channel document. Its public key is checked in at
`config/channel-signing-public.pem`; the appliance carries that public key and
the matching pkg fingerprint. Restrict the secret to `freesense-os-base` after
the repository is recreated.

The `broker` environment additionally holds:

- `CLOUDFLARE_BROKER_API_TOKEN`
- `R2_BROKER_PARENT_ACCESS_KEY_ID`
- `R2_BROKER_PARENT_SECRET_ACCESS_KEY`

The parent R2 credential must cover only buckets `freesense-pkg` and
`freesense-downloads`. Build-artifact
sessions may list objects so a stage can copy an immutable dependency repository;
other sessions cannot list. The downloads publishing session can write only
under `v1/releases/`. Retention uses separate read, delete-only, and
state-write-only sessions; no session can both write and delete. No session
grants multipart actions.
Published files remain under prefix `v1`; failed partial uploads are safe because
`complete.json` is always last.

The daily retention workflow runs at 04:30 UTC. Its reader sessions can list
and read only the build/input, download-release, and broker-smoke prefixes
needed to construct a reference-aware plan. Stable 1.0.x artifacts are kept
forever. Development retains four completed System and ISO artifacts. Optional
Packages follow the active signed channel and the exact Packages fingerprints
recorded by retained ISOs, together with their transitive inputs. A retained
legacy ISO without that fingerprint protects every Development Packages
repository until it rotates out. Each bucket keeps its newest broker smoke
marker. Incomplete artifacts and unreferenced inputs must be seven days old.

No candidate is deleted on first observation. The exact candidate set must be
seen again at least 20 hours later. Delete-only credentials are then issued
separately for the build and downloads buckets, with Stable, state, and control
paths rejected again by the client and a 5,000-object/50-GiB per-run cap. The
workflow stores only its small confirmation record at
`v1/state/retention.json`.
