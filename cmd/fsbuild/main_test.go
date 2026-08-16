package main

import (
	"encoding/json"
	"strings"
	"testing"
)

const (
	resultID     = "1111111111111111111111111111111111111111111111111111111111111111"
	systemID     = "2222222222222222222222222222222222222222222222222222222222222222"
	platformID   = "3333333333333333333333333333333333333333333333333333333333333333"
	otherID      = "4444444444444444444444444444444444444444444444444444444444444444"
	isoSHA256    = "5555555555555555555555555555555555555555555555555555555555555555"
	freeBSDPinID = "6666666666666666666666666666666666666666666666666666666666666666"
)

func TestValidateResultMarkerAcceptsCompleteClosures(t *testing.T) {
	tests := []struct {
		name       string
		stage      string
		id         string
		systemID   string
		platformID string
		marker     resultMarker
	}{
		{
			name:       "system",
			stage:      "system",
			id:         systemID,
			platformID: platformID,
			marker:     repositoryMarker("system", systemID, systemID, platformID),
		},
		{
			name:       "packages",
			stage:      "packages",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     repositoryMarker("packages", resultID, systemID, platformID),
		},
		{
			name:       "iso",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     isoMarker(resultID, systemID, platformID, "FreeSense-16-devel.iso"),
		},
		{
			name:       "legacy iso",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     legacyISOMarker(resultID, systemID, platformID, "FreeSense-16-devel.iso"),
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			marker, err := validateResultMarker(
				test.stage,
				test.id,
				test.systemID,
				resultID,
				test.platformID,
				freeBSDPinID,
				"",
				"amd64", "amd64", "generic-amd64",
				0,
				1,
				marshalMarker(t, test.marker),
			)
			if err != nil {
				t.Fatalf("validateResultMarker() error = %v", err)
			}
			if marker.Fingerprint != test.id {
				t.Fatalf("validated fingerprint = %q, want %q", marker.Fingerprint, test.id)
			}
		})
	}
}

func TestValidateResultMarkerRejectsBrokenClosures(t *testing.T) {
	tests := []struct {
		name       string
		stage      string
		id         string
		systemID   string
		platformID string
		marker     resultMarker
		wantError  string
	}{
		{
			name:       "generation mismatch",
			stage:      "system",
			id:         systemID,
			platformID: platformID,
			marker:     repositoryMarker("system", systemID, systemID, platformID),
			wantError:  "different generation",
		},
		{
			name:       "system platform mismatch",
			stage:      "system",
			id:         systemID,
			platformID: platformID,
			marker:     repositoryMarker("system", systemID, systemID, otherID),
			wantError:  "invalid identity",
		},
		{
			name:       "system identity mismatch",
			stage:      "system",
			id:         systemID,
			platformID: platformID,
			marker:     repositoryMarker("system", systemID, otherID, platformID),
			wantError:  "invalid identity",
		},
		{
			name:       "packages FreeBSD pin mismatch",
			stage:      "packages",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     repositoryMarkerWithPin("packages", resultID, otherID, platformID, otherID),
			wantError:  "different FreeBSD pin",
		},
		{
			name:       "iso platform mismatch",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     isoMarker(resultID, systemID, otherID, "FreeSense.iso"),
			wantError:  "invalid closure",
		},
		{
			name:       "iso system mismatch",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     isoMarker(resultID, otherID, platformID, "FreeSense.iso"),
			wantError:  "invalid closure",
		},
		{
			name:       "iso Packages mismatch",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     isoMarkerWithPackages(resultID, systemID, otherID, platformID, "FreeSense.iso"),
			wantError:  "invalid closure",
		},
		{
			name:       "unsafe iso file",
			stage:      "iso",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     isoMarker(resultID, systemID, platformID, "../FreeSense.iso"),
			wantError:  "invalid closure",
		},
		{
			name:       "content identity mismatch",
			stage:      "packages",
			id:         resultID,
			systemID:   systemID,
			platformID: platformID,
			marker:     repositoryMarker("packages", otherID, systemID, platformID),
			wantError:  "conflicts with its content ID",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			expectedGeneration := uint64(1)
			if test.name == "generation mismatch" {
				expectedGeneration = 2
			}
			_, err := validateResultMarker(
				test.stage,
				test.id,
				test.systemID,
				resultID,
				test.platformID,
				freeBSDPinID,
				"",
				"amd64", "amd64", "generic-amd64",
				0,
				expectedGeneration,
				marshalMarker(t, test.marker),
			)
			if err == nil {
				t.Fatal("validateResultMarker() unexpectedly succeeded")
			}
			if !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("validateResultMarker() error = %q, want substring %q", err, test.wantError)
			}
		})
	}
}

func TestValidateCloudResultMarkerChecksFilesystemAndSize(t *testing.T) {
	marker := map[string]any{
		"schema_version":     "freesense.cloud-image/v1",
		"fingerprint":        resultID,
		"bundle_fingerprint": otherID,
		"generation":         1,
		"filesystem":         "zfs",
		"inputs": map[string]any{
			"system": systemID, "packages": resultID, "platform": platformID,
		},
		"files": []map[string]any{
			{"format": "qcow2", "file": "FreeSense-amd64-zfs.qcow2.xz", "sha256": otherID, "size": 10, "virtual_size": int64(32 * 1024 * 1024 * 1024)},
			{"format": "raw", "file": "FreeSense-amd64-zfs.raw.xz", "sha256": otherID, "size": 10, "virtual_size": int64(32 * 1024 * 1024 * 1024)},
		},
	}
	data, err := json.Marshal(marker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := validateResultMarker(
		"cloud", resultID, systemID, resultID, platformID, freeBSDPinID,
		"zfs", "amd64", "amd64", "generic-amd64", 32, 1, data,
	); err != nil {
		t.Fatalf("valid ZFS marker rejected: %v", err)
	}
	if _, err := validateResultMarker(
		"cloud", resultID, systemID, resultID, platformID, freeBSDPinID,
		"ufs", "amd64", "amd64", "generic-amd64", 16, 1, data,
	); err == nil {
		t.Fatal("ZFS marker accepted as UFS")
	}
}

func repositoryMarker(stage, fingerprint, boundSystem, platform string) resultMarker {
	return repositoryMarkerWithPin(stage, fingerprint, boundSystem, platform, freeBSDPinID)
}

func repositoryMarkerWithPin(stage, fingerprint, boundSystem, platform, pinID string) resultMarker {
	marker := resultMarker{
		SchemaVersion: "freesense.artifact/v1",
		Stage:         stage,
		Fingerprint:   fingerprint,
		Generation:    1,
	}
	marker.Inputs.Platform = platform
	marker.Inputs.System = boundSystem
	marker.Inputs.FreeBSDPinID = pinID
	return marker
}

func isoMarker(fingerprint, system, platform, file string) resultMarker {
	return isoMarkerWithPackages(fingerprint, system, resultID, platform, file)
}

func isoMarkerWithPackages(fingerprint, system, packages, platform, file string) resultMarker {
	marker := resultMarker{
		SchemaVersion: "freesense.iso/v2",
		Fingerprint:   fingerprint,
		Generation:    1,
		System:        system,
		SHA256:        isoSHA256,
		Size:          1024,
		File:          file,
	}
	marker.Inputs.Platform = platform
	marker.Inputs.Packages = packages
	return marker
}

func legacyISOMarker(fingerprint, system, platform, file string) resultMarker {
	marker := isoMarker(fingerprint, system, platform, file)
	marker.SchemaVersion = "freesense.iso/v1"
	marker.Inputs.Packages = ""
	return marker
}

func marshalMarker(t *testing.T, marker resultMarker) []byte {
	t.Helper()
	data, err := json.Marshal(marker)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	return data
}
