package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

type memoryStore struct{ objects map[string]store.Object }

func newMemoryStore() *memoryStore { return &memoryStore{objects: map[string]store.Object{}} }
func (m *memoryStore) Get(_ context.Context, key string) (store.Object, error) {
	object, ok := m.objects[key]
	if !ok {
		return store.Object{}, store.ErrNotFound
	}
	return object, nil
}
func (m *memoryStore) Head(ctx context.Context, key string) (store.ObjectInfo, error) {
	object, err := m.Get(ctx, key)
	return store.ObjectInfo{Key: key, Size: object.Size, ETag: object.ETag, SHA256: object.SHA256}, err
}
func (m *memoryStore) PutIfAbsent(_ context.Context, key string, content store.Content) (store.ObjectInfo, bool, error) {
	if object, ok := m.objects[key]; ok {
		return store.ObjectInfo{Key: key, Size: object.Size, ETag: object.ETag, SHA256: object.SHA256}, false, nil
	}
	reader, _ := content.Open()
	defer reader.Close()
	data := make([]byte, content.Size)
	_, _ = reader.Read(data)
	m.objects[key] = store.Object{Key: key, Data: data, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256}
	return store.ObjectInfo{Key: key, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256}, true, nil
}
func (m *memoryStore) CompareAndSwap(context.Context, string, string, store.Content) (store.ObjectInfo, error) {
	return store.ObjectInfo{}, errors.New("unused")
}

func TestGenerationReservationIsStableAcrossRetries(t *testing.T) {
	backend := newMemoryStore()
	fingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	first, created, err := ReserveGeneration(context.Background(), backend, fingerprint, 17)
	if err != nil || !created || first.Generation != 17 {
		t.Fatalf("first reservation: %#v %v %v", first, created, err)
	}
	second, created, err := ReserveGeneration(context.Background(), backend, fingerprint, 99)
	if err != nil || created || second.Generation != 17 {
		t.Fatalf("retry reservation: %#v %v %v", second, created, err)
	}
}

func TestSignedChannelUpdateVerifyAndPromote(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	fingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, UpdateOptions{
		Channel: "devel", Component: "system", Fingerprint: fingerprint,
		FreeBSDPinID: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
		URL:          "https://pkg.freesense.org/v1/artifacts/system/" + fingerprint + "/amd64",
		Generation:   4, PackageTrain: "1.1", ABI: "FreeBSD:16:amd64", AltABI: "freebsd:16:x86:64", PublishedAt: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := ParseSigned(encoded, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Promote(decoded, "system", now.Add(8*24*time.Hour), 7*24*time.Hour); err == nil {
		t.Fatal("unverified component promoted")
	}
	decoded, err = Verify(decoded, "system", fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	packageFingerprint := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	decoded, err = Update(decoded, updateOptions("packages", packageFingerprint, fingerprint, 5, now))
	if err != nil {
		t.Fatal(err)
	}
	decoded, err = Verify(decoded, "packages", packageFingerprint)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err = Promote(decoded, "system", now.Add(8*24*time.Hour), 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if decoded.Channels["stable"].System.Fingerprint != fingerprint {
		t.Fatal("stable did not receive exact tested system")
	}
	decoded, err = Promote(decoded, "packages", now.Add(8*24*time.Hour), 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
}

func TestStableReleaseCanBeSealedOnlyOnce(t *testing.T) {
	now := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packagesFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	system := updateOptions("system", systemFingerprint, "", 1, now)
	system.PackageTrain = "1.0"
	packages := updateOptions("packages", packagesFingerprint, systemFingerprint, 2, now)
	packages.PackageTrain = "1.0"
	packages.URL = "https://pkg.freesense.org/v1/artifacts/packages/1.0/" + packagesFingerprint + "/amd64"

	payload, err := SealStable(Payload{}, system, packages)
	if err != nil {
		t.Fatal(err)
	}
	stable := payload.Channels["stable"]
	if !stable.Default || stable.System == nil || stable.Packages == nil ||
		!stable.System.Verified || !stable.Packages.Verified {
		t.Fatalf("sealed stable channel is incomplete: %#v", stable)
	}
	retrySystem, retryPackages := system, packages
	retrySystem.PublishedAt = now.Add(time.Hour)
	retryPackages.PublishedAt = now.Add(time.Hour)
	if _, err := SealStable(payload, retrySystem, retryPackages); err != nil {
		t.Fatalf("identical seal retry failed: %v", err)
	}
	changed := packages
	changed.Fingerprint = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	changed.URL = "https://pkg.freesense.org/v1/artifacts/packages/1.0/" + changed.Fingerprint + "/amd64"
	if _, err := SealStable(payload, system, changed); err == nil {
		t.Fatal("changed stable release was accepted")
	}

	rolling, err := Update(payload, updateOptions(
		"system",
		"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
		"",
		3,
		now.Add(2*time.Hour),
	))
	if err != nil {
		t.Fatal(err)
	}
	if !rolling.Channels["devel"].Default || rolling.Channels["stable"].Default {
		t.Fatal("rolling devel did not become the single default channel")
	}
}

func TestStableSealReplacesLegacyV1DevelopmentChannel(t *testing.T) {
	now := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	legacy := Payload{
		SchemaVersion: "freesense.channels/v1",
		Channels: map[string]Channel{
			"devel": {Name: "devel", Default: true, System: &Component{
				Fingerprint: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
			}},
		},
	}
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packagesFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	system := updateOptions("system", systemFingerprint, "", 1, now)
	system.PackageTrain = "1.0"
	packages := updateOptions("packages", packagesFingerprint, systemFingerprint, 2, now)
	packages.PackageTrain = "1.0"
	packages.URL = "https://pkg.freesense.org/v1/artifacts/packages/1.0/" + packagesFingerprint + "/amd64"

	sealed, err := SealStable(legacy, system, packages)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := sealed.Channels["devel"]; ok {
		t.Fatal("legacy v1 devel channel survived the v2 stable seal")
	}
	if !sealed.Channels["stable"].Default {
		t.Fatal("stable is not the initial v2 default channel")
	}
}

func TestManifestRejectsTampering(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	encoded, _ := MarshalSigned(Payload{}, key)
	encoded[len(encoded)-3] ^= 1
	if _, err := ParseSigned(encoded, &key.PublicKey); err == nil {
		t.Fatal("tampered envelope accepted")
	}
}

func TestPackagesRequireCurrentSystemBinding(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packageFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, updateOptions("system", systemFingerprint, "", 1, now))
	if err != nil {
		t.Fatal(err)
	}

	withoutBinding := updateOptions("packages", packageFingerprint, "", 2, now)
	if _, err := Update(payload, withoutBinding); err == nil {
		t.Fatal("packages publication without a system binding was accepted")
	}
	mismatchedBinding := updateOptions("packages", packageFingerprint, "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", 2, now)
	if _, err := Update(payload, mismatchedBinding); err == nil {
		t.Fatal("packages publication for a different system was accepted")
	}

	payload, err = Update(payload, updateOptions("packages", packageFingerprint, systemFingerprint, 2, now))
	if err != nil {
		t.Fatal(err)
	}
	if got := payload.Channels["devel"].Packages.SystemFingerprint; got != systemFingerprint {
		t.Fatalf("packages system binding = %q, want %q", got, systemFingerprint)
	}
}

func TestPublicationURLMustMatchComponentIdentity(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	fingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	options := updateOptions("system", fingerprint, "", 1, now)
	options.URL = "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + fingerprint + "/amd64"
	if _, err := Update(Payload{}, options); err == nil {
		t.Fatal("cross-component artifact URL was accepted")
	}
}

func TestIdenticalUpdatesPreservePublicationAndVerification(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packageFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	systemOptions := updateOptions("system", systemFingerprint, "", 1, now)
	payload, err := Update(Payload{}, systemOptions)
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "system", systemFingerprint)
	if err != nil {
		t.Fatal(err)
	}
	beforeRetry := clonePayload(t, payload)
	systemOptions.PublishedAt = now.Add(time.Hour)
	payload, err = Update(payload, systemOptions)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(payload, beforeRetry) {
		t.Fatal("identical system retry changed the channel payload")
	}

	packageOptions := updateOptions("packages", packageFingerprint, systemFingerprint, 2, now)
	payload, err = Update(payload, packageOptions)
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "packages", packageFingerprint)
	if err != nil {
		t.Fatal(err)
	}
	beforeRetry = clonePayload(t, payload)
	packageOptions.PublishedAt = now.Add(2 * time.Hour)
	payload, err = Update(payload, packageOptions)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(payload, beforeRetry) {
		t.Fatal("identical packages retry changed the channel payload")
	}
}

func TestChangingSystemPreservesPackagesForSameFreeBSDPin(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemA := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	systemB := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	packages := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, updateOptions("system", systemA, "", 1, now))
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Update(payload, updateOptions("packages", packages, systemA, 2, now))
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Update(payload, updateOptions("system", systemB, "", 3, now.Add(time.Hour)))
	if err != nil {
		t.Fatal(err)
	}
	if payload.Channels["devel"].Packages == nil ||
		payload.Channels["devel"].Packages.Fingerprint != packages ||
		payload.Channels["devel"].Packages.SystemFingerprint != systemB {
		t.Fatal("same-pin packages did not survive a devel system change")
	}
}

func TestLegacyUnboundPackagesParseButFailClosed(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packageFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, updateOptions("system", systemFingerprint, "", 1, now))
	if err != nil {
		t.Fatal(err)
	}
	channel := payload.Channels["devel"]
	channel.Packages = &Component{
		Fingerprint: packageFingerprint,
		URL:         artifactURL("packages", packageFingerprint),
		Generation:  2,
		PublishedAt: now,
		Verified:    true,
	}
	payload.Channels["devel"] = channel

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseSigned(encoded, &key.PublicKey)
	if err != nil {
		t.Fatalf("legacy manifest did not parse: %v", err)
	}
	if _, err := Verify(parsed, "packages", packageFingerprint); err == nil {
		t.Fatal("legacy unbound packages were verified")
	}
	if _, err := Promote(parsed, "packages", now.Add(8*24*time.Hour), 7*24*time.Hour); err == nil {
		t.Fatal("legacy unbound packages were promoted")
	}
}

func TestVerifyPackagesRejectsStaleSystemBinding(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemFingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packageFingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, updateOptions("system", systemFingerprint, "", 1, now))
	if err != nil {
		t.Fatal(err)
	}
	channel := payload.Channels["devel"]
	channel.Packages = &Component{
		Fingerprint:        packageFingerprint,
		SystemFingerprint:  "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		BuiltAgainstSystem: systemFingerprint,
		FreeBSDPinID:       "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
		URL:                artifactURL("packages", packageFingerprint),
		Generation:         2,
		PublishedAt:        now,
	}
	payload.Channels["devel"] = channel
	if _, err := Verify(payload, "packages", packageFingerprint); err == nil {
		t.Fatal("packages bound to a stale system were verified")
	}
}

func TestPromotionMaintainsCoherentStablePair(t *testing.T) {
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	systemA := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	packagesA := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	systemB := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	packagesB := "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

	payload, err := Update(Payload{}, updateOptions("system", systemA, "", 1, now))
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "system", systemA)
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Update(payload, updateOptions("packages", packagesA, systemA, 2, now))
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "packages", packagesA)
	if err != nil {
		t.Fatal(err)
	}
	promoteAt := now.Add(8 * 24 * time.Hour)
	if _, err := Promote(payload, "packages", promoteAt, 7*24*time.Hour); err == nil {
		t.Fatal("packages promoted before their system")
	}
	payload, err = Promote(payload, "system", promoteAt, 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if payload.Channels["stable"].Packages != nil {
		t.Fatal("initial system promotion retained packages")
	}
	payload, err = Promote(payload, "packages", promoteAt, 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	assertStablePair(t, payload, systemA, packagesA)

	secondPublishedAt := promoteAt.Add(time.Hour)
	systemBOptions := updateOptions("system", systemB, "", 3, secondPublishedAt)
	systemBOptions.FreeBSDPinID = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
	payload, err = Update(payload, systemBOptions)
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "system", systemB)
	if err != nil {
		t.Fatal(err)
	}
	packagesBOptions := updateOptions("packages", packagesB, systemB, 4, secondPublishedAt)
	packagesBOptions.FreeBSDPinID = systemBOptions.FreeBSDPinID
	payload, err = Update(payload, packagesBOptions)
	if err != nil {
		t.Fatal(err)
	}
	payload, err = Verify(payload, "packages", packagesB)
	if err != nil {
		t.Fatal(err)
	}
	secondPromoteAt := secondPublishedAt.Add(8 * 24 * time.Hour)
	if _, err := Promote(payload, "packages", secondPromoteAt, 7*24*time.Hour); err == nil {
		t.Fatal("new packages mixed with the previous stable system")
	}
	payload, err = Promote(payload, "system", secondPromoteAt, 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if payload.Channels["stable"].Packages != nil {
		t.Fatal("new stable system retained packages bound to the old system")
	}
	payload, err = Promote(payload, "packages", secondPromoteAt, 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	assertStablePair(t, payload, systemB, packagesB)
}

func updateOptions(component, fingerprint, systemFingerprint string, generation uint64, publishedAt time.Time) UpdateOptions {
	return UpdateOptions{
		Channel:           "devel",
		Component:         component,
		Fingerprint:       fingerprint,
		SystemFingerprint: systemFingerprint,
		FreeBSDPinID:      "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
		URL:               artifactURL(component, fingerprint),
		Generation:        generation,
		PackageTrain:      "1.1",
		ABI:               "FreeBSD:16:amd64",
		AltABI:            "freebsd:16:x86:64",
		PublishedAt:       publishedAt,
	}
}

func artifactURL(component, fingerprint string) string {
	if component == "packages" {
		return "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + fingerprint + "/amd64"
	}
	return "https://pkg.freesense.org/v1/artifacts/" + component + "/" + fingerprint + "/amd64"
}

func clonePayload(t *testing.T, payload Payload) Payload {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	cloned, err := ParseSigned(encoded, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	return cloned
}

func assertStablePair(t *testing.T, payload Payload, systemFingerprint, packageFingerprint string) {
	t.Helper()
	stable := payload.Channels["stable"]
	if stable.System == nil || stable.Packages == nil {
		t.Fatal("stable channel does not contain a complete pair")
	}
	if stable.System.Fingerprint != systemFingerprint || stable.Packages.Fingerprint != packageFingerprint {
		t.Fatalf("stable pair = %s/%s, want %s/%s", stable.System.Fingerprint, stable.Packages.Fingerprint, systemFingerprint, packageFingerprint)
	}
	if stable.Packages.SystemFingerprint != stable.System.Fingerprint {
		t.Fatal("stable packages are not bound to the stable system")
	}
}
