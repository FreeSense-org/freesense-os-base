package control

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

const MultiarchSchema = "freesense.multiarch-release/v1"
const MultiarchCommitKey = "releases/devel.multiarch.json"

// MultiarchRelease is the authoritative commit document for a Development pair.
// Mutable per-architecture manifests are prepared before this document; clients
// must use its exact document hashes to distinguish a completed pair from an
// interrupted sequence of R2 writes.
type MultiarchRelease struct {
	SchemaVersion   string                        `json:"schema_version"`
	Channel         string                        `json:"channel"`
	Generation      uint64                        `json:"generation"`
	PairFingerprint string                        `json:"pair_fingerprint"`
	FreeBSDPin      string                        `json:"freebsd_pin"`
	Architectures   map[string]MultiarchArtifacts `json:"architectures"`
}

type MultiarchArtifacts struct {
	SystemFingerprint   string            `json:"system_fingerprint"`
	PackagesFingerprint string            `json:"packages_fingerprint"`
	RepositoryDocument  string            `json:"repository_document_sha256"`
	ReleaseDocument     string            `json:"release_document_sha256"`
	Artifacts           map[string]string `json:"artifact_document_sha256"`
}

func (release MultiarchRelease) Validate() error {
	if release.SchemaVersion != MultiarchSchema || release.Channel != "devel" || release.Generation == 0 ||
		!fingerprintPattern.MatchString(release.PairFingerprint) || !fingerprintPattern.MatchString(release.FreeBSDPin) {
		return errors.New("invalid Development multiarch release identity")
	}
	if len(release.Architectures) != 2 {
		return errors.New("multiarch release requires exactly amd64 and arm64")
	}
	for arch, required := range map[string][]string{
		"amd64": {"installer", "cloud-ufs", "cloud-zfs"},
		"arm64": {"installer", "arm64-rpi4b", "arm64-rpi5-d0"},
	} {
		result, ok := release.Architectures[arch]
		if !ok || len(result.Artifacts) != len(required) {
			return fmt.Errorf("incomplete %s release artifacts", arch)
		}
		for _, value := range []string{result.SystemFingerprint, result.PackagesFingerprint, result.RepositoryDocument, result.ReleaseDocument} {
			if !fingerprintPattern.MatchString(value) {
				return fmt.Errorf("invalid %s repository or release document identity", arch)
			}
		}
		for _, artifact := range required {
			if !fingerprintPattern.MatchString(result.Artifacts[artifact]) {
				return fmt.Errorf("missing verified %s/%s document", arch, artifact)
			}
		}
	}
	return nil
}

// MarshalMultiarchSigned does not publish anything. Callers must verify every
// referenced immutable document and artifact before attempting mutable writes.
func MarshalMultiarchSigned(release MultiarchRelease, key *rsa.PrivateKey) ([]byte, error) {
	if key == nil {
		return nil, errors.New("multiarch release signing key is required")
	}
	if err := release.Validate(); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(release)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(raw)
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		return nil, err
	}
	data, err := json.Marshal(Envelope{
		SchemaVersion: MultiarchSchema,
		Payload:       base64.StdEncoding.EncodeToString(raw),
		Signature:     base64.StdEncoding.EncodeToString(signature),
	})
	return append(data, '\n'), err
}

func ParseMultiarchSigned(data []byte, key *rsa.PublicKey) (MultiarchRelease, error) {
	var result MultiarchRelease
	var envelope Envelope
	if key == nil {
		return result, errors.New("multiarch verification key is required")
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return result, err
	}
	if envelope.SchemaVersion != MultiarchSchema {
		return result, errors.New("unsupported multiarch envelope")
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(envelope.Payload)
	if err != nil {
		return result, err
	}
	signature, err := base64.StdEncoding.Strict().DecodeString(envelope.Signature)
	if err != nil {
		return result, err
	}
	digest := sha256.Sum256(raw)
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature); err != nil {
		return result, errors.New("invalid multiarch release signature")
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return MultiarchRelease{}, err
	}
	if err := result.Validate(); err != nil {
		return MultiarchRelease{}, err
	}
	return result, nil
}

// CommitMultiarch advances the authoritative document with compare-and-swap.
// Architecture-qualified mutable documents must already contain the exact bytes
// referenced by next. A failed CAS never rewrites a newer completion point.
func CommitMultiarch(ctx context.Context, backend store.Backend, next []byte, key *rsa.PublicKey) (store.ObjectInfo, bool, error) {
	candidate, err := ParseMultiarchSigned(next, key)
	if err != nil {
		return store.ObjectInfo{}, false, err
	}
	content := store.BytesContent(next)
	for attempt := 0; attempt < 5; attempt++ {
		current, getErr := backend.Get(ctx, MultiarchCommitKey)
		if errors.Is(getErr, store.ErrNotFound) {
			info, created, putErr := backend.PutIfAbsent(ctx, MultiarchCommitKey, content)
			if putErr == nil && created {
				return info, true, nil
			}
			if putErr != nil && !errors.Is(putErr, store.ErrPrecondition) {
				return store.ObjectInfo{}, false, putErr
			}
			continue
		}
		if getErr != nil {
			return store.ObjectInfo{}, false, getErr
		}
		existing, err := ParseMultiarchSigned(current.Data, key)
		if err != nil {
			return store.ObjectInfo{}, false, fmt.Errorf("verify existing multiarch completion: %w", err)
		}
		if candidate.Generation < existing.Generation {
			return store.ObjectInfo{}, false, errors.New("multiarch completion cannot move backwards")
		}
		if candidate.Generation == existing.Generation {
			if candidate.PairFingerprint != existing.PairFingerprint || !bytes.Equal(next, current.Data) {
				return store.ObjectInfo{}, false, errors.New("multiarch generation cannot be rewritten")
			}
			return store.ObjectInfo{Key: current.Key, Size: current.Size, ETag: current.ETag, SHA256: current.SHA256}, false, nil
		}
		info, err := backend.CompareAndSwap(ctx, MultiarchCommitKey, current.ETag, content)
		if err == nil {
			return info, true, nil
		}
		if !errors.Is(err, store.ErrPrecondition) {
			return store.ObjectInfo{}, false, err
		}
	}
	return store.ObjectInfo{}, false, errors.New("multiarch completion changed repeatedly; refusing a lost update")
}
