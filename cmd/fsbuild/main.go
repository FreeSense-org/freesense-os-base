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
		return errors.New("usage: fsbuild channel <update|verify|seal-stable> [options]")
	}
	flags := newFlagSet("channel " + args[0])
	component := flags.String("component", "", "system or packages")
	fingerprint := flags.String("fingerprint", "", "exact artifact fingerprint")
	privateKeyFile := flags.String("private-key", "", "RSA channel signing key")
	output := flags.String("output", "-", "JSON report path or -")
	architecture := flags.String("architecture", "amd64", "product architecture")
	packageArch := flags.String("package-arch", "amd64", "pkg repository architecture")
	qualifiedManifest := flags.Bool("qualified-manifest", false, "use repos.<package-arch>.manifest.json")
	var mutate func(control.Payload) (control.Payload, error)

	switch args[0] {
	case "update":
		artifactURL := flags.String("url", "", "immutable public artifact URL")
		generation := flags.Uint64("generation", 0, "reserved build generation")
		systemFingerprint := flags.String("system-fingerprint", "", "exact system fingerprint required for packages")
		builtAgainstSystem := flags.String("built-against-system", "", "immutable System used to build packages")
		freeBSDPinID := flags.String("freebsd-pin-id", "", "exact 14-day FreeBSD compatibility pin")
		packageTrain := flags.String("package-train", "", "major.minor compatibility train")
		version := flags.String("version", "", "exact release version")
		abi := flags.String("abi", "FreeBSD:16:amd64", "pkg ABI")
		altABI := flags.String("altabi", "freebsd:16:x86:64", "pkg alternate ABI")
		osVersion := flags.Uint64("osversion", 0, "exact System __FreeBSD_version")
		publishedAt := flags.String("published-at", "", "RFC3339 publication time")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		policy, err := readReleasePolicy()
		if err != nil {
			return err
		}
		if err := validatePolicyVersion(*version, *packageTrain, policy.DevelopmentTrain, "Development"); err != nil {
			return err
		}
		when, err := time.Parse(time.RFC3339, *publishedAt)
		if err != nil {
			return errors.New("--published-at must be RFC3339")
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			return control.Update(payload, control.UpdateOptions{
				Channel: "devel", Component: *component, Fingerprint: *fingerprint,
				SystemFingerprint: *systemFingerprint, BuiltAgainstSystem: *builtAgainstSystem,
				FreeBSDPinID: *freeBSDPinID,
				URL:          *artifactURL, Generation: *generation, Version: *version, PackageTrain: *packageTrain,
				ABI: *abi, AltABI: *altABI, Architecture: *architecture, PackageArch: *packageArch,
				DeclareArchitecture: *qualifiedManifest,
				OSVersion:           *osVersion, PublishedAt: when,
			})
		}
	case "verify":
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			return control.Verify(payload, *component, *fingerprint)
		}
	case "seal-stable":
		version := flags.String("version", "", "exact immutable Stable release version")
		systemFingerprint := flags.String("system-fingerprint", "", "sealed System fingerprint")
		systemURL := flags.String("system-url", "", "immutable System URL")
		systemGeneration := flags.Uint64("system-generation", 0, "System build generation")
		packagesFingerprint := flags.String("packages-fingerprint", "", "sealed Packages fingerprint")
		packagesURL := flags.String("packages-url", "", "immutable Packages URL")
		packagesGeneration := flags.Uint64("packages-generation", 0, "Packages build generation")
		packagesBuiltAgainstSystem := flags.String("packages-built-against-system", "", "immutable System used to build Packages")
		freeBSDPinID := flags.String("freebsd-pin-id", "", "exact FreeBSD compatibility pin")
		packageTrain := flags.String("package-train", "", "sealed package train from build policy")
		abi := flags.String("abi", "FreeBSD:16:amd64", "pkg ABI")
		altABI := flags.String("altabi", "freebsd:16:x86:64", "pkg alternate ABI")
		osVersion := flags.Uint64("osversion", 0, "exact System __FreeBSD_version")
		publishedAt := flags.String("published-at", "", "RFC3339 publication time")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		policy, err := readReleasePolicy()
		if err != nil {
			return err
		}
		if err := validatePolicyVersion(*version, *packageTrain, policy.StableTrain, "Stable"); err != nil {
			return err
		}
		when, err := time.Parse(time.RFC3339, *publishedAt)
		if err != nil {
			return errors.New("--published-at must be RFC3339")
		}
		mutate = func(payload control.Payload) (control.Payload, error) {
			common := control.UpdateOptions{
				Channel: "devel", FreeBSDPinID: *freeBSDPinID, Version: *version, PackageTrain: *packageTrain,
				ABI: *abi, AltABI: *altABI, Architecture: *architecture, PackageArch: *packageArch,
				DeclareArchitecture: *qualifiedManifest,
				PublishedAt:         when,
			}
			system := common
			system.Component, system.Fingerprint, system.URL, system.Generation =
				"system", *systemFingerprint, *systemURL, *systemGeneration
			system.OSVersion = *osVersion
			packages := common
			packages.Component, packages.Fingerprint, packages.SystemFingerprint,
				packages.URL, packages.Generation = "packages", *packagesFingerprint,
				*systemFingerprint, *packagesURL, *packagesGeneration
			packages.BuiltAgainstSystem = *packagesBuiltAgainstSystem
			return control.SealStable(payload, system, packages)
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
	manifestKey, err := control.ManifestKeyForPackageArch(*packageArch, *qualifiedManifest)
	if err != nil {
		return err
	}
	for attempt := 1; attempt <= 5; attempt++ {
		existing, getErr := backend.Get(ctx, manifestKey)
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
			_, created, putErr := backend.PutIfAbsent(ctx, manifestKey, content)
			if putErr == nil && created {
				return writeJSON(*output, map[string]any{"updated": true, "attempt": attempt, "sha256": content.SHA256})
			}
			if putErr != nil && !errors.Is(putErr, store.ErrPrecondition) {
				return putErr
			}
			continue
		}
		_, err = backend.CompareAndSwap(ctx, manifestKey, existing.ETag, content)
		if err == nil {
			return writeJSON(*output, map[string]any{"updated": true, "attempt": attempt, "sha256": content.SHA256})
		}
		if !errors.Is(err, store.ErrPrecondition) {
			return err
		}
	}
	return errors.New("channel manifest changed repeatedly; refusing a lost update")
}

type releasePolicy struct {
	StableTrain      string `json:"stable_train"`
	DevelopmentTrain string `json:"development_train"`
}

func readReleasePolicy() (releasePolicy, error) {
	candidates := []string{os.Getenv("FSBUILD_POLICY"), filepath.Join("config", "build-policy.json"),
		filepath.Join("..", "..", "config", "build-policy.json")}
	var data []byte
	var err error
	for _, candidate := range candidates {
		if candidate == "" {
			continue
		}
		data, err = os.ReadFile(candidate)
		if err == nil {
			break
		}
	}
	if err != nil {
		return releasePolicy{}, fmt.Errorf("read build policy: %w", err)
	}
	var document struct {
		Release releasePolicy `json:"release"`
	}
	if err := json.Unmarshal(data, &document); err != nil {
		return releasePolicy{}, fmt.Errorf("parse build policy: %w", err)
	}
	if !regexp.MustCompile(`^[0-9]+\.[0-9]+$`).MatchString(document.Release.StableTrain) ||
		!regexp.MustCompile(`^[0-9]+\.[0-9]+$`).MatchString(document.Release.DevelopmentTrain) ||
		document.Release.StableTrain == document.Release.DevelopmentTrain {
		return releasePolicy{}, errors.New("build policy has invalid release trains")
	}
	return document.Release, nil
}

func validatePolicyVersion(version, packageTrain, configuredTrain, lifecycle string) error {
	match := regexp.MustCompile(`^([0-9]+)\.([0-9]+)\.[0-9]+$`).FindStringSubmatch(version)
	if len(match) != 3 || packageTrain != configuredTrain ||
		match[1]+"."+match[2] != configuredTrain {
		return fmt.Errorf("%s version and package train must match configured train %s", lifecycle, configuredTrain)
	}
	return nil
}

func commandResult(ctx context.Context, args []string) error {
	if len(args) == 0 || args[0] != "check" {
		return errors.New("usage: fsbuild result check --stage NAME --id SHA256 --platform-id SHA256 [--generation NUMBER] [--system-id SHA256] --github-output PATH")
	}
	flags := newFlagSet("result check")
	stage := flags.String("stage", "", "system, packages, iso, or cloud")
	id := flags.String("id", "", "content-derived result ID")
	systemID := flags.String("system-id", "", "exact required system for packages or ISO")
	packagesID := flags.String("packages-id", "", "exact Packages artifact required for ISO")
	platformID := flags.String("platform-id", "", "exact platform closure")
	packageTrain := flags.String("package-train", "", "required for optional package results")
	freeBSDPinID := flags.String("freebsd-pin-id", "", "required compatibility pin for optional package results")
	generation := flags.Uint64("generation", 0, "expected reserved or selected generation")
	filesystem := flags.String("filesystem", "", "expected cloud filesystem")
	virtualSizeGiB := flags.Uint64("virtual-size-gib", 0, "expected cloud virtual size in GiB")
	architecture := flags.String("architecture", "amd64", "product architecture")
	packageArch := flags.String("package-arch", "amd64", "pkg repository architecture")
	imageProfile := flags.String("image-profile", "generic-amd64", "image platform profile")
	githubOutput := flags.String("github-output", os.Getenv("GITHUB_OUTPUT"), "GitHub output file")
	if err := parseFlags(flags, args[1:]); err != nil {
		return err
	}
	if !map[string]bool{"system": true, "packages": true, "iso": true, "cloud": true}[*stage] ||
		!sha256Pattern.MatchString(*id) || !sha256Pattern.MatchString(*platformID) {
		return errors.New("result stage or ID is invalid")
	}
	if (*stage == "packages" || *stage == "iso" || *stage == "cloud") && !sha256Pattern.MatchString(*systemID) {
		return errors.New("packages and release image result checks require --system-id")
	}
	if (*stage == "iso" || *stage == "cloud") && !sha256Pattern.MatchString(*packagesID) {
		return errors.New("release image result checks require --packages-id")
	}
	if *stage == "packages" && !sha256Pattern.MatchString(*freeBSDPinID) {
		return errors.New("package result checks require --freebsd-pin-id")
	}
	if *stage == "cloud" && ((*filesystem != "ufs" && *filesystem != "zfs") || *virtualSizeGiB == 0) {
		return errors.New("cloud result checks require --filesystem and --virtual-size-gib")
	}
	if !((*architecture == "amd64" && *packageArch == "amd64") ||
		(*architecture == "arm64" && *packageArch == "aarch64")) {
		return errors.New("result architecture mapping is invalid")
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
		marker, validateErr := validateResultMarker(*stage, *id, *systemID, *packagesID, *platformID, *freeBSDPinID, *filesystem, *architecture, *packageArch, *imageProfile, *virtualSizeGiB, *generation, object.Data)
		if validateErr != nil {
			return validateErr
		}
		if *stage == "iso" {
			info, headErr := store.HeadArtifact(ctx, backend, fmt.Sprintf("artifacts/iso/%s/%s", *id, marker.File))
			if headErr != nil || info.Size != marker.Size || (info.SHA256 != "" && info.SHA256 != marker.SHA256) {
				return errors.New("ISO completion marker does not match its immutable image")
			}
		} else if *stage == "cloud" {
			for _, file := range marker.Files {
				info, headErr := store.HeadArtifact(ctx, backend, fmt.Sprintf("artifacts/cloud/%s/%s", *id, file.File))
				if headErr != nil || info.Size != file.Size || (info.SHA256 != "" && info.SHA256 != file.SHA256) {
					return errors.New("cloud completion marker does not match its immutable image")
				}
			}
		} else {
			prefix := fmt.Sprintf("artifacts/%s/%s/%s", *stage, *id, *packageArch)
			if *stage == "packages" {
				prefix = fmt.Sprintf("artifacts/packages/%s/%s/%s", *packageTrain, *id, *packageArch)
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
	SchemaVersion     string          `json:"schema_version"`
	Stage             string          `json:"stage"`
	Fingerprint       string          `json:"fingerprint"`
	Generation        uint64          `json:"generation"`
	System            string          `json:"system"`
	SHA256            string          `json:"sha256"`
	Size              int64           `json:"size"`
	File              string          `json:"file"`
	BundleFingerprint string          `json:"bundle_fingerprint"`
	Filesystem        string          `json:"filesystem"`
	Architecture      string          `json:"architecture"`
	PackageArch       string          `json:"package_arch"`
	Platform          string          `json:"platform"`
	Firmware          []string        `json:"firmware"`
	Capabilities      map[string]bool `json:"capabilities"`
	Files             []struct {
		Format      string `json:"format"`
		SHA256      string `json:"sha256"`
		Size        int64  `json:"size"`
		File        string `json:"file"`
		VirtualSize int64  `json:"virtual_size"`
	} `json:"files"`
	Inputs struct {
		Platform     string `json:"platform"`
		System       string `json:"system"`
		Packages     string `json:"packages"`
		FreeBSDPinID string `json:"freebsd_pin_id"`
	} `json:"inputs"`
}

func validateResultMarker(stage, id, systemID, packagesID, platformID, freeBSDPinID, filesystem, architecture, packageArch, imageProfile string, virtualSizeGiB, generation uint64, data []byte) (resultMarker, error) {
	var marker resultMarker
	if err := json.Unmarshal(data, &marker); err != nil || marker.Fingerprint != id || marker.Generation == 0 {
		return resultMarker{}, errors.New("result completion marker conflicts with its content ID")
	}
	if generation != 0 && marker.Generation != generation {
		return resultMarker{}, errors.New("result completion marker belongs to a different generation")
	}
	legacyAMD64 := marker.Architecture == "" && marker.PackageArch == "" && architecture == "amd64"
	if !legacyAMD64 && (marker.Architecture != architecture || marker.PackageArch != packageArch ||
		marker.Platform != imageProfile || len(marker.Firmware) == 0 || marker.Capabilities == nil) {
		return resultMarker{}, errors.New("result completion marker belongs to a different architecture or platform")
	}
	if stage == "iso" {
		legacy := marker.SchemaVersion == "freesense.iso/v1" && marker.Inputs.Packages == ""
		current := marker.SchemaVersion == "freesense.iso/v2" && marker.Inputs.Packages == packagesID
		if (!legacy && !current) || marker.System != systemID ||
			marker.Inputs.Platform != platformID || !sha256Pattern.MatchString(marker.SHA256) || marker.Size <= 0 ||
			!regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*\.iso$`).MatchString(marker.File) {
			return resultMarker{}, errors.New("ISO completion marker has an invalid closure")
		}
		return marker, nil
	}
	if stage == "cloud" {
		if marker.SchemaVersion != "freesense.cloud-image/v1" ||
			marker.Inputs.System != systemID || marker.Inputs.Packages != packagesID ||
			marker.Inputs.Platform != platformID || !sha256Pattern.MatchString(marker.BundleFingerprint) ||
			marker.Filesystem != filesystem ||
			len(marker.Files) != 2 {
			return resultMarker{}, errors.New("cloud completion marker has an invalid closure")
		}
		formats := map[string]bool{}
		for _, file := range marker.Files {
			if !map[string]bool{"qcow2": true, "raw": true}[file.Format] ||
				!sha256Pattern.MatchString(file.SHA256) || file.Size <= 0 ||
				file.VirtualSize != int64(virtualSizeGiB)*1024*1024*1024 ||
				!regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*\.(qcow2|raw)\.xz$`).MatchString(file.File) {
				return resultMarker{}, errors.New("cloud completion marker has an invalid file")
			}
			formats[file.Format] = true
		}
		if len(formats) != 2 {
			return resultMarker{}, errors.New("cloud completion marker is missing a format")
		}
		return marker, nil
	}
	if marker.SchemaVersion != "freesense.artifact/v1" || marker.Stage != stage {
		return resultMarker{}, errors.New("repository completion marker has an invalid closure")
	}
	if stage == "system" && (marker.Inputs.System != id || marker.Inputs.Platform != platformID) {
		return resultMarker{}, errors.New("system completion marker has an invalid identity")
	}
	if stage == "packages" && marker.Inputs.FreeBSDPinID != freeBSDPinID {
		return resultMarker{}, errors.New("packages completion marker is bound to a different FreeBSD pin")
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
