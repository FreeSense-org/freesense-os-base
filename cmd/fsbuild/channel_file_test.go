package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/control"
)

func signFileFixture(t *testing.T, key *rsa.PrivateKey) (directory, payloadPath, keyPath string, stable control.Channel) {
	t.Helper()
	directory = t.TempDir()
	hash := strings.Repeat("a", 64)
	pin := strings.Repeat("f", 64)
	when := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)
	devel := control.Channel{
		Name: "devel", Description: "Development version", Version: "1.2.0", PackageTrain: "1.2",
		ABI: "FreeBSD:16:amd64", AltABI: "freebsd:16:x86:64", Architecture: "amd64", PackageArch: "amd64", Default: true,
		System: &control.Component{Fingerprint: hash, FreeBSDPinID: pin, OSVersion: 1600027, Generation: 9, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/system/" + hash + "/amd64"},
		Packages: &control.Component{Fingerprint: hash, SystemFingerprint: hash, BuiltAgainstSystem: hash, FreeBSDPinID: pin,
			Generation: 9, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/packages/1.2/" + hash + "/amd64"},
	}
	raw, err := json.Marshal(control.Payload{SchemaVersion: control.PayloadSchema, Channels: map[string]control.Channel{"devel": devel}})
	if err != nil {
		t.Fatal(err)
	}
	payloadPath = filepath.Join(directory, "payload.json")
	if err := os.WriteFile(payloadPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	keyPath = filepath.Join(directory, "channel.key")
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	if err := os.WriteFile(keyPath, pemKey, 0o600); err != nil {
		t.Fatal(err)
	}
	stableHash := strings.Repeat("d", 64)
	stable = control.Channel{
		Name: "stable", Description: "FreeSense 1.1 Stable", Version: "1.1.0", PackageTrain: "1.1",
		ABI: "FreeBSD:16:amd64", AltABI: "freebsd:16:x86:64", Architecture: "amd64", PackageArch: "amd64",
		System: &control.Component{Fingerprint: stableHash, FreeBSDPinID: pin, OSVersion: 1600027, Generation: 4, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/system/" + stableHash + "/amd64"},
		Packages: &control.Component{Fingerprint: strings.Repeat("e", 64), SystemFingerprint: stableHash, BuiltAgainstSystem: stableHash,
			FreeBSDPinID: pin, Generation: 5, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + strings.Repeat("e", 64) + "/amd64"},
	}
	return directory, payloadPath, keyPath, stable
}

func TestSignFilePreservesTheLiveStableChannel(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	directory, payloadPath, keyPath, stable := signFileFixture(t, key)
	live, err := control.MarshalSigned(control.Payload{Channels: map[string]control.Channel{"stable": stable}}, key)
	if err != nil {
		t.Fatal(err)
	}
	livePath := filepath.Join(directory, "live.json")
	if err := os.WriteFile(livePath, live, 0o600); err != nil {
		t.Fatal(err)
	}
	base := []string{"--payload", payloadPath, "--architecture", "amd64", "--package-arch", "amd64", "--private-key", keyPath}

	plainPath := filepath.Join(directory, "plain.json")
	if err := commandSignChannelFile(append(append([]string{}, base...), "--output", plainPath)); err != nil {
		t.Fatalf("sign without Stable: %v", err)
	}
	plain, _ := os.ReadFile(plainPath)
	parsed, err := control.ParseSigned(plain, &key.PublicKey)
	if err != nil || len(parsed.Channels) != 1 {
		t.Fatalf("plain manifest should carry only devel: %v %v", err, parsed.Channels)
	}

	mergedPath := filepath.Join(directory, "merged.json")
	if err := commandSignChannelFile(append(append([]string{}, base...), "--preserve-stable", livePath, "--output", mergedPath)); err != nil {
		t.Fatalf("sign with Stable: %v", err)
	}
	merged, _ := os.ReadFile(mergedPath)
	parsed, err = control.ParseSigned(merged, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Channels["stable"].Version != "1.1.0" || parsed.Channels["devel"].Version != "1.2.0" {
		t.Fatalf("merged channels are wrong: %v", parsed.Channels)
	}
	if !parsed.Channels["devel"].Default || parsed.Channels["stable"].Default {
		t.Fatal("exactly one default channel is required")
	}

	// A live manifest that does not verify must stop signing, not be ignored.
	other, _ := rsa.GenerateKey(rand.Reader, 2048)
	forged, _ := control.MarshalSigned(control.Payload{Channels: map[string]control.Channel{"stable": stable}}, other)
	forgedPath := filepath.Join(directory, "forged.json")
	if err := os.WriteFile(forgedPath, forged, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := commandSignChannelFile(append(append([]string{}, base...), "--preserve-stable", forgedPath,
		"--output", filepath.Join(directory, "forged-out.json"))); err == nil {
		t.Fatal("signed with a Stable channel from an unverifiable manifest")
	}
}
