package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

func qualifiedFixture(t *testing.T, key *rsa.PrivateKey, arch string, generation uint64) ([]byte, []byte) {
	t.Helper()
	return qualifiedPairFixture(t, key, arch, generation, generation, generation)
}

func qualifiedPairFixture(t *testing.T, key *rsa.PrivateKey, arch string, systemGeneration, packagesGeneration, releaseGeneration uint64) ([]byte, []byte) {
	t.Helper()
	return qualifiedComponentsFixture(t, key, arch, systemGeneration, packagesGeneration, releaseGeneration, strings.Repeat("a", 64))
}

func qualifiedComponentsFixture(t *testing.T, key *rsa.PrivateKey, arch string, systemGeneration, packagesGeneration, releaseGeneration uint64, hash string) ([]byte, []byte) {
	t.Helper()
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[arch]
	abi := map[string]string{"amd64": "FreeBSD:16:amd64", "arm64": "FreeBSD:16:aarch64"}[arch]
	alt := map[string]string{"amd64": "freebsd:16:x86:64", "arm64": "freebsd:16:aarch64:64"}[arch]
	when := time.Date(2026, 9, 11, 0, 0, 0, 0, time.UTC)
	system := &Component{Fingerprint: hash, FreeBSDPinID: hash, OSVersion: 1600001, URL: "https://pkg.freesense.org/v1/artifacts/system/" + hash + "/" + packageArch, Generation: systemGeneration, PublishedAt: when, Verified: true}
	packages := &Component{Fingerprint: hash, SystemFingerprint: hash, BuiltAgainstSystem: hash, FreeBSDPinID: hash, URL: "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + hash + "/" + packageArch, Generation: packagesGeneration, PublishedAt: when, Verified: true}
	payload := Payload{SchemaVersion: PayloadSchema, Channels: map[string]Channel{"devel": {Name: "devel", Description: "Development version", Version: "1.1.0", PackageTrain: "1.1", ABI: abi, AltABI: alt, Architecture: arch, PackageArch: packageArch, Default: true, System: system, Packages: packages}}}
	repositories, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	release, _ := json.Marshal(map[string]any{"schema_version": "freesense.download/v4", "channel": "devel", "architecture": arch, "generation": releaseGeneration, "system": hash, "packages_fingerprint": hash})
	return repositories, append(release, '\n')
}

func TestQualifiedCommitIsIndependentMonotonicAndIdempotent(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	backend := newMemoryStore()
	repo, release := qualifiedFixture(t, key, "amd64", 10)
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "amd64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("first: %v %v", updated, err)
	}
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "amd64", &key.PublicKey); err != nil || updated {
		t.Fatalf("retry: %v %v", updated, err)
	}
	armRepo, armRelease := qualifiedFixture(t, key, "arm64", 9)
	if updated, err := CommitQualified(context.Background(), backend, armRepo, armRelease, "arm64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("independent arm: %v %v", updated, err)
	}
	olderRepo, olderRelease := qualifiedFixture(t, key, "amd64", 8)
	if _, err := CommitQualified(context.Background(), backend, olderRepo, olderRelease, "amd64", &key.PublicKey); err == nil {
		t.Fatal("accepted rollback")
	}
	// A later cycle republishing the live pair regenerates its documents;
	// the first publication at that generation stands.
	republishRepo, republishRelease := qualifiedFixture(t, key, "amd64", 10)
	republishRelease = append(republishRelease, ' ')
	if updated, err := CommitQualified(context.Background(), backend, republishRepo, republishRelease, "amd64", &key.PublicKey); err != nil || updated {
		t.Fatalf("republished same pair: %v %v", updated, err)
	}
	conflictRepo, conflictRelease := qualifiedComponentsFixture(t, key, "amd64", 10, 10, 10, strings.Repeat("c", 64))
	if _, err := CommitQualified(context.Background(), backend, conflictRepo, conflictRelease, "amd64", &key.PublicKey); err == nil {
		t.Fatal("accepted a different pair at the same generation")
	}
}

func TestQualifiedReleaseBindsComponentsAndLegacyAliasesAreMonotonic(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	backend := newMemoryStore()
	repo, release := qualifiedFixture(t, key, "amd64", 12)
	var changed map[string]any
	if err := json.Unmarshal(release, &changed); err != nil {
		t.Fatal(err)
	}
	changed["packages_fingerprint"] = strings.Repeat("b", 64)
	tampered, _ := json.Marshal(changed)
	if _, err := CommitQualified(context.Background(), backend, repo, tampered, "amd64", &key.PublicKey); err == nil {
		t.Fatal("accepted release bound to different package repository")
	}
	if updated, err := CommitLegacyAMD64(context.Background(), backend, repo, release, &key.PublicKey); err != nil || !updated {
		t.Fatalf("legacy first: %v %v", updated, err)
	}
	if updated, err := CommitLegacyAMD64(context.Background(), backend, repo, release, &key.PublicKey); err != nil || updated {
		t.Fatalf("legacy retry: %v %v", updated, err)
	}
	oldRepo, oldRelease := qualifiedFixture(t, key, "amd64", 11)
	if _, err := CommitLegacyAMD64(context.Background(), backend, oldRepo, oldRelease, &key.PublicKey); err == nil {
		t.Fatal("accepted legacy rollback")
	}
}

// A component keeps the generation of the cycle that built it, so a System-only
// or Packages-only change publishes a pair of two generations. The pair's
// generation is the newer one; the release must bind it and publication must
// still only move forwards.
func TestQualifiedPairMayMixComponentGenerations(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	backend := newMemoryStore()
	repo, staleRelease := qualifiedPairFixture(t, key, "arm64", 5, 9, 5)
	if _, err := CommitQualified(context.Background(), backend, repo, staleRelease, "arm64", &key.PublicKey); err == nil {
		t.Fatal("accepted a release bound to the older component generation")
	}
	repo, release := qualifiedPairFixture(t, key, "arm64", 5, 9, 9)
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "arm64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("reused System with newer Packages: %v %v", updated, err)
	}
	repo, release = qualifiedPairFixture(t, key, "arm64", 12, 9, 12)
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "arm64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("newer System with reused Packages: %v %v", updated, err)
	}
	repo, release = qualifiedPairFixture(t, key, "arm64", 11, 11, 11)
	if _, err := CommitQualified(context.Background(), backend, repo, release, "arm64", &key.PublicKey); err == nil {
		t.Fatal("accepted a pair older than the published pair")
	}
	// Both components reused from earlier cycles, e.g. after a revert: the
	// cycle's newer pair generation still publishes.
	repo, release = qualifiedPairFixture(t, key, "arm64", 5, 9, 15)
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "arm64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("reverted pair under a newer cycle generation: %v %v", updated, err)
	}
}

// legacyStore models objects uploaded before fsbuild published them: the strict
// Get refuses them for missing fsbuild SHA-256 metadata, the artifact reader
// returns them, as the S3 backend does.
type legacyStore struct {
	*memoryStore
	legacy map[string]bool
}

func (l *legacyStore) Get(ctx context.Context, key string) (store.Object, error) {
	if l.legacy[key] {
		return store.Object{}, errors.New("S3 object has no valid fsbuild SHA-256 metadata")
	}
	return l.memoryStore.Get(ctx, key)
}

func (l *legacyStore) GetArtifact(ctx context.Context, key string) (store.Object, error) {
	return l.memoryStore.Get(ctx, key)
}

func (l *legacyStore) HeadArtifact(ctx context.Context, key string) (store.ObjectInfo, error) {
	return l.memoryStore.Head(ctx, key)
}

func TestQualifiedCommitReplacesDocumentsWithoutFsbuildMetadata(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	memory := newMemoryStore()
	backend := &legacyStore{memoryStore: memory, legacy: map[string]bool{"releases/devel.arm64.json": true}}
	_, oldRelease := qualifiedFixture(t, key, "arm64", 3)
	if _, _, err := memory.PutIfAbsent(context.Background(), "releases/devel.arm64.json", store.BytesContent(oldRelease)); err != nil {
		t.Fatal(err)
	}
	repo, release := qualifiedPairFixture(t, key, "arm64", 5, 9, 9)
	if updated, err := CommitQualified(context.Background(), backend, repo, release, "arm64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("replace legacy release document: %v %v", updated, err)
	}
}
