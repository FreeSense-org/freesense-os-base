package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"strings"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

// stableChannelFixture is a sealed Stable pair for the amd64 fixtures.
func stableChannelFixture(version, train, hash string, isDefault bool) Channel {
	when := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)
	pin := strings.Repeat("f", 64)
	return Channel{
		Name: "stable", Description: "FreeSense " + version + " stable", Version: version, PackageTrain: train,
		ABI: "FreeBSD:16:amd64", AltABI: "freebsd:16:x86:64", Architecture: "amd64", PackageArch: "amd64", Default: isDefault,
		System: &Component{Fingerprint: hash, FreeBSDPinID: pin, OSVersion: 1600027, Generation: 7, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/system/" + hash + "/amd64"},
		Packages: &Component{Fingerprint: strings.Repeat("e", 64), SystemFingerprint: hash, BuiltAgainstSystem: hash, FreeBSDPinID: pin,
			Generation: 8, PublishedAt: when, Verified: true,
			URL: "https://pkg.freesense.org/v1/artifacts/packages/" + train + "/" + strings.Repeat("e", 64) + "/amd64"},
	}
}

func signedWithStable(t *testing.T, key *rsa.PrivateKey, repositories []byte, stable Channel) []byte {
	t.Helper()
	payload, err := ParseSigned(repositories, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	payload.Channels["stable"] = stable
	if stable.Default {
		devel := payload.Channels["devel"]
		devel.Default = false
		payload.Channels["devel"] = devel
	}
	signed, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	return signed
}

func TestPreserveStableCarriesTheChannelIntoANewDevelopmentPair(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	live, _ := qualifiedFixture(t, key, "amd64", 10)
	for _, stableDefault := range []bool{false, true} {
		live := signedWithStable(t, key, live, stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), stableDefault))
		freshBytes, _ := qualifiedComponentsFixture(t, key, "amd64", 11, 11, 11, strings.Repeat("c", 64))
		fresh, err := ParseSigned(freshBytes, &key.PublicKey)
		if err != nil {
			t.Fatal(err)
		}
		merged, err := PreserveStable(fresh, live, &key.PublicKey)
		if err != nil {
			t.Fatalf("default=%v: %v", stableDefault, err)
		}
		if err := ValidateDevelopmentPair(merged, "amd64", "amd64"); err != nil {
			t.Fatalf("default=%v: merged manifest is invalid: %v", stableDefault, err)
		}
		if merged.Channels["stable"].Version != "1.1.0" || merged.Channels["devel"].System.Fingerprint != strings.Repeat("c", 64) {
			t.Fatalf("default=%v: merge lost a channel", stableDefault)
		}
		if merged.Channels["devel"].Default == stableDefault {
			t.Fatalf("default=%v: devel default = %v, want exactly one default", stableDefault, merged.Channels["devel"].Default)
		}
	}
}

func TestPreserveStableWithoutAStableChannelLeavesThePayloadAlone(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	live, _ := qualifiedFixture(t, key, "amd64", 10)
	freshBytes, _ := qualifiedFixture(t, key, "amd64", 11)
	fresh, _ := ParseSigned(freshBytes, &key.PublicKey)
	merged, err := PreserveStable(fresh, live, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := merged.Channels["stable"]; ok || len(merged.Channels) != 1 || !merged.Channels["devel"].Default {
		t.Fatalf("payload changed without a Stable channel: %#v", merged.Channels)
	}
}

func TestPreserveStableRefusesAManifestItCannotVerify(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	other, _ := rsa.GenerateKey(rand.Reader, 2048)
	live, _ := qualifiedFixture(t, other, "amd64", 10)
	fresh, _ := ParseSigned(live, &other.PublicKey)
	if _, err := PreserveStable(fresh, live, &key.PublicKey); err == nil {
		t.Fatal("a manifest signed by another key was trusted")
	}
	if _, err := PreserveStable(fresh, []byte("not json"), &key.PublicKey); err == nil {
		t.Fatal("garbage was treated as an empty manifest")
	}
}

func TestDevelopmentPairRejectsAMalformedOrDoublyDefaultStable(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	repo, _ := qualifiedFixture(t, key, "amd64", 10)
	payload, _ := ParseSigned(repo, &key.PublicKey)
	for name, mutate := range map[string]func(*Channel){
		"two defaults":     func(c *Channel) { c.Default = true },
		"unverified":       func(c *Channel) { c.System.Verified = false },
		"wrong ABI":        func(c *Channel) { c.ABI = "FreeBSD:16:aarch64" },
		"wrong arch":       func(c *Channel) { c.Architecture = "arm64" },
		"train mismatch":   func(c *Channel) { c.PackageTrain = "1.0" },
		"unbound packages": func(c *Channel) { c.Packages.SystemFingerprint = strings.Repeat("0", 64) },
	} {
		stable := stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), false)
		mutate(&stable)
		candidate := Payload{SchemaVersion: PayloadSchema, Channels: map[string]Channel{"devel": payload.Channels["devel"], "stable": stable}}
		if err := ValidateDevelopmentPair(candidate, "amd64", "amd64"); err == nil {
			t.Fatalf("%s: accepted", name)
		}
	}
	if err := ValidateDevelopmentPair(Payload{SchemaVersion: PayloadSchema, Channels: map[string]Channel{
		"devel": payload.Channels["devel"], "stable": stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), false),
		"extra": stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), false)}}, "amd64", "amd64"); err == nil {
		t.Fatal("accepted an unknown third channel")
	}
}

func TestDevelopmentPublicationNeverDropsOrRegressesStable(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	ctx := context.Background()
	backend := newMemoryStore()
	base, _ := qualifiedFixture(t, key, "amd64", 10)
	withStable := signedWithStable(t, key, base, stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), false))
	// seal-stable writes the sealed manifests itself, outside the Development
	// commit path, so place them as the live state.
	for _, name := range []string{"repos.amd64.manifest.json", "repos.manifest.json"} {
		if _, created, err := backend.PutIfAbsent(ctx, name, store.BytesContent(withStable)); err != nil || !created {
			t.Fatalf("placing sealed %s: %v %v", name, created, err)
		}
	}

	// A newer Development pair signed without Stable would erase it.
	withoutStable, _ := qualifiedComponentsFixture(t, key, "amd64", 11, 11, 11, strings.Repeat("c", 64))
	if _, err := CommitRepositories(ctx, backend, withoutStable, "amd64", &key.PublicKey); err == nil {
		t.Fatal("a Development publication dropped the Stable channel")
	}
	// An older Stable (a stale merge racing a newer seal) must not win.
	stale := signedWithStable(t, key, withoutStable, stableChannelFixture("1.0.7", "1.0", strings.Repeat("a", 64), false))
	if _, err := CommitRepositories(ctx, backend, stale, "amd64", &key.PublicKey); err == nil {
		t.Fatal("a Development publication moved Stable backwards")
	}
	// The same version cannot be rewritten to a different pair.
	rewritten := signedWithStable(t, key, withoutStable, stableChannelFixture("1.1.0", "1.1", strings.Repeat("b", 64), false))
	if _, err := CommitRepositories(ctx, backend, rewritten, "amd64", &key.PublicKey); err == nil {
		t.Fatal("a Development publication rewrote an immutable Stable version")
	}
	// The correct merge, and a newer Stable, both publish.
	kept := signedWithStable(t, key, withoutStable, stableChannelFixture("1.1.0", "1.1", strings.Repeat("d", 64), false))
	if updated, err := CommitRepositories(ctx, backend, kept, "amd64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("a Development pair that keeps Stable: %v %v", updated, err)
	}
	newer, _ := qualifiedComponentsFixture(t, key, "amd64", 12, 12, 12, strings.Repeat("9", 64))
	advanced := signedWithStable(t, key, newer, stableChannelFixture("1.1.1", "1.1", strings.Repeat("7", 64), false))
	if updated, err := CommitRepositories(ctx, backend, advanced, "amd64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("a newer Stable alongside a newer pair: %v %v", updated, err)
	}
}

func TestStableArm64SealsIntoTheAarch64ManifestAndValidates(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	now := time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC)
	live, _ := qualifiedFixture(t, key, "arm64", 10)
	payload, err := ParseSigned(live, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	systemFingerprint, packagesFingerprint := strings.Repeat("d", 64), strings.Repeat("e", 64)
	arm64 := func(options *UpdateOptions) {
		options.Version, options.PackageTrain = "1.1.1", "1.1"
		options.ABI, options.AltABI = "FreeBSD:16:aarch64", "freebsd:16:aarch64:64"
		options.Architecture, options.PackageArch, options.DeclareArchitecture = "arm64", "aarch64", true
		options.FreeBSDPinID = strings.Repeat("f", 64)
	}
	system := updateOptions("system", systemFingerprint, "", 6, now)
	system.URL = "https://pkg.freesense.org/v1/artifacts/system/" + systemFingerprint + "/aarch64"
	packages := updateOptions("packages", packagesFingerprint, systemFingerprint, 6, now)
	packages.URL = "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + packagesFingerprint + "/aarch64"
	arm64(&system)
	arm64(&packages)

	sealed, err := SealStable(payload, system, packages)
	if err != nil {
		t.Fatalf("sealing Stable arm64: %v", err)
	}
	stable := sealed.Channels["stable"]
	if stable.Version != "1.1.1" || stable.Architecture != "arm64" || stable.PackageArch != "aarch64" {
		t.Fatalf("stable channel = %+v", stable)
	}
	if sealed.Channels["devel"].System == nil || sealed.Channels["devel"].Version != "1.1.0" {
		t.Fatal("sealing Stable disturbed the Development channel")
	}
	if err := ValidateDevelopmentPair(sealed, "arm64", "aarch64"); err != nil {
		t.Fatalf("sealed aarch64 manifest does not validate: %v", err)
	}
	// An amd64 manifest never accepts the arm64 Stable pair.
	if err := ValidateDevelopmentPair(sealed, "amd64", "amd64"); err == nil {
		t.Fatal("arm64 Stable accepted as an amd64 manifest")
	}
}
