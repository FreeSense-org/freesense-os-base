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

// testingPair is one Stable-train pair as stable.yml stage=testing publishes it.
func testingPair(version, system, packages string, generation uint64) (UpdateOptions, UpdateOptions) {
	now := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	systemOptions := updateOptions("system", system, "", generation, now)
	packagesOptions := updateOptions("packages", packages, system, generation, now)
	for _, options := range []*UpdateOptions{&systemOptions, &packagesOptions} {
		options.Version, options.PackageTrain = version, "1.1"
		options.Architecture, options.PackageArch, options.DeclareArchitecture = "amd64", "amd64", true
	}
	return systemOptions, packagesOptions
}

func liveDevelopmentWithStable(t *testing.T, key *rsa.PrivateKey) Payload {
	t.Helper()
	base, _ := qualifiedFixture(t, key, "amd64", 10)
	signed := signedWithStable(t, key, base, stableChannelFixture("1.1.5", "1.1", strings.Repeat("d", 64), false))
	payload, err := ParseSigned(signed, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func TestTestingChannelIsPublishedNextToStableAndDevel(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	live := liveDevelopmentWithStable(t, key)
	system, packages := testingPair("1.1.6", strings.Repeat("1", 64), strings.Repeat("2", 64), 20)
	published, err := PublishTesting(clonePayload(t, live), system, packages)
	if err != nil {
		t.Fatal(err)
	}
	testing := published.Channels[TestingChannel]
	if testing.Name != "testing" || testing.Description != "FreeSense 1.1 Testing" || testing.Default ||
		testing.Version != "1.1.6" || !testing.System.Verified || !testing.Packages.Verified {
		t.Fatalf("testing channel = %+v", testing)
	}
	if published.Channels["stable"].Version != "1.1.5" || !published.Channels["devel"].Default {
		t.Fatal("publishing Testing disturbed Stable or Development")
	}
	if err := ValidateDevelopmentPair(published, "amd64", "amd64"); err != nil {
		t.Fatalf("a manifest with Testing no longer validates as a Development pair: %v", err)
	}
}

func TestTestingChannelRollsForwardOnly(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	system, packages := testingPair("1.1.6", strings.Repeat("1", 64), strings.Repeat("2", 64), 20)
	published, err := PublishTesting(liveDevelopmentWithStable(t, key), system, packages)
	if err != nil {
		t.Fatal(err)
	}
	retrySystem, retryPackages := system, packages
	retrySystem.PublishedAt = retrySystem.PublishedAt.Add(time.Hour)
	retryPackages.PublishedAt = retryPackages.PublishedAt.Add(time.Hour)
	again, err := PublishTesting(clonePayload(t, published), retrySystem, retryPackages)
	if err != nil || !again.Channels[TestingChannel].System.PublishedAt.Equal(system.PublishedAt) {
		t.Fatalf("an identical rerun was not a no-op: %v", err)
	}
	newer, newerPackages := testingPair("1.1.6", strings.Repeat("3", 64), strings.Repeat("4", 64), 21)
	if _, err := PublishTesting(clonePayload(t, published), newer, newerPackages); err != nil {
		t.Fatalf("a newer nightly of the same version was refused: %v", err)
	}
	older, olderPackages := testingPair("1.1.6", strings.Repeat("3", 64), strings.Repeat("4", 64), 19)
	if _, err := PublishTesting(clonePayload(t, published), older, olderPackages); err == nil {
		t.Fatal("an older generation replaced the Testing channel")
	}
	next, nextPackages := testingPair("1.1.7", strings.Repeat("5", 64), strings.Repeat("6", 64), 1)
	if _, err := PublishTesting(clonePayload(t, published), next, nextPackages); err != nil {
		t.Fatalf("the next patch version was refused: %v", err)
	}
	previous, previousPackages := testingPair("1.1.5", strings.Repeat("5", 64), strings.Repeat("6", 64), 30)
	if _, err := PublishTesting(clonePayload(t, published), previous, previousPackages); err == nil {
		t.Fatal("the Testing channel moved to an older version")
	}
}

func TestTestingChannelNeedsADefaultNextToIt(t *testing.T) {
	system, packages := testingPair("1.1.6", strings.Repeat("1", 64), strings.Repeat("2", 64), 20)
	if _, err := PublishTesting(Payload{}, system, packages); err == nil {
		t.Fatal("Testing was published as the only channel")
	}
}

func TestDevelopmentSigningCarriesTestingForward(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	system, packages := testingPair("1.1.6", strings.Repeat("1", 64), strings.Repeat("2", 64), 20)
	withTesting, err := PublishTesting(liveDevelopmentWithStable(t, key), system, packages)
	if err != nil {
		t.Fatal(err)
	}
	live, err := MarshalSigned(withTesting, key)
	if err != nil {
		t.Fatal(err)
	}
	freshBytes, _ := qualifiedComponentsFixture(t, key, "amd64", 11, 11, 11, strings.Repeat("c", 64))
	fresh, _ := ParseSigned(freshBytes, &key.PublicKey)
	merged, err := PreserveStable(fresh, live, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if merged.Channels[TestingChannel].Version != "1.1.6" || merged.Channels["stable"].Version != "1.1.5" {
		t.Fatalf("Development signing lost a channel: %v", merged.Channels)
	}
	if err := ValidateDevelopmentPair(merged, "amd64", "amd64"); err != nil {
		t.Fatalf("merged manifest is invalid: %v", err)
	}
	for name, mutate := range map[string]func(*Channel){
		"default":     func(c *Channel) { c.Default = true },
		"label":       func(c *Channel) { c.Description = "Testing" },
		"unverified":  func(c *Channel) { c.Packages.Verified = false },
		"wrong train": func(c *Channel) { c.PackageTrain = "1.2" },
	} {
		broken := clonePayload(t, merged)
		channel := broken.Channels[TestingChannel]
		mutate(&channel)
		broken.Channels[TestingChannel] = channel
		if err := ValidateDevelopmentPair(broken, "amd64", "amd64"); err == nil {
			t.Fatalf("%s: an invalid Testing channel was accepted", name)
		}
	}
}

func TestDevelopmentPublicationNeverDropsOrRollsBackTesting(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	ctx := context.Background()
	backend := newMemoryStore()
	system, packages := testingPair("1.1.6", strings.Repeat("1", 64), strings.Repeat("2", 64), 20)
	withTesting, err := PublishTesting(liveDevelopmentWithStable(t, key), system, packages)
	if err != nil {
		t.Fatal(err)
	}
	live, _ := MarshalSigned(withTesting, key)
	for _, name := range []string{"repos.amd64.manifest.json", "repos.manifest.json"} {
		if _, created, err := backend.PutIfAbsent(ctx, name, store.BytesContent(live)); err != nil || !created {
			t.Fatalf("placing %s: %v %v", name, created, err)
		}
	}
	freshBytes, _ := qualifiedComponentsFixture(t, key, "amd64", 11, 11, 11, strings.Repeat("c", 64))
	fresh, _ := ParseSigned(freshBytes, &key.PublicKey)

	// Signed before Testing existed: Stable is kept, Testing would be dropped.
	staleLive := signedWithStable(t, key, freshBytes, stableChannelFixture("1.1.5", "1.1", strings.Repeat("d", 64), false))
	if _, err := CommitRepositories(ctx, backend, staleLive, "amd64", &key.PublicKey); err == nil {
		t.Fatal("a Development publication dropped the Testing channel")
	}
	// Signed with an older Testing pair: it must not roll Testing back.
	olderSystem, olderPackages := testingPair("1.1.6", strings.Repeat("7", 64), strings.Repeat("8", 64), 19)
	olderTesting, err := PublishTesting(liveDevelopmentWithStable(t, key), olderSystem, olderPackages)
	if err != nil {
		t.Fatal(err)
	}
	stale, _ := PreserveStable(clonePayload(t, fresh), mustSign(t, key, olderTesting), &key.PublicKey)
	if _, err := CommitRepositories(ctx, backend, mustSign(t, key, stale), "amd64", &key.PublicKey); err == nil {
		t.Fatal("a Development publication rolled the Testing channel back")
	}
	// The correct merge publishes.
	kept, err := PreserveStable(clonePayload(t, fresh), live, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if updated, err := CommitRepositories(ctx, backend, mustSign(t, key, kept), "amd64", &key.PublicKey); err != nil || !updated {
		t.Fatalf("a Development pair that keeps Testing: %v %v", updated, err)
	}
}

func mustSign(t *testing.T, key *rsa.PrivateKey, payload Payload) []byte {
	t.Helper()
	signed, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	return signed
}
