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

// pairGeneration is the newest generation among a published pair's
// components. A component is reused across cycles while its fingerprint is
// unchanged and keeps the generation it was built in, so a System-only or
// Packages-only change legitimately pairs two generations.
func pairGeneration(channel Channel) uint64 {
	var generation uint64
	if channel.System != nil {
		generation = channel.System.Generation
	}
	if channel.Packages != nil && channel.Packages.Generation > generation {
		generation = channel.Packages.Generation
	}
	return generation
}

// qualifiedPair identifies one published System/Packages pair.
type qualifiedPair struct {
	Generation uint64
	System     string
	Packages   string
}

func parseQualified(repositories, release []byte, architecture string, key *rsa.PublicKey) (qualifiedPair, error) {
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[architecture]
	if packageArch == "" {
		return qualifiedPair{}, errors.New("invalid qualified architecture")
	}
	payload, err := ParseSigned(repositories, key)
	if err != nil {
		return qualifiedPair{}, err
	}
	if err := ValidateDevelopmentPair(payload, architecture, packageArch); err != nil {
		return qualifiedPair{}, err
	}
	channel := payload.Channels["devel"]
	var document qualifiedReleaseIdentity
	if err := json.Unmarshal(release, &document); err != nil {
		return qualifiedPair{}, err
	}
	// The release carries the cycle's pair generation. It can be newer than
	// both components (one reverted to a fingerprint an earlier cycle built),
	// but never older than either.
	if document.Channel != "devel" || document.Architecture != architecture || document.Generation == 0 || document.Generation < pairGeneration(channel) {
		return qualifiedPair{}, errors.New("qualified release does not bind its repository generation")
	}
	if document.System != channel.System.Fingerprint || document.Packages != channel.Packages.Fingerprint {
		return qualifiedPair{}, errors.New("qualified release does not bind its exact repository components")
	}
	return qualifiedPair{Generation: document.Generation, System: document.System, Packages: document.Packages}, nil
}

func putQualifiedDocument(ctx context.Context, backend store.Backend, key string, data []byte, pair qualifiedPair, signed bool, publicKey *rsa.PublicKey) (bool, error) {
	content := store.BytesContent(data)
	var lastSwap error
	var lastETag string
	for attempt := 0; attempt < 5; attempt++ {
		// The published documents predate fsbuild publication (the live
		// releases/devel.arm64.json was uploaded without fsbuild's SHA-256
		// metadata), so read them like artifacts: the bytes are hashed either
		// way, the manifest's signature and the release fields are verified
		// below, and the compare-and-swap still uses the object's ETag.
		current, err := store.GetArtifact(ctx, backend, key)
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
		lastETag = current.ETag
		if bytes.Equal(current.Data, data) {
			return false, nil
		}
		var oldGeneration uint64
		var oldSystem, oldPackages string
		if signed {
			old, parseErr := ParseSigned(current.Data, publicKey)
			if parseErr != nil {
				return false, fmt.Errorf("verify existing qualified manifest: %w", parseErr)
			}
			channel, ok := old.Channels["devel"]
			if !ok || channel.System == nil {
				return false, errors.New("existing qualified manifest has no Development generation")
			}
			oldGeneration = pairGeneration(channel)
			oldSystem = channel.System.Fingerprint
			if channel.Packages != nil {
				oldPackages = channel.Packages.Fingerprint
			}
		} else {
			var old qualifiedReleaseIdentity
			if json.Unmarshal(current.Data, &old) != nil || old.Channel != "devel" || old.Generation == 0 {
				return false, errors.New("invalid existing qualified release")
			}
			oldGeneration = old.Generation
			oldSystem, oldPackages = old.System, old.Packages
		}
		if pair.Generation < oldGeneration {
			return false, errors.New("qualified publication cannot move backwards")
		}
		if pair.Generation == oldGeneration {
			// A later cycle republishing the pair already live at this
			// generation regenerates the documents (fresh timestamps) but not
			// their identity: the first publication stands. A different pair
			// at the same generation is still refused.
			if pair.System == oldSystem && pair.Packages == oldPackages {
				return false, nil
			}
			return false, errors.New("qualified generation cannot be rewritten")
		}
		_, swapErr := backend.CompareAndSwap(ctx, key, current.ETag, content)
		if swapErr == nil {
			return true, nil
		}
		if !errors.Is(swapErr, store.ErrPrecondition) {
			return false, swapErr
		}
		lastSwap = swapErr
	}
	if lastSwap == nil {
		return false, fmt.Errorf("qualified publication of %q changed repeatedly", key)
	}
	return false, fmt.Errorf("qualified publication of %q changed repeatedly (etag %q): %w", key, lastETag, lastSwap)
}

// CommitQualified stages the release document and commits the signed repository
// manifest last. Consumers treat the latter as the architecture commit point.
func CommitQualified(ctx context.Context, backend store.Backend, repositories, release []byte, architecture string, key *rsa.PublicKey) (bool, error) {
	pair, err := parseQualified(repositories, release, architecture, key)
	if err != nil {
		return false, err
	}
	packageArch := map[string]string{"amd64": "amd64", "arm64": "aarch64"}[architecture]
	if _, err := putQualifiedDocument(ctx, backend, "releases/devel."+architecture+".json", release, pair, false, key); err != nil {
		return false, err
	}
	return putQualifiedDocument(ctx, backend, "repos."+packageArch+".manifest.json", repositories, pair, true, key)
}

// CommitLegacyAMD64 advances the compatibility aliases only after the caller
// has committed the aggregate pair. The repository manifest is the final
// legacy commit point, matching qualified publication ordering.
func CommitLegacyAMD64(ctx context.Context, backend store.Backend, repositories, release []byte, key *rsa.PublicKey) (bool, error) {
	pair, err := parseQualified(repositories, release, "amd64", key)
	if err != nil {
		return false, err
	}
	if _, err := putQualifiedDocument(ctx, backend, "releases/devel.json", release, pair, false, key); err != nil {
		return false, err
	}
	return putQualifiedDocument(ctx, backend, "repos.manifest.json", repositories, pair, true, key)
}
