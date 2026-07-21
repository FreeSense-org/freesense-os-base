package credentials

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

const SmokeSchema = "fsbuild.credential-broker-smoke/v1"

var deploymentSHAPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

type SmokeOptions struct {
	Backend       store.Backend
	DeploymentSHA string
}

type SmokeResult struct {
	SchemaVersion string `json:"schema_version"`
	Key           string `json:"key"`
	SHA256        string `json:"sha256"`
	Created       bool   `json:"created"`
}

type smokeMarker struct {
	SchemaVersion string `json:"schema_version"`
	DeploymentSHA string `json:"deployment_sha"`
}

// Smoke proves that a broker-issued credential can perform a conditional PUT
// and HEAD on its commit-scoped object. The deterministic key and payload make
// retries idempotent: an existing matching marker is accepted, never replaced.
func Smoke(ctx context.Context, options SmokeOptions) (SmokeResult, error) {
	if options.Backend == nil {
		return SmokeResult{}, errors.New("credential broker smoke requires an object store")
	}
	if !deploymentSHAPattern.MatchString(options.DeploymentSHA) {
		return SmokeResult{}, errors.New("credential broker smoke deployment SHA must be a 40-character lowercase commit")
	}
	key := fmt.Sprintf("smoke/broker/%s.json", options.DeploymentSHA)
	payload, err := json.Marshal(smokeMarker{
		SchemaVersion: SmokeSchema,
		DeploymentSHA: options.DeploymentSHA,
	})
	if err != nil {
		return SmokeResult{}, fmt.Errorf("encode credential broker smoke marker: %w", err)
	}
	payload = append(payload, '\n')
	content := store.BytesContent(payload)
	info, created, err := options.Backend.PutIfAbsent(ctx, key, content)
	if err != nil {
		return SmokeResult{}, fmt.Errorf("conditionally publish credential broker smoke marker: %w", err)
	}
	if info.Key != key || info.Size != content.Size || info.SHA256 != content.SHA256 {
		return SmokeResult{}, errors.New("credential broker smoke marker conflicts with an existing object")
	}
	head, err := options.Backend.Head(ctx, key)
	if err != nil {
		return SmokeResult{}, fmt.Errorf("head credential broker smoke marker: %w", err)
	}
	if head.Key != key || head.Size != content.Size || head.SHA256 != content.SHA256 {
		return SmokeResult{}, errors.New("credential broker smoke HEAD does not match the immutable marker")
	}
	conflict := store.BytesContent(append(append([]byte(nil), payload...), byte(' ')))
	conflictingInfo, conflictingCreated, err := options.Backend.PutIfAbsent(ctx, key, conflict)
	if err != nil {
		return SmokeResult{}, fmt.Errorf("verify credential broker conditional PUT: %w", err)
	}
	if conflictingCreated ||
		conflictingInfo.Key != key ||
		conflictingInfo.Size != content.Size ||
		conflictingInfo.SHA256 != content.SHA256 {
		return SmokeResult{}, errors.New("credential broker smoke conditional PUT replaced an immutable marker")
	}
	return SmokeResult{
		SchemaVersion: SmokeSchema,
		Key:           key,
		SHA256:        content.SHA256,
		Created:       created,
	}, nil
}
