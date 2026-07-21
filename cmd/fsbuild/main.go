// fsbuild contains only the small trusted primitives used by GitHub Actions.
// Build policy lives in the split workflows and checked FreeBSD worker scripts.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/control"
	"github.com/FreeSense-org/freesense-os-base/internal/credentials"
	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

var sha256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func main() {
	if err := run(context.Background(), os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "fsbuild:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return usage()
	}
	switch args[0] {
	case "blob":
		return commandBlob(ctx, args[1:])
	case "result":
		return commandResult(ctx, args[1:])
	case "state":
		return commandState(ctx, args[1:])
	case "channel":
		return commandChannel(ctx, args[1:])
	case "credentials":
		return commandCredentials(ctx, args[1:])
	case "help", "-h", "--help":
		fmt.Println("fsbuild <blob|result|state|channel|credentials> [options]")
		return nil
	default:
		return usage()
	}
}

func usage() error {
	return errors.New("usage: fsbuild <blob|result|state|channel|credentials> [options]")
}

func commandState(ctx context.Context, args []string) error {
	if len(args) == 0 || args[0] != "reserve-generation" {
		return errors.New("usage: fsbuild state reserve-generation --fingerprint SHA256 --proposed NUMBER")
	}
	flags := newFlagSet("state reserve-generation")
	fingerprint := flags.String("fingerprint", "", "canonical input fingerprint")
	proposed := flags.Uint64("proposed", 0, "monotonic workflow run number")
	output := flags.String("output", "-", "JSON report path or -")
	if err := parseFlags(flags, args[1:]); err != nil {
		return err
	}
	backend, err := openStore()
	if err != nil {
		return err
	}
	generation, created, err := control.ReserveGeneration(ctx, backend, *fingerprint, *proposed)
	if err != nil {
		return err
	}
	return writeJSON(*output, map[string]any{
		"schema_version": control.GenerationSchema,
		"fingerprint":    generation.Fingerprint,
		"generation":     generation.Generation,
		"created":        created,
	})
}

func commandChannel(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: fsbuild channel <update|verify|promote> [options]")
	}
	flags := newFlagSet("channel " + args[0])
	component := flags.String("component", "", "system or packages")
	fingerprint := flags.String("fingerprint", "", "exact artifact fingerprint")
	privateKeyFile := flags.String("private-key", "", "RSA channel signing key")
	output := flags.String("output", "-", "JSON report path or -")
	var mutate func(control.Payload) (control.Payload, error)

	switch args[0] {
	case "update":
		artifactURL := flags.String("url", "", "immutable public artifact URL")
		generation := flags.Uint64("generation", 0, "reserved build generation")
		systemFingerprint := flags.String("system-fingerprint", "", "exact system fingerprint required for packages")
		packageTrain := flags.String("package-train", "", "major.minor compatibility train")
		abi := flags.String("abi", "FreeBSD:16:amd64", "pkg ABI")
		altABI := flags.String("altabi", "freebsd:16:x86:64", "pkg alternate ABI")
		publishedAt := flags.String("published-at", "", "RFC3339 publication time")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		when, err := time.Parse(time.RFC3339, *publishedAt)
		if err != nil {
			return errors.New("--published-at must be RFC3339")
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			return control.Update(payload, control.UpdateOptions{
				Channel: "devel", Component: *component, Fingerprint: *fingerprint, SystemFingerprint: *systemFingerprint,
				URL: *artifactURL, Generation: *generation, PackageTrain: *packageTrain,
				ABI: *abi, AltABI: *altABI, PublishedAt: when,
			})
		}
	case "verify":
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			return control.Verify(payload, *component, *fingerprint)
		}
	case "promote":
		soak := flags.Duration("soak", 7*24*time.Hour, "required verified soak")
		nowValue := flags.String("now", "", "RFC3339 evaluation time")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		now := time.Now().UTC()
		if *nowValue != "" {
			parsed, err := time.Parse(time.RFC3339, *nowValue)
			if err != nil {
				return errors.New("--now must be RFC3339")
			}
			now = parsed
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			return control.Promote(payload, *component, now, *soak)
		}
	default:
		return fmt.Errorf("unknown channel command %q", args[0])
	}

	if *privateKeyFile == "" {
		return errors.New("--private-key is required")
	}
	keyData, err := os.ReadFile(*privateKeyFile)
	if err != nil {
		return fmt.Errorf("read channel signing key: %w", err)
	}
	privateKey, err := control.ParsePrivateKey(keyData)
	if err != nil {
		return err
	}
	backend, err := openStore()
	if err != nil {
		return err
	}
	for attempt := 1; attempt <= 5; attempt++ {
		existing, getErr := backend.Get(ctx, control.ManifestKey)
		payload := control.Payload{}
		var beforeMutation []byte
		if getErr == nil {
			payload, err = control.ParseSigned(existing.Data, &privateKey.PublicKey)
			if err != nil {
				return err
			}
			beforeMutation, err = json.Marshal(payload)
			if err != nil {
				return err
			}
		} else if !errors.Is(getErr, store.ErrNotFound) {
			return getErr
		}
		payload, err = mutate(payload)
		if err != nil {
			return err
		}
		if getErr == nil {
			afterMutation, marshalErr := json.Marshal(payload)
			if marshalErr != nil {
				return marshalErr
			}
			if bytes.Equal(beforeMutation, afterMutation) {
				return writeJSON(*output, map[string]any{
					"updated": false, "attempt": attempt, "sha256": store.BytesContent(existing.Data).SHA256,
				})
			}
		}
		encoded, err := control.MarshalSigned(payload, privateKey)
		if err != nil {
			return err
		}
		content := store.BytesContent(encoded)
		if errors.Is(getErr, store.ErrNotFound) {
			_, created, putErr := backend.PutIfAbsent(ctx, control.ManifestKey, content)
			if putErr == nil && created {
				return writeJSON(*output, map[string]any{"updated": true, "attempt": attempt, "sha256": content.SHA256})
			}
			if putErr != nil && !errors.Is(putErr, store.ErrPrecondition) {
				return putErr
			}
			continue
		}
		_, err = backend.CompareAndSwap(ctx, control.ManifestKey, existing.ETag, content)
		if err == nil {
			return writeJSON(*output, map[string]any{"updated": true, "attempt": attempt, "sha256": content.SHA256})
		}
		if !errors.Is(err, store.ErrPrecondition) {
			return err
		}
	}
	return errors.New("channel manifest changed repeatedly; refusing a lost update")
}

func commandResult(ctx context.Context, args []string) error {
	if len(args) == 0 || args[0] != "check" {
		return errors.New("usage: fsbuild result check --stage NAME --id SHA256 --platform-id SHA256 [--generation NUMBER] [--system-id SHA256] --github-output PATH")
	}
	flags := newFlagSet("result check")
	stage := flags.String("stage", "", "platform, system, packages, or iso")
	id := flags.String("id", "", "content-derived result ID")
	systemID := flags.String("system-id", "", "exact required system for packages or ISO")
	platformID := flags.String("platform-id", "", "exact platform closure")
	packageTrain := flags.String("package-train", "", "required for optional package results")
	generation := flags.Uint64("generation", 0, "expected reserved or selected generation")
	githubOutput := flags.String("github-output", os.Getenv("GITHUB_OUTPUT"), "GitHub output file")
	if err := parseFlags(flags, args[1:]); err != nil {
		return err
	}
	if !map[string]bool{"system": true, "packages": true, "iso": true}[*stage] ||
		!sha256Pattern.MatchString(*id) || !sha256Pattern.MatchString(*platformID) {
		return errors.New("result stage or ID is invalid")
	}
	if (*stage == "packages" || *stage == "iso") && !sha256Pattern.MatchString(*systemID) {
		return errors.New("packages and ISO result checks require --system-id")
	}
	backend, err := openStore()
	if err != nil {
		return err
	}
	key := fmt.Sprintf("artifacts/%s/%s/complete.json", *stage, *id)
	if *stage == "packages" {
		if !regexp.MustCompile(`^[0-9]+\.[0-9]+$`).MatchString(*packageTrain) {
			return errors.New("package results require --package-train major.minor")
		}
		key = fmt.Sprintf("artifacts/packages/%s/%s/complete.json", *packageTrain, *id)
	}
	object, err := store.GetArtifact(ctx, backend, key)
	complete := false
	if errors.Is(err, store.ErrNotFound) {
		complete = false
	} else if err != nil {
		return err
	} else {
		marker, validateErr := validateResultMarker(*stage, *id, *systemID, *platformID, *generation, object.Data)
		if validateErr != nil {
			return validateErr
		}
		if *stage == "iso" {
			info, headErr := store.HeadArtifact(ctx, backend, fmt.Sprintf("artifacts/iso/%s/%s", *id, marker.File))
			if headErr != nil || info.Size != marker.Size || (info.SHA256 != "" && info.SHA256 != marker.SHA256) {
				return errors.New("ISO completion marker does not match its immutable image")
			}
		} else {
			prefix := fmt.Sprintf("artifacts/%s/%s/amd64", *stage, *id)
			if *stage == "packages" {
				prefix = fmt.Sprintf("artifacts/packages/%s/%s/amd64", *packageTrain, *id)
			}
			for _, catalog := range []string{"meta.conf", "packagesite.pkg"} {
				info, headErr := store.HeadArtifact(ctx, backend, prefix+"/"+catalog)
				if headErr != nil || info.Size <= 0 {
					return errors.New("repository completion marker is missing a required catalog")
				}
			}
		}
		complete = true
	}
	if *githubOutput == "" {
		return errors.New("--github-output is required")
	}
	file, err := os.OpenFile(*githubOutput, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	defer file.Close()
	_, err = fmt.Fprintf(file, "complete=%t\n", complete)
	return err
}

type resultMarker struct {
	SchemaVersion string `json:"schema_version"`
	Stage         string `json:"stage"`
	Fingerprint   string `json:"fingerprint"`
	Generation    uint64 `json:"generation"`
	System        string `json:"system"`
	SHA256        string `json:"sha256"`
	Size          int64  `json:"size"`
	File          string `json:"file"`
	Inputs        struct {
		Platform string `json:"platform"`
		System   string `json:"system"`
	} `json:"inputs"`
}

func validateResultMarker(stage, id, systemID, platformID string, generation uint64, data []byte) (resultMarker, error) {
	var marker resultMarker
	if err := json.Unmarshal(data, &marker); err != nil || marker.Fingerprint != id || marker.Generation == 0 {
		return resultMarker{}, errors.New("result completion marker conflicts with its content ID")
	}
	if generation != 0 && marker.Generation != generation {
		return resultMarker{}, errors.New("result completion marker belongs to a different generation")
	}
	if stage == "iso" {
		if marker.SchemaVersion != "freesense.iso/v1" || marker.System != systemID ||
			marker.Inputs.Platform != platformID || !sha256Pattern.MatchString(marker.SHA256) || marker.Size <= 0 ||
			!regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*\.iso$`).MatchString(marker.File) {
			return resultMarker{}, errors.New("ISO completion marker has an invalid closure")
		}
		return marker, nil
	}
	if marker.SchemaVersion != "freesense.artifact/v1" || marker.Stage != stage || marker.Inputs.Platform != platformID {
		return resultMarker{}, errors.New("repository completion marker has an invalid closure")
	}
	if stage == "system" && marker.Inputs.System != id {
		return resultMarker{}, errors.New("system completion marker has an invalid identity")
	}
	if stage == "packages" && marker.Inputs.System != systemID {
		return resultMarker{}, errors.New("packages completion marker is bound to a different system")
	}
	return marker, nil
}

func commandBlob(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: fsbuild blob <check|put|url> [options]")
	}
	if args[0] == "check" {
		flags := newFlagSet("blob check")
		sha := flags.String("sha256", "", "expected content SHA-256")
		output := flags.String("output", "-", "JSON report path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		if !sha256Pattern.MatchString(*sha) {
			return errors.New("--sha256 must be lowercase hexadecimal SHA-256")
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		key := "inputs/sha256/" + *sha
		info, err := backend.Head(ctx, key)
		if err != nil {
			return err
		}
		if info.SHA256 != *sha || info.Size <= 0 {
			return errors.New("immutable blob metadata conflicts with its SHA-256 identity")
		}
		return writeJSON(*output, map[string]any{
			"schema_version": "freesense.blob/v1",
			"key":            key, "sha256": info.SHA256, "size": info.Size, "created": false,
		})
	}
	if args[0] == "url" {
		flags := newFlagSet("blob url")
		sha := flags.String("sha256", "", "exact content SHA-256")
		validity := flags.Duration("validity", 30*time.Minute, "short-lived URL validity")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		if !sha256Pattern.MatchString(*sha) {
			return errors.New("--sha256 must be lowercase hexadecimal SHA-256")
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		key := "inputs/sha256/" + *sha
		info, err := backend.Head(ctx, key)
		if err != nil {
			return err
		}
		if info.SHA256 != *sha || info.Size <= 0 {
			return errors.New("immutable blob metadata conflicts with its SHA-256 identity")
		}
		signer, ok := backend.(store.GetURLSigner)
		if !ok {
			return errors.New("blob URLs require an S3-compatible store")
		}
		url, err := signer.PresignGet(key, *validity)
		if err != nil {
			return err
		}
		fmt.Println(url)
		return nil
	}
	if args[0] != "put" {
		return errors.New("usage: fsbuild blob <check|put|url> [options]")
	}
	flags := newFlagSet("blob put")
	filename := flags.String("file", "", "file to commit by SHA-256")
	output := flags.String("output", "-", "JSON report path or -")
	if err := parseFlags(flags, args[1:]); err != nil {
		return err
	}
	if *filename == "" {
		return errors.New("--file is required")
	}
	content, err := store.FileContent(*filename)
	if err != nil {
		return err
	}
	backend, err := openStore()
	if err != nil {
		return err
	}
	key := "inputs/sha256/" + content.SHA256
	info, created, err := backend.PutIfAbsent(ctx, key, content)
	if err != nil {
		return err
	}
	if info.Size != content.Size || info.SHA256 != content.SHA256 {
		return errors.New("existing immutable blob conflicts with its SHA-256 identity")
	}
	return writeJSON(*output, map[string]any{
		"schema_version": "freesense.blob/v1",
		"key":            key, "sha256": content.SHA256, "size": content.Size, "created": created,
	})
}

func commandCredentials(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("credentials requires acquire or smoke")
	}
	switch args[0] {
	case "acquire":
		flags := newFlagSet("credentials acquire")
		brokerURL := flags.String("broker-url", os.Getenv("R2_CREDENTIAL_BROKER_URL"), "credential broker URL")
		audience := flags.String("audience", os.Getenv("R2_CREDENTIAL_BROKER_AUDIENCE"), "GitHub OIDC audience")
		role := flags.String("role", "", "broker role")
		bucket := flags.String("expected-bucket", os.Getenv("R2_BUCKET"), "expected R2 bucket")
		prefix := flags.String("expected-prefix", "v1", "expected R2 prefix")
		endpoint := flags.String("expected-endpoint", os.Getenv("R2_ENDPOINT"), "expected R2 endpoint")
		githubEnv := flags.String("github-env", os.Getenv("GITHUB_ENV"), "GitHub environment file")
		minimum := flags.Duration("minimum-validity", 5*time.Minute, "minimum remaining validity")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		if os.Getenv("GITHUB_ACTIONS") != "true" {
			return errors.New("credential acquisition is available only in GitHub Actions")
		}
		return credentials.AcquireAndExport(ctx, credentials.Options{
			BrokerURL: *brokerURL, Audience: *audience, Role: *role,
			ExpectedBucket: *bucket, ExpectedPrefix: *prefix, ExpectedEndpoint: *endpoint,
			GitHubEnv: *githubEnv, OIDCRequestURL: os.Getenv("ACTIONS_ID_TOKEN_REQUEST_URL"),
			OIDCRequestToken: os.Getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN"), MinimumValidity: *minimum,
		})
	case "smoke":
		flags := newFlagSet("credentials smoke")
		sha := flags.String("deployment-sha", os.Getenv("GITHUB_SHA"), "deployed commit")
		output := flags.String("output", "-", "JSON report path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		result, err := credentials.Smoke(ctx, credentials.SmokeOptions{Backend: backend, DeploymentSHA: *sha})
		if err != nil {
			return err
		}
		return writeJSON(*output, result)
	default:
		return fmt.Errorf("unknown credentials command %q", args[0])
	}
}

func openStore() (store.Backend, error) {
	bucket := os.Getenv("R2_BUCKET")
	if bucket == "" || os.Getenv("AWS_ACCESS_KEY_ID") == "" || os.Getenv("AWS_SECRET_ACCESS_KEY") == "" {
		return nil, errors.New("R2_BUCKET and temporary AWS credentials are required")
	}
	prefix := strings.Trim(os.Getenv("FSBUILD_STORE_PREFIX"), "/")
	if prefix == "" {
		prefix = "v1"
	}
	return store.Open("s3://" + bucket + "/" + prefix)
}

func newFlagSet(name string) *flag.FlagSet {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	return flags
}

func parseFlags(flags *flag.FlagSet, args []string) error {
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(flags.Args(), " "))
	}
	return nil
}

func writeJSON(filename string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if filename == "-" {
		_, err = os.Stdout.Write(data)
		return err
	}
	if filename == "" {
		return errors.New("output path is required")
	}
	if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
		return err
	}
	return os.WriteFile(filename, data, 0o600)
}
