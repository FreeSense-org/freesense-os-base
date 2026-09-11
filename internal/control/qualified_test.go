package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func qualifiedFixture(t *testing.T, key *rsa.PrivateKey, arch string, generation uint64) ([]byte, []byte) {
	t.Helper()
	hash := strings.Repeat("a", 64)
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[arch]
	abi := map[string]string{"amd64": "FreeBSD:16:amd64", "arm64": "FreeBSD:16:aarch64"}[arch]
	alt := map[string]string{"amd64": "freebsd:16:x86:64", "arm64": "freebsd:16:aarch64:64"}[arch]
	when := time.Date(2026, 9, 11, 0, 0, 0, 0, time.UTC)
	system := &Component{Fingerprint: hash, FreeBSDPinID: hash, OSVersion: 1600001, URL: "https://pkg.freesense.org/v1/artifacts/system/" + hash + "/" + packageArch, Generation: generation, PublishedAt: when, Verified: true}
	packages := &Component{Fingerprint: hash, SystemFingerprint: hash, BuiltAgainstSystem: hash, FreeBSDPinID: hash, URL: "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + hash + "/" + packageArch, Generation: generation, PublishedAt: when, Verified: true}
	payload := Payload{SchemaVersion: PayloadSchema, Channels: map[string]Channel{"devel": {Name: "devel", Description: "Development version", Version: "1.1.0", PackageTrain: "1.1", ABI: abi, AltABI: alt, Architecture: arch, PackageArch: packageArch, Default: true, System: system, Packages: packages}}}
	repositories, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	release, _ := json.Marshal(map[string]any{"schema_version": "freesense.download/v4", "channel": "devel", "architecture": arch, "generation": generation, "system": hash, "packages_fingerprint": hash})
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
	conflictRepo, conflictRelease := qualifiedFixture(t, key, "amd64", 10)
	conflictRelease = append(conflictRelease, ' ')
	if _, err := CommitQualified(context.Background(), backend, conflictRepo, conflictRelease, "amd64", &key.PublicKey); err == nil {
		t.Fatal("accepted same-generation conflict")
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
