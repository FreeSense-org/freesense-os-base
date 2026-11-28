# Signing the 2.0 package repository

Contract between `.github/workflows/v2-build.yml` and the build driver.

- The `repo` stage writes an unsigned repository to `<work>/out/repo`. Its stage key must not depend on signature
  files, so that the later `iso` stage keeps the signed repository instead of building it again.
- `build/sign-repo REPOSITORY KEYFILE PUBLICKEY` signs REPOSITORY in place, in a throwaway VM from the pinned image of
  `build/lock.json` on a GitHub-hosted `ubuntu-24.04` runner with `/dev/kvm`, QEMU, OVMF, xorriso and zstd. It needs only
  the `build/` folder. It verifies with PUBLICKEY: `pkg update` exits 0 even when a signature is wrong (pkg 2.9.3), so
  the check is that every package is visible with that key and none with another.
- The `iso` stage runs with `FREESENSE_REQUIRE_SIGNED=1` and refuses an unsigned repository.
- The public key is `keys/freesense-2.0-pkg.pub`, kept byte for byte (`.gitattributes`). The installer and the installed
  system trust that file (`signature_type: "pubkey"`); an unsigned or differently signed repository is refused.
- The private key exists only as the secret `FREESENSE_2_PKG_SIGNING_KEY_B64` of the environment `signing-2.0` in
  freesense-os-base, which accepts the branch `2.0` only. Only the `sign` job names it;
  `scripts/ci/workflow_lint.py` fails on any other use.
- It is a test key. Nothing signed with it is published.
