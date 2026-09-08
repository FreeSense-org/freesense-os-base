package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

func multiarchFixture() MultiarchRelease {
	hash := strings.Repeat("a", 64)
	result := MultiarchRelease{SchemaVersion: MultiarchSchema, Channel: "devel", Generation: 42,
		PairFingerprint: hash, FreeBSDPin: hash, Architectures: map[string]MultiarchArtifacts{}}
	for arch, artifacts := range map[string][]string{
		"amd64": {"installer", "cloud-ufs", "cloud-zfs"},
		"arm64": {"installer", "arm64-rpi4b", "arm64-rpi5-d0"},
	} {
		entry := MultiarchArtifacts{SystemFingerprint: hash, PackagesFingerprint: hash,
			RepositoryDocument: hash, ReleaseDocument: hash, Artifacts: map[string]string{}}
		for _, artifact := range artifacts {
			entry.Artifacts[artifact] = hash
		}
		result.Architectures[arch] = entry
	}
	return result
}

func TestMultiarchSignatureAndTampering(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	data, err := MarshalMultiarchSigned(multiarchFixture(), key)
	if err != nil {
		t.Fatal(err)
	}
	got, err := ParseMultiarchSigned(data, &key.PublicKey)
	if err != nil || got.Generation != 42 {
		t.Fatalf("round trip: %+v, %v", got, err)
	}
	var envelope Envelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		t.Fatal(err)
	}
	changed := multiarchFixture()
	changed.Generation++
	raw, _ := json.Marshal(changed)
	envelope.Payload = base64.StdEncoding.EncodeToString(raw)
	data, _ = json.Marshal(envelope)
	if _, err := ParseMultiarchSigned(data, &key.PublicKey); err == nil {
		t.Fatal("accepted tampered pair generation")
	}
}

func TestMultiarchRequiresEveryArchitectureAndArtifact(t *testing.T) {
	for _, arch := range []string{"amd64", "arm64"} {
		release := multiarchFixture()
		delete(release.Architectures, arch)
		if err := release.Validate(); err == nil {
			t.Fatalf("accepted absent architecture %s", arch)
		}
		for artifact := range multiarchFixture().Architectures[arch].Artifacts {
			release = multiarchFixture()
			delete(release.Architectures[arch].Artifacts, artifact)
			if err := release.Validate(); err == nil {
				t.Fatalf("accepted absent artifact %s/%s", arch, artifact)
			}
		}
	}
	release := multiarchFixture()
	release.Channel = "stable"
	if err := release.Validate(); err == nil {
		t.Fatal("multiarch rollout must not change Stable")
	}
}

func TestMultiarchCommitIsMonotonicAndIdempotent(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	backend := newMemoryStore()
	first, err := MarshalMultiarchSigned(multiarchFixture(), key)
	if err != nil {
		t.Fatal(err)
	}
	if info, updated, err := CommitMultiarch(context.Background(), backend, first, &key.PublicKey); err != nil || !updated || info.Key != MultiarchCommitKey {
		t.Fatalf("first commit: %+v %v %v", info, updated, err)
	}
	if _, updated, err := CommitMultiarch(context.Background(), backend, first, &key.PublicKey); err != nil || updated {
		t.Fatalf("idempotent commit: %v %v", updated, err)
	}
	changed := multiarchFixture()
	changed.PairFingerprint = strings.Repeat("b", 64)
	rewritten, _ := MarshalMultiarchSigned(changed, key)
	if _, _, err := CommitMultiarch(context.Background(), backend, rewritten, &key.PublicKey); err == nil {
		t.Fatal("rewrote one generation")
	}
	older := multiarchFixture()
	older.Generation--
	olderBytes, _ := MarshalMultiarchSigned(older, key)
	if _, _, err := CommitMultiarch(context.Background(), backend, olderBytes, &key.PublicKey); err == nil {
		t.Fatal("moved completion backwards")
	}
	newer := multiarchFixture()
	newer.Generation++
	newer.PairFingerprint = strings.Repeat("c", 64)
	newerBytes, _ := MarshalMultiarchSigned(newer, key)
	if _, updated, err := CommitMultiarch(context.Background(), backend, newerBytes, &key.PublicKey); err != nil || !updated {
		t.Fatalf("new generation: %v %v", updated, err)
	}
}

func TestMultiarchCommitRejectsTamperedOrUnknownExistingDocuments(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	backend := newMemoryStore()
	backend.objects[MultiarchCommitKey] = store.Object{Key: MultiarchCommitKey, Data: []byte("invalid"), Size: 7, ETag: "etag"}
	next, _ := MarshalMultiarchSigned(multiarchFixture(), key)
	if _, _, err := CommitMultiarch(context.Background(), backend, next, &key.PublicKey); err == nil {
		t.Fatal("overwrote an unverifiable existing commit")
	}
	next[10] ^= 1
	if _, _, err := CommitMultiarch(context.Background(), newMemoryStore(), next, &key.PublicKey); err == nil {
		t.Fatal("accepted tampered new completion")
	}
}
