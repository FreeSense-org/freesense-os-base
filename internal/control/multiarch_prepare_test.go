package control

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

func preparationFixture(t *testing.T, key *rsa.PrivateKey) (CanaryEvidence, map[string][]byte, map[string][]byte) {
	t.Helper()
	release := multiarchFixture()
	evidence := CanaryEvidence{SchemaVersion: "freesense.multiarch-canary/v1", Generation: release.Generation,
		PairFingerprint: release.PairFingerprint, FreeBSDPin: release.FreeBSDPin, Architectures: map[string]CanaryArtifacts{}}
	evidence.JobTimings.RunID = 123
	evidence.JobTimings.WatchdogSeconds = 19800
	evidence.JobTimings.MaximumJobSeconds = 18000
	evidence.JobTimings.PeakStandardRunners = 10
	repositories, releases := map[string][]byte{}, map[string][]byte{}
	publishedAt := time.Date(2026, 9, 8, 6, 0, 0, 0, time.UTC)
	for arch, packageArch := range map[string]string{"amd64": "amd64", "arm64": "aarch64"} {
		entry := release.Architectures[arch]
		pin := strings.Repeat("b", 64)
		abi := map[string]string{"amd64": "FreeBSD:16:amd64", "arm64": "FreeBSD:16:aarch64"}[arch]
		altabi := map[string]string{"amd64": "freebsd:16:x86:64", "arm64": "freebsd:16:aarch64:64"}[arch]
		payload := Payload{Channels: map[string]Channel{"devel": {
			Name: "devel", Default: true, Version: "1.1.0", PackageTrain: "1.1",
			Architecture: arch, PackageArch: packageArch, ABI: abi, AltABI: altabi,
			System: &Component{Fingerprint: entry.SystemFingerprint, FreeBSDPinID: pin, Verified: true,
				URL:        "https://pkg.freesense.org/v1/artifacts/system/" + entry.SystemFingerprint + "/" + packageArch,
				Generation: 40, OSVersion: 1600019, PublishedAt: publishedAt},
			Packages: &Component{Fingerprint: entry.PackagesFingerprint, FreeBSDPinID: pin, Verified: true,
				SystemFingerprint: entry.SystemFingerprint, BuiltAgainstSystem: strings.Repeat("c", 64),
				URL:        "https://pkg.freesense.org/v1/artifacts/packages/1.1/" + entry.PackagesFingerprint + "/" + packageArch,
				Generation: 41, PublishedAt: publishedAt},
		}}}
		var err error
		repositories[arch], err = MarshalSigned(payload, key)
		if err != nil {
			t.Fatal(err)
		}
		releases[arch], err = json.Marshal(map[string]any{"channel": "devel", "architecture": arch,
			"system": entry.SystemFingerprint, "generation": release.Generation})
		if err != nil {
			t.Fatal(err)
		}
		evidence.Architectures[arch] = CanaryArtifacts{SystemFingerprint: entry.SystemFingerprint,
			PackagesFingerprint: entry.PackagesFingerprint, FreeBSDPinID: pin,
			ReleaseDocument: store.BytesContent(releases[arch]).SHA256, Artifacts: entry.Artifacts}
	}
	return evidence, repositories, releases
}

func TestPrepareMultiarchCommitBindsVerifiedBytes(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	evidence, repositories, releases := preparationFixture(t, key)
	completion, err := PrepareMultiarchCommit(evidence, repositories, releases, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	for _, arch := range []string{"amd64", "arm64"} {
		if completion.Architectures[arch].RepositoryDocument != store.BytesContent(repositories[arch]).SHA256 ||
			completion.Architectures[arch].ReleaseDocument != store.BytesContent(releases[arch]).SHA256 {
			t.Fatalf("completion did not bind exact %s documents", arch)
		}
	}
	signed, err := MarshalMultiarchSigned(completion, key)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseMultiarchSigned(signed, &key.PublicKey); err != nil {
		t.Fatal(err)
	}
}

func TestPrepareMultiarchCommitRejectsIncompleteOrChangedEvidence(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	for _, fault := range []string{"missing-arm", "release-bytes", "signature", "unverified", "different-system", "missing-pi", "generation", "watchdog", "concurrency"} {
		t.Run(fault, func(t *testing.T) {
			evidence, repositories, releases := preparationFixture(t, key)
			switch fault {
			case "missing-arm":
				delete(evidence.Architectures, "arm64")
			case "release-bytes":
				releases["arm64"] = append(releases["arm64"], ' ')
			case "signature":
				repositories["arm64"] = []byte(`{"schema_version":"freesense.repositories/v3","payload":"","signature":""}`)
			case "unverified", "different-system":
				payload, err := ParseSigned(repositories["arm64"], &key.PublicKey)
				if err != nil {
					t.Fatal(err)
				}
				if fault == "unverified" {
					payload.Channels["devel"].Packages.Verified = false
				} else {
					payload.Channels["devel"].Packages.SystemFingerprint = strings.Repeat("f", 64)
				}
				repositories["arm64"], err = MarshalSigned(payload, key)
				if err != nil {
					t.Fatal(err)
				}
			case "missing-pi":
				delete(evidence.Architectures["arm64"].Artifacts, "arm64-rpi5-d0")
			case "generation":
				evidence.Generation++
			case "watchdog":
				evidence.JobTimings.MaximumJobSeconds = 19800
			case "concurrency":
				evidence.JobTimings.PeakStandardRunners = 20
			}
			if _, err := PrepareMultiarchCommit(evidence, repositories, releases, &key.PublicKey); err == nil {
				t.Fatal("accepted invalid pair evidence")
			}
		})
	}
}

func TestValidateDevelopmentPairRequiresQualifiedVerifiedClosure(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	_, repositories, _ := preparationFixture(t, key)
	payload, err := ParseSigned(repositories["arm64"], &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := ValidateDevelopmentPair(payload, "arm64", "aarch64"); err != nil {
		t.Fatal(err)
	}
	for _, fault := range []string{"architecture", "unverified", "system-binding", "pin", "generation", "abi", "version", "system-url", "packages-url", "published-at"} {
		t.Run(fault, func(t *testing.T) {
			changed, err := ParseSigned(repositories["arm64"], &key.PublicKey)
			if err != nil {
				t.Fatal(err)
			}
			channel := changed.Channels["devel"]
			system, packages := *channel.System, *channel.Packages
			channel.System, channel.Packages = &system, &packages
			switch fault {
			case "architecture":
				channel.Architecture = "amd64"
			case "unverified":
				channel.Packages.Verified = false
			case "system-binding":
				channel.Packages.SystemFingerprint = strings.Repeat("f", 64)
			case "pin":
				channel.Packages.FreeBSDPinID = strings.Repeat("f", 64)
			case "generation":
				channel.System.Generation = 0
			case "abi":
				channel.ABI = "FreeBSD:15:aarch64"
			case "version":
				channel.Version = "1.2.0"
			case "system-url":
				channel.System.URL += "/mutable"
			case "packages-url":
				channel.Packages.URL += "/mutable"
			case "published-at":
				channel.Packages.PublishedAt = channel.Packages.PublishedAt.Add(time.Second)
			}
			changed.Channels["devel"] = channel
			if err := ValidateDevelopmentPair(changed, "arm64", "aarch64"); err == nil {
				t.Fatal("accepted incomplete pair")
			}
		})
	}
}
