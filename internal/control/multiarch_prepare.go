package control

import (
	"crypto/rsa"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

// CanaryEvidence is the output of verify_multiarch_canary.py. It must come from
// the trusted same-run verifier; this report is not independently signed evidence.
type CanaryEvidence struct {
	SchemaVersion      string                     `json:"schema_version"`
	PairFingerprint    string                     `json:"pair_fingerprint"`
	FreeBSDPin         string                     `json:"freebsd_pin"`
	Generation         uint64                     `json:"generation"`
	PublicationEnabled bool                       `json:"publication_enabled"`
	Architectures      map[string]CanaryArtifacts `json:"architectures"`
	JobTimings         struct {
		RunID               uint64  `json:"run_id"`
		WatchdogSeconds     int     `json:"watchdog_seconds"`
		MaximumJobSeconds   float64 `json:"maximum_job_seconds"`
		PeakStandardRunners int     `json:"peak_standard_runners"`
	} `json:"job_timings"`
}

type CanaryArtifacts struct {
	SystemFingerprint   string            `json:"system_fingerprint"`
	PackagesFingerprint string            `json:"packages_fingerprint"`
	FreeBSDPinID        string            `json:"freebsd_pin_id"`
	ReleaseDocument     string            `json:"release_document_sha256"`
	Artifacts           map[string]string `json:"artifact_document_sha256"`
}

// PrepareMultiarchCommit binds the trusted verifier report to the exact local
// signed repository manifests and download documents. It performs no store I/O
// and must finish before publication makes any mutable write.
func PrepareMultiarchCommit(evidence CanaryEvidence, repositories, releases map[string][]byte, key *rsa.PublicKey) (MultiarchRelease, error) {
	result := MultiarchRelease{SchemaVersion: MultiarchSchema, Channel: "devel",
		Generation: evidence.Generation, PairFingerprint: evidence.PairFingerprint,
		FreeBSDPin: evidence.FreeBSDPin, Architectures: map[string]MultiarchArtifacts{}}
	if evidence.SchemaVersion != "freesense.multiarch-canary/v1" || evidence.PublicationEnabled ||
		len(evidence.Architectures) != 2 || len(repositories) != 2 || len(releases) != 2 ||
		evidence.JobTimings.RunID == 0 || evidence.JobTimings.WatchdogSeconds != 19800 ||
		evidence.JobTimings.MaximumJobSeconds < 0 || evidence.JobTimings.MaximumJobSeconds >= 19800 ||
		evidence.JobTimings.PeakStandardRunners < 1 || evidence.JobTimings.PeakStandardRunners >= 20 {
		return result, errors.New("complete publish-disabled canary evidence is required")
	}
	for arch, packageArch := range map[string]string{"amd64": "amd64", "arm64": "aarch64"} {
		observed, ok := evidence.Architectures[arch]
		if !ok || !fingerprintPattern.MatchString(observed.FreeBSDPinID) {
			return result, fmt.Errorf("missing %s canary pin binding", arch)
		}
		payload, err := ParseSigned(repositories[arch], key)
		if err != nil {
			return result, fmt.Errorf("verify %s repository document: %w", arch, err)
		}
		if err := ValidateDevelopmentPair(payload, arch, packageArch); err != nil {
			return result, fmt.Errorf("validate %s repository pair: %w", arch, err)
		}
		channel, ok := payload.Channels["devel"]
		if !ok || channel.Architecture != arch || channel.PackageArch != packageArch ||
			channel.System == nil || channel.Packages == nil ||
			!channel.System.Verified || !channel.Packages.Verified ||
			channel.System.Fingerprint != observed.SystemFingerprint ||
			channel.Packages.Fingerprint != observed.PackagesFingerprint ||
			channel.System.FreeBSDPinID != observed.FreeBSDPinID ||
			channel.Packages.FreeBSDPinID != observed.FreeBSDPinID {
			return result, fmt.Errorf("%s repository manifest differs from the verified pair", arch)
		}
		if err := validatePackageBinding(channel, channel.Packages); err != nil {
			return result, err
		}
		if store.BytesContent(releases[arch]).SHA256 != observed.ReleaseDocument {
			return result, fmt.Errorf("%s release document differs from verified bytes", arch)
		}
		var download struct {
			Channel      string `json:"channel"`
			Architecture string `json:"architecture"`
			System       string `json:"system"`
			Generation   uint64 `json:"generation"`
		}
		if err := json.Unmarshal(releases[arch], &download); err != nil {
			return result, err
		}
		if download.Channel != "devel" || download.Architecture != arch ||
			download.System != observed.SystemFingerprint || download.Generation != evidence.Generation {
			return result, fmt.Errorf("%s release does not bind the shared generation", arch)
		}
		result.Architectures[arch] = MultiarchArtifacts{
			SystemFingerprint: observed.SystemFingerprint, PackagesFingerprint: observed.PackagesFingerprint,
			RepositoryDocument: store.BytesContent(repositories[arch]).SHA256,
			ReleaseDocument:    observed.ReleaseDocument, Artifacts: observed.Artifacts,
		}
	}
	return result, result.Validate()
}
