# Credential broker operations

The Cloudflare Worker at `https://r2-credentials.freesense.org/v1/credentials`
exchanges a protected-main GitHub OIDC identity for a short-lived R2 session.
It binds the organization ID, repository ID, environment, workflow, runner type,
and exact `main` ref. No workflow receives a permanent R2 key.

Repository variables:

- `R2_ACCOUNT_ID`
- `R2_BUILD_BUCKET` (`freesense-pkg`)
- `R2_ENDPOINT`
- `R2_CREDENTIAL_BROKER_URL`
- `R2_CREDENTIAL_BROKER_AUDIENCE`

Environments:

- `broker`: Worker deployment and one smoke object
- `build-coordinator`: immutable generation reservations
- `build`: immutable inputs/artifacts from the Ryzen reusable workflow
- `pin`: weekly input mirroring on Ryzen
- `channel-publisher`: the single signed channel object

The organization secret `FREESENSE_PKG_SIGNING_KEY` signs both pkg repository
catalogs and the channel document. Its public key is checked in at
`config/channel-signing-public.pem`; the appliance carries that public key and
the matching pkg fingerprint. Restrict the secret to `freesense-os-base` after
the repository is recreated.

The `broker` environment additionally holds:

- `CLOUDFLARE_BROKER_API_TOKEN`
- `R2_BROKER_PARENT_ACCESS_KEY_ID`
- `R2_BROKER_PARENT_SECRET_ACCESS_KEY`

The parent R2 credential must cover only bucket `freesense-pkg`. Broker sessions
never grant delete or multipart actions. Published files remain under prefix
`v1`; failed partial uploads are safe because `complete.json` is always last.
