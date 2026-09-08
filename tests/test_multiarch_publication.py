import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_multiarch_publication as publication


def make_bundle(generation=1, pair_hash=None):
    pair_fingerprint = pair_hash or hashlib.sha256(f"pair-{generation}".encode()).hexdigest()
    repositories = {
        "amd64": json.dumps({"schema_version": "freesense.repositories/v3", "channel": "devel",
                             "generation": generation, "arch": "amd64"}).encode(),
        "arm64": json.dumps({"schema_version": "freesense.repositories/v3", "channel": "devel",
                             "generation": generation, "arch": "aarch64"}).encode(),
    }
    releases = {
        "amd64": json.dumps({"schema_version": "freesense.download/v4", "channel": "devel",
                             "generation": generation, "architecture": "amd64"}).encode(),
        "arm64": json.dumps({"schema_version": "freesense.download/v4", "channel": "devel",
                             "generation": generation, "architecture": "arm64"}).encode(),
    }
    completion = {
        "schema_version": "freesense.multiarch-release/v1",
        "channel": "devel",
        "generation": generation,
        "pair_fingerprint": pair_fingerprint,
        "freebsd_pin": hashlib.sha256(f"pin-{generation}".encode()).hexdigest(),
        "architectures": {
            arch: {
                "system_fingerprint": hashlib.sha256(f"sys-{arch}-{generation}".encode()).hexdigest(),
                "packages_fingerprint": hashlib.sha256(f"pkg-{arch}-{generation}".encode()).hexdigest(),
                "repository_document_sha256": hashlib.sha256(repositories[arch]).hexdigest(),
                "release_document_sha256": hashlib.sha256(releases[arch]).hexdigest(),
                "artifact_document_sha256": {
                    "installer": hashlib.sha256(f"inst-{arch}".encode()).hexdigest(),
                },
            }
            for arch in publication.ARCHES
        },
    }
    return completion, repositories, releases


def wrap_envelope(payload_dict: dict) -> bytes:
    raw = json.dumps(payload_dict).encode()
    envelope = {
        "schema_version": "freesense.multiarch-release/v1",
        "payload": base64.b64encode(raw).decode(),
        "signature": base64.b64encode(b"simulated-signature").decode(),
    }
    return json.dumps(envelope).encode() + b"\n"


def unwrap_envelope(envelope_bytes: bytes) -> dict:
    envelope = json.loads(envelope_bytes.decode())
    if envelope.get("schema_version") != "freesense.multiarch-release/v1":
        raise ValueError("unsupported multiarch envelope")
    raw = base64.b64decode(envelope["payload"])
    return json.loads(raw.decode())


class MockPublicationStore:
    """Simulate R2 storage with compare-and-swap semantics matching internal/control/multiarch.go."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._etag_counter = 0

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> str:
        self._etag_counter += 1
        etag = f"etag-{self._etag_counter}"
        self.objects[key] = data
        self.etags[key] = etag
        return etag

    def compare_and_swap(self, key: str, expected_etag: str, new_data: bytes) -> tuple[str, bool]:
        current_etag = self.etags.get(key)
        if current_etag != expected_etag:
            return current_etag or "", False
        self._etag_counter += 1
        new_etag = f"etag-{self._etag_counter}"
        self.objects[key] = new_data
        self.etags[key] = new_etag
        return new_etag, True

    def commit_multiarch(self, candidate_envelope: bytes) -> tuple[bool, str]:
        """Atomic commit point matching Go CommitMultiarch behavior.
        Returns (updated, message)."""
        candidate = unwrap_envelope(candidate_envelope)
        key = "v1/releases/devel.multiarch.json"
        current_bytes = self.get(key)
        if current_bytes is None:
            self.put(key, candidate_envelope)
            return True, "created"

        existing = unwrap_envelope(current_bytes)
        candidate_gen = candidate.get("generation", 0)
        existing_gen = existing.get("generation", 0)

        if candidate_gen < existing_gen:
            raise ValueError("multiarch completion cannot move backwards")

        if candidate_gen == existing_gen:
            if candidate.get("pair_fingerprint") != existing.get("pair_fingerprint") or candidate_envelope != current_bytes:
                raise ValueError("multiarch generation cannot be rewritten")
            return False, "idempotent"

        expected_etag = self.etags.get(key, "")
        _, swapped = self.compare_and_swap(key, expected_etag, candidate_envelope)
        if not swapped:
            raise ValueError("multiarch completion changed repeatedly; refusing a lost update")
        return True, "advanced"


class MultiarchPublisher:
    """Simulates the GitHub Actions steps in development-multiarch-publish.yml."""

    def __init__(self, store: MockPublicationStore):
        self.store = store

    def run(self, completion: dict, repositories: dict[str, bytes], releases: dict[str, bytes],
            fail_at: str | None = None) -> None:
        # Step 1: Verify bundle before writes
        if fail_at == "pre_verify":
            raise RuntimeError("pre_verify failed: local verification failed")
        publication.verify(completion, repositories, releases)

        # Step 2: Publish immutable download artifacts (simulated)
        if fail_at == "download_artifacts":
            raise RuntimeError("download_artifacts failed: network timeout during R2 upload")

        # Step 3: Publish qualified documents before the commit point
        if fail_at == "first_manifest":
            self.store.put("v1/repos.amd64.manifest.json", repositories["amd64"])
            raise RuntimeError("first_manifest failed: aborted after uploading repos.amd64")

        self.store.put("v1/repos.amd64.manifest.json", repositories["amd64"])
        self.store.put("v1/repos.aarch64.manifest.json", repositories["arm64"])
        self.store.put("v1/releases/devel.amd64.json", releases["amd64"])
        self.store.put("v1/releases/devel.arm64.json", releases["arm64"])

        # Pre-commit verification of published manifests
        visible_repos = {
            "amd64": self.store.get("v1/repos.amd64.manifest.json") or b"",
            "arm64": self.store.get("v1/repos.aarch64.manifest.json") or b"",
        }
        visible_releases = {
            "amd64": self.store.get("v1/releases/devel.amd64.json") or b"",
            "arm64": self.store.get("v1/releases/devel.arm64.json") or b"",
        }
        publication.verify(completion, visible_repos, visible_releases)

        if fail_at == "pre_commit":
            raise RuntimeError("pre_commit failed: failure right before commit point")

        # Step 4: Authoritative atomic commit
        candidate_envelope = wrap_envelope(completion)
        self.store.commit_multiarch(candidate_envelope)

        if fail_at == "refresh_aliases":
            raise RuntimeError("refresh_aliases failed: failure while refreshing legacy amd64 aliases")

        # Step 5: Refresh legacy amd64 aliases after commit point
        self.store.put("v1/repos.manifest.json", repositories["amd64"])
        self.store.put("v1/releases/devel.json", releases["amd64"])


def resolve_channel_state(store: MockPublicationStore) -> dict | None:
    """Client / consumer resolution: reads signed completion first, then verifies
    and resolves qualified repository and release documents."""
    commit_bytes = store.get("v1/releases/devel.multiarch.json")
    if commit_bytes is None:
        return None
    completion = unwrap_envelope(commit_bytes)
    repos = {
        "amd64": store.get("v1/repos.amd64.manifest.json") or b"",
        "arm64": store.get("v1/repos.aarch64.manifest.json") or b"",
    }
    releases = {
        "amd64": store.get("v1/releases/devel.amd64.json") or b"",
        "arm64": store.get("v1/releases/devel.arm64.json") or b"",
    }
    return publication.resolve_authoritative_release(completion, repos, releases)


class PublicationBundleTests(unittest.TestCase):
    def fixture(self):
        return make_bundle(generation=1)

    def test_accepts_exact_local_or_visible_document_bytes(self):
        publication.verify(*self.fixture())

    def test_missing_architecture_and_each_changed_document_fail(self):
        completion, repositories, releases = self.fixture()
        del completion["architectures"]["arm64"]
        with self.assertRaisesRegex(ValueError, "completion"):
            publication.verify(completion, repositories, releases)
        for kind in ("repositories", "releases"):
            completion, repositories, releases = self.fixture()
            selected = repositories if kind == "repositories" else releases
            selected["arm64"] += b" changed"
            with self.assertRaisesRegex(ValueError, "authoritative"):
                publication.verify(completion, repositories, releases)

    def test_invalid_schema_or_channel_fails(self):
        completion, repositories, releases = self.fixture()
        completion["schema_version"] = "invalid.schema/v0"
        with self.assertRaisesRegex(ValueError, "invalid multiarch completion payload"):
            publication.verify(completion, repositories, releases)

        completion, repositories, releases = self.fixture()
        completion["channel"] = "stable"
        with self.assertRaisesRegex(ValueError, "invalid multiarch completion payload"):
            publication.verify(completion, repositories, releases)

    def test_verify_public_succeeds_and_fails_on_mismatch(self):
        completion, repositories, releases = self.fixture()
        url_map = {
            "https://pkg.freesense.org/v1/repos.amd64.manifest.json?multiarch=verify": repositories["amd64"],
            "https://pkg.freesense.org/v1/repos.aarch64.manifest.json?multiarch=verify": repositories["arm64"],
            "https://pkg.freesense.org/v1/releases/devel.amd64.json?multiarch=verify": releases["amd64"],
            "https://pkg.freesense.org/v1/releases/devel.arm64.json?multiarch=verify": releases["arm64"],
        }
        mock_reader = lambda url: url_map[url]
        publication.verify_public("https://pkg.freesense.org/v1", completion, reader=mock_reader)

        # Mismatch on public URL fetch
        url_map["https://pkg.freesense.org/v1/repos.aarch64.manifest.json?multiarch=verify"] = b"tampered"
        with self.assertRaisesRegex(ValueError, "arm64 qualified document differs"):
            publication.verify_public("https://pkg.freesense.org/v1", completion, reader=mock_reader)

    def test_resolve_authoritative_release_parses_json_documents(self):
        completion, repositories, releases = self.fixture()
        resolved = publication.resolve_authoritative_release(completion, repositories, releases)
        self.assertEqual(resolved["amd64"]["repository"]["arch"], "amd64")
        self.assertEqual(resolved["arm64"]["repository"]["arch"], "aarch64")
        self.assertEqual(resolved["amd64"]["release"]["architecture"], "amd64")
        self.assertEqual(resolved["arm64"]["release"]["architecture"], "arm64")


class PublicationFailurePathTests(unittest.TestCase):
    """Rigorous tests covering all 7 publication failure and boundary scenarios:
    1. Failure before qualified manifests are written.
    2. Failure after one qualified manifest is written.
    3. Failure after both manifests but before the authoritative commit.
    4. Concurrent or stale generation publication.
    5. Exact idempotent retry after a completed commit.
    6. Failure while refreshing legacy amd64 aliases.
    7. Confirm the signed multiarch completion remains the authoritative state in every case.
    """

    def setUp(self):
        self.store = MockPublicationStore()
        self.publisher = MultiarchPublisher(self.store)

        # Initialize the store with an existing valid generation 1 release
        self.g1_completion, self.g1_repos, self.g1_releases = make_bundle(generation=1)
        self.publisher.run(self.g1_completion, self.g1_repos, self.g1_releases)

        # Prepare generation 2 bundle
        self.g2_completion, self.g2_repos, self.g2_releases = make_bundle(generation=2)

    def test_failure_before_qualified_manifests_are_written(self):
        """Scenario 1: Failure during pre-validation or download artifact upload before manifest writes."""
        with self.assertRaisesRegex(RuntimeError, "pre_verify failed"):
            self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases, fail_at="pre_verify")

        # Assert no G2 documents exist in store; G1 remains authoritative
        state = resolve_channel_state(self.store)
        self.assertIsNotNone(state)
        self.assertEqual(state["amd64"]["release"]["generation"], 1)
        self.assertEqual(state["arm64"]["release"]["generation"], 1)
        self.assertEqual(self.store.get("v1/repos.amd64.manifest.json"), self.g1_repos["amd64"])

        # Also test failure during download artifact publishing
        with self.assertRaisesRegex(RuntimeError, "download_artifacts failed"):
            self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases, fail_at="download_artifacts")
        state = resolve_channel_state(self.store)
        self.assertEqual(state["amd64"]["release"]["generation"], 1)

    def test_failure_after_one_qualified_manifest_is_written(self):
        """Scenario 2: Failure after writing only one qualified manifest (partial upload)."""
        with self.assertRaisesRegex(RuntimeError, "first_manifest failed"):
            self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases, fail_at="first_manifest")

        # repos.amd64 was overwritten with G2, but other files are still G1
        self.assertEqual(self.store.get("v1/repos.amd64.manifest.json"), self.g2_repos["amd64"])
        self.assertEqual(self.store.get("v1/repos.aarch64.manifest.json"), self.g1_repos["arm64"])

        # The authoritative commit document was NOT updated; it is still at G1
        commit_bytes = self.store.get("v1/releases/devel.multiarch.json")
        self.assertIsNotNone(commit_bytes)
        current_commit = unwrap_envelope(commit_bytes)
        self.assertEqual(current_commit["generation"], 1)

        # Consumers attempting to resolve the release verify against the authoritative completion:
        # Since repos.amd64 now has G2 bytes, it differs from G1's authoritative hash!
        # The consumer detects corruption/inconsistency rather than silently accepting partial state.
        with self.assertRaisesRegex(ValueError, "amd64 qualified document differs from authoritative completion"):
            resolve_channel_state(self.store)

    def test_failure_after_both_manifests_before_authoritative_commit(self):
        """Scenario 3: All qualified manifests written, but failure occurs before commit point."""
        with self.assertRaisesRegex(RuntimeError, "pre_commit failed"):
            self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases, fail_at="pre_commit")

        # All 4 qualified manifests in store contain G2 bytes
        self.assertEqual(self.store.get("v1/repos.amd64.manifest.json"), self.g2_repos["amd64"])
        self.assertEqual(self.store.get("v1/repos.aarch64.manifest.json"), self.g2_repos["arm64"])
        self.assertEqual(self.store.get("v1/releases/devel.amd64.json"), self.g2_releases["amd64"])
        self.assertEqual(self.store.get("v1/releases/devel.arm64.json"), self.g2_releases["arm64"])

        # But the commit document is STILL at G1!
        commit_bytes = self.store.get("v1/releases/devel.multiarch.json")
        current_commit = unwrap_envelope(commit_bytes)
        self.assertEqual(current_commit["generation"], 1)

        # Because G2 was never committed, resolving via completion rejects the manifests because
        # they do not match G1 completion hashes. G2 is uncommitted and never becomes active.
        with self.assertRaisesRegex(ValueError, "differs from authoritative completion"):
            resolve_channel_state(self.store)

    def test_concurrent_or_stale_generation_publication(self):
        """Scenario 4: Rejection of stale generation and concurrent conflicting generation commits."""
        # Advance store cleanly to generation 2
        self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases)
        self.assertEqual(unwrap_envelope(self.store.get("v1/releases/devel.multiarch.json"))["generation"], 2)

        # 4a: Stale runner tries to publish G1 (generation 1 < generation 2)
        stale_envelope = wrap_envelope(self.g1_completion)
        with self.assertRaisesRegex(ValueError, "multiarch completion cannot move backwards"):
            self.store.commit_multiarch(stale_envelope)

        # 4b: Concurrent runner tries to commit a conflicting G2 with different pair fingerprint
        conflicting_g2, _, _ = make_bundle(generation=2, pair_hash="f" * 64)
        conflicting_envelope = wrap_envelope(conflicting_g2)
        with self.assertRaisesRegex(ValueError, "multiarch generation cannot be rewritten"):
            self.store.commit_multiarch(conflicting_envelope)

        # Authoritative state remains the original generation 2
        state = resolve_channel_state(self.store)
        self.assertEqual(state["amd64"]["release"]["generation"], 2)
        self.assertEqual(unwrap_envelope(self.store.get("v1/releases/devel.multiarch.json"))["pair_fingerprint"],
                         self.g2_completion["pair_fingerprint"])

    def test_exact_idempotent_retry_after_completed_commit(self):
        """Scenario 5: Exact idempotent retry after a completed commit succeeds cleanly."""
        # First publication of G2 succeeds
        self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases)
        state_after_first = resolve_channel_state(self.store)
        self.assertEqual(state_after_first["amd64"]["release"]["generation"], 2)

        # Re-executing exact same publication (idempotent retry)
        self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases)
        state_after_retry = resolve_channel_state(self.store)
        self.assertEqual(state_after_retry["amd64"]["release"]["generation"], 2)

        # Multiarch commit returned idempotent success without rewriting the object
        commit_bytes = self.store.get("v1/releases/devel.multiarch.json")
        self.assertEqual(unwrap_envelope(commit_bytes)["generation"], 2)

    def test_failure_while_refreshing_legacy_amd64_aliases(self):
        """Scenario 6: Authoritative commit succeeds, but failure occurs during legacy alias refresh."""
        # Fail at refresh_aliases step
        with self.assertRaisesRegex(RuntimeError, "refresh_aliases failed"):
            self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases, fail_at="refresh_aliases")

        # The authoritative commit of G2 DID succeed!
        commit_bytes = self.store.get("v1/releases/devel.multiarch.json")
        current_commit = unwrap_envelope(commit_bytes)
        self.assertEqual(current_commit["generation"], 2)

        # Qualified manifests are all G2 and fully verified by the completion document
        state = resolve_channel_state(self.store)
        self.assertIsNotNone(state)
        self.assertEqual(state["amd64"]["release"]["generation"], 2)
        self.assertEqual(state["arm64"]["release"]["generation"], 2)

        # Legacy aliases were not yet updated (they remain at G1 from setUp)
        self.assertEqual(self.store.get("v1/repos.manifest.json"), self.g1_repos["amd64"])
        self.assertEqual(self.store.get("v1/releases/devel.json"), self.g1_releases["amd64"])

        # When the pipeline is retried, idempotent execution completes the alias update cleanly
        self.publisher.run(self.g2_completion, self.g2_repos, self.g2_releases)
        self.assertEqual(self.store.get("v1/repos.manifest.json"), self.g2_repos["amd64"])
        self.assertEqual(self.store.get("v1/releases/devel.json"), self.g2_releases["amd64"])

    def test_signed_multiarch_completion_remains_authoritative_state_in_every_case(self):
        """Scenario 7: In all intermediate, interrupted, or retried states, the signed completion
        document alone defines authoritative channel state."""
        # Fresh store with no objects
        fresh_store = MockPublicationStore()
        # Case A: Empty store -> no authoritative release
        self.assertIsNone(resolve_channel_state(fresh_store))

        # Case B: Only loose manifests written without completion -> cannot resolve active release
        fresh_store.put("v1/repos.amd64.manifest.json", self.g1_repos["amd64"])
        fresh_store.put("v1/releases/devel.amd64.json", self.g1_releases["amd64"])
        self.assertIsNone(resolve_channel_state(fresh_store))

        # Case C: After commit, signed completion establishes authoritative release
        fresh_publisher = MultiarchPublisher(fresh_store)
        fresh_publisher.run(self.g1_completion, self.g1_repos, self.g1_releases)
        state_c = resolve_channel_state(fresh_store)
        self.assertIsNotNone(state_c)
        self.assertEqual(state_c["amd64"]["release"]["generation"], 1)

        # Case D: Tampering with signed completion envelope is rejected
        tampered_bytes = fresh_store.get("v1/releases/devel.multiarch.json")
        fresh_store.put("v1/releases/devel.multiarch.json", b'{"schema_version":"invalid"}')
        with self.assertRaisesRegex(ValueError, "unsupported multiarch envelope"):
            resolve_channel_state(fresh_store)

        # Restore valid completion
        fresh_store.put("v1/releases/devel.multiarch.json", tampered_bytes)

        # Case E: Tampering with any qualified manifest referenced by completion is rejected
        fresh_store.put("v1/repos.aarch64.manifest.json", b'{"tampered":true}')
        with self.assertRaisesRegex(ValueError, "arm64 qualified document differs"):
            resolve_channel_state(fresh_store)


if __name__ == "__main__":
    unittest.main()
