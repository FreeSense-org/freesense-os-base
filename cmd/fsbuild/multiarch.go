package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"

	"github.com/FreeSense-org/freesense-os-base/internal/control"
)

// This is deliberately a local preparation command. Store publication is a
// separate gate; preparing a signature cannot advance a release channel.
func commandMultiarch(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("usage: fsbuild multiarch <prepare|verify|commit|cycle-commit>")
	}
	if args[0] == "cycle-get" {
		flags := newFlagSet("multiarch cycle-get")
		output := flags.String("output", "-", "cycle JSON path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		cycle, exists, err := control.LoadDevelopmentCycle(ctx, backend)
		if err != nil {
			return err
		}
		if !exists {
			return writeJSON(*output, map[string]any{"exists": false})
		}
		return writeJSON(*output, struct {
			Exists bool                     `json:"exists"`
			Cycle  control.DevelopmentCycle `json:"cycle"`
		}{true, cycle})
	}
	if args[0] == "cycle-commit" {
		flags := newFlagSet("multiarch cycle-commit")
		document := flags.String("document", "", "Development cycle JSON")
		output := flags.String("output", "-", "commit result path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		raw, err := os.ReadFile(*document)
		if err != nil {
			return err
		}
		var cycle control.DevelopmentCycle
		if err := json.Unmarshal(raw, &cycle); err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		info, updated, err := control.CommitDevelopmentCycle(ctx, backend, cycle)
		if err != nil {
			return err
		}
		return writeJSON(*output, map[string]any{"updated": updated, "key": info.Key, "sha256": info.SHA256})
	}
	if args[0] == "commit-qualified" {
		flags := newFlagSet("multiarch commit-qualified")
		architecture := flags.String("architecture", "", "amd64 or arm64")
		repositories := flags.String("repositories", "", "signed qualified repository manifest")
		release := flags.String("release", "", "qualified release document")
		publicKey := flags.String("public-key", "", "RSA verification key")
		output := flags.String("output", "-", "commit result path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		repositoryBytes, err := os.ReadFile(*repositories)
		if err != nil {
			return err
		}
		releaseBytes, err := os.ReadFile(*release)
		if err != nil {
			return err
		}
		keyBytes, err := os.ReadFile(*publicKey)
		if err != nil {
			return err
		}
		key, err := control.ParsePublicKey(keyBytes)
		if err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		updated, err := control.CommitQualified(ctx, backend, repositoryBytes, releaseBytes, *architecture, key)
		if err != nil {
			return err
		}
		return writeJSON(*output, map[string]any{"updated": updated, "architecture": *architecture})
	}
	if args[0] == "commit-legacy-amd64" {
		flags := newFlagSet("multiarch commit-legacy-amd64")
		repositories := flags.String("repositories", "", "signed amd64 repository manifest")
		release := flags.String("release", "", "amd64 release document")
		publicKey := flags.String("public-key", "", "RSA verification key")
		output := flags.String("output", "-", "commit result path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		repositoryBytes, err := os.ReadFile(*repositories)
		if err != nil {
			return err
		}
		releaseBytes, err := os.ReadFile(*release)
		if err != nil {
			return err
		}
		keyBytes, err := os.ReadFile(*publicKey)
		if err != nil {
			return err
		}
		key, err := control.ParsePublicKey(keyBytes)
		if err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		updated, err := control.CommitLegacyAMD64(ctx, backend, repositoryBytes, releaseBytes, key)
		if err != nil {
			return err
		}
		return writeJSON(*output, map[string]any{"updated": updated, "architecture": "amd64"})
	}
	if args[0] == "verify" {
		flags := newFlagSet("multiarch verify")
		document := flags.String("document", "", "signed multiarch completion document")
		publicKey := flags.String("public-key", "", "RSA verification key")
		output := flags.String("output", "-", "verified payload JSON path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		documentBytes, err := os.ReadFile(*document)
		if err != nil {
			return err
		}
		keyBytes, err := os.ReadFile(*publicKey)
		if err != nil {
			return err
		}
		key, err := control.ParsePublicKey(keyBytes)
		if err != nil {
			return err
		}
		payload, err := control.ParseMultiarchSigned(documentBytes, key)
		if err != nil {
			return err
		}
		return writeJSON(*output, payload)
	}
	if args[0] == "commit" {
		flags := newFlagSet("multiarch commit")
		document := flags.String("document", "", "signed multiarch completion document")
		privateKey := flags.String("private-key", "", "RSA channel signing key")
		output := flags.String("output", "-", "JSON result path or -")
		if err := parseFlags(flags, args[1:]); err != nil {
			return err
		}
		documentBytes, err := os.ReadFile(*document)
		if err != nil {
			return err
		}
		keyBytes, err := os.ReadFile(*privateKey)
		if err != nil {
			return err
		}
		key, err := control.ParsePrivateKey(keyBytes)
		if err != nil {
			return err
		}
		backend, err := openStore()
		if err != nil {
			return err
		}
		info, updated, err := control.CommitMultiarch(ctx, backend, documentBytes, &key.PublicKey)
		if err != nil {
			return err
		}
		return writeJSON(*output, map[string]any{"updated": updated, "key": info.Key, "sha256": info.SHA256})
	}
	if args[0] != "prepare" {
		return errors.New("usage: fsbuild multiarch <prepare|verify|commit|cycle-commit>")
	}
	flags := newFlagSet("multiarch prepare")
	evidencePath := flags.String("evidence", "", "same-run canary verification report")
	privateKeyPath := flags.String("private-key", "", "RSA channel signing key")
	output := flags.String("output", "", "local signed completion document")
	repositories, releases := map[string]*string{}, map[string]*string{}
	for _, arch := range []string{"amd64", "arm64"} {
		repositories[arch] = flags.String(arch+"-repositories", "", "exact signed architecture repository document")
		releases[arch] = flags.String(arch+"-release", "", "exact verified architecture release document")
	}
	if err := parseFlags(flags, args[1:]); err != nil {
		return err
	}
	if *output == "" || *output == "-" || *evidencePath == "" || *privateKeyPath == "" {
		return errors.New("evidence, private key and local output paths are required")
	}
	outputPath, err := filepath.Abs(*output)
	if err != nil {
		return err
	}
	outputInfo, _ := os.Stat(outputPath)
	inputs := []string{*evidencePath, *privateKeyPath, *repositories["amd64"], *repositories["arm64"], *releases["amd64"], *releases["arm64"]}
	for _, input := range inputs {
		inputPath, err := filepath.Abs(input)
		if err != nil {
			return err
		}
		inputInfo, _ := os.Stat(inputPath)
		if inputPath == outputPath || (inputInfo != nil && outputInfo != nil && os.SameFile(inputInfo, outputInfo)) {
			return errors.New("completion output must not overwrite an input or signing key")
		}
	}
	keyBytes, err := os.ReadFile(*privateKeyPath)
	if err != nil {
		return err
	}
	key, err := control.ParsePrivateKey(keyBytes)
	if err != nil {
		return err
	}
	raw, err := os.ReadFile(*evidencePath)
	if err != nil {
		return err
	}
	var evidence control.CanaryEvidence
	if err := json.Unmarshal(raw, &evidence); err != nil {
		return err
	}
	repositoryBytes, releaseBytes := map[string][]byte{}, map[string][]byte{}
	for _, arch := range []string{"amd64", "arm64"} {
		if repositoryBytes[arch], err = os.ReadFile(*repositories[arch]); err != nil {
			return err
		}
		if releaseBytes[arch], err = os.ReadFile(*releases[arch]); err != nil {
			return err
		}
	}
	completion, err := control.PrepareMultiarchCommit(evidence, repositoryBytes, releaseBytes, &key.PublicKey)
	if err != nil {
		return err
	}
	signed, err := control.MarshalMultiarchSigned(completion, key)
	if err != nil {
		return err
	}
	return os.WriteFile(*output, signed, 0o644)
}
