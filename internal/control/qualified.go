package control

import (
	"bytes"
	"context"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

type qualifiedReleaseIdentity struct {
	SchemaVersion string `json:"schema_version"`
	Channel       string `json:"channel"`
	Architecture  string `json:"architecture"`
	Generation    uint64 `json:"generation"`
	System        string `json:"system"`
	Packages      string `json:"packages_fingerprint"`
}

func parseQualified(repositories, release []byte, architecture string, key *rsa.PublicKey) (uint64, error) {
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[architecture]
	if packageArch == "" {
		return 0, errors.New("invalid qualified architecture")
	}
	payload, err := ParseSigned(repositories, key)
	if err != nil {
		return 0, err
	}
	if err := ValidateDevelopmentPair(payload, architecture, packageArch); err != nil {
		return 0, err
	}
	channel := payload.Channels["devel"]
	if channel.System.Generation != channel.Packages.Generation {
		return 0, errors.New("qualified components must share the frozen cycle generation")
	}
	var document qualifiedReleaseIdentity
	if err := json.Unmarshal(release, &document); err != nil {
		return 0, err
	}
	if document.Channel != "devel" || document.Architecture != architecture || document.Generation != channel.System.Generation || document.Generation == 0 {
		return 0, errors.New("qualified release does not bind its repository generation")
	}
	if document.System != channel.System.Fingerprint || document.Packages != channel.Packages.Fingerprint {
		return 0, errors.New("qualified release does not bind its exact repository components")
	}
	return document.Generation, nil
}

func putQualifiedDocument(ctx context.Context, backend store.Backend, key string, data []byte, generation uint64, signed bool, publicKey *rsa.PublicKey) (bool, error) {
	content := store.BytesContent(data)
	for attempt := 0; attempt < 5; attempt++ {
		current, err := backend.Get(ctx, key)
		if errors.Is(err, store.ErrNotFound) {
			_, created, putErr := backend.PutIfAbsent(ctx, key, content)
			if putErr == nil && created {
				return true, nil
			}
			if putErr != nil && !errors.Is(putErr, store.ErrPrecondition) {
				return false, putErr
			}
			continue
		}
		if err != nil {
			return false, err
		}
		if bytes.Equal(current.Data, data) {
			return false, nil
		}
		var oldGeneration uint64
		if signed {
			old, parseErr := ParseSigned(current.Data, publicKey)
			if parseErr != nil {
				return false, fmt.Errorf("verify existing qualified manifest: %w", parseErr)
			}
			channel, ok := old.Channels["devel"]
			if !ok || channel.System == nil {
				return false, errors.New("existing qualified manifest has no Development generation")
			}
			oldGeneration = channel.System.Generation
		} else {
			var old qualifiedReleaseIdentity
			if json.Unmarshal(current.Data, &old) != nil || old.Channel != "devel" || old.Generation == 0 {
				return false, errors.New("invalid existing qualified release")
			}
			oldGeneration = old.Generation
		}
		if generation < oldGeneration {
			return false, errors.New("qualified publication cannot move backwards")
		}
		if generation == oldGeneration {
			return false, errors.New("qualified generation cannot be rewritten")
		}
		if _, swapErr := backend.CompareAndSwap(ctx, key, current.ETag, content); swapErr == nil {
			return true, nil
		} else if !errors.Is(swapErr, store.ErrPrecondition) {
			return false, swapErr
		}
	}
	return false, errors.New("qualified publication changed repeatedly")
}

// CommitQualified stages the release document and commits the signed repository
// manifest last. Consumers treat the latter as the architecture commit point.
func CommitQualified(ctx context.Context, backend store.Backend, repositories, release []byte, architecture string, key *rsa.PublicKey) (bool, error) {
	generation, err := parseQualified(repositories, release, architecture, key)
	if err != nil {
		return false, err
	}
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[architecture]
	if _, err := putQualifiedDocument(ctx, backend, "releases/devel."+architecture+".json", release, generation, false, key); err != nil {
		return false, err
	}
	return putQualifiedDocument(ctx, backend, "repos."+packageArch+".manifest.json", repositories, generation, true, key)
}

// CommitLegacyAMD64 advances the compatibility aliases only after the caller
// has committed the aggregate pair. The repository manifest is the final
// legacy commit point, matching qualified publication ordering.
func CommitLegacyAMD64(ctx context.Context, backend store.Backend, repositories, release []byte, key *rsa.PublicKey) (bool, error) {
	generation, err := parseQualified(repositories, release, "amd64", key)
	if err != nil {
		return false, err
	}
	if _, err := putQualifiedDocument(ctx, backend, "releases/devel.json", release, generation, false, key); err != nil {
		return false, err
	}
	return putQualifiedDocument(ctx, backend, "repos.manifest.json", repositories, generation, true, key)
}
