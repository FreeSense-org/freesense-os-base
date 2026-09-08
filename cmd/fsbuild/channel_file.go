package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/FreeSense-org/freesense-os-base/internal/control"
)

func commandSignChannelFile(args []string) error {
	flags := newFlagSet("channel sign-file")
	payloadPath := flags.String("payload", "", "local unsigned channel payload")
	privateKeyPath := flags.String("private-key", "", "RSA channel signing key")
	architecture := flags.String("architecture", "", "product architecture")
	packageArch := flags.String("package-arch", "", "pkg repository architecture")
	outputPath := flags.String("output", "", "local signed manifest")
	if err := parseFlags(flags, args); err != nil {
		return err
	}
	if *payloadPath == "" || *privateKeyPath == "" || *outputPath == "" || *outputPath == "-" {
		return errors.New("payload, private key, architecture, package arch and local output are required")
	}
	output, err := filepath.Abs(*outputPath)
	if err != nil {
		return err
	}
	for _, input := range []string{*payloadPath, *privateKeyPath} {
		absolute, err := filepath.Abs(input)
		if err != nil {
			return err
		}
		if absolute == output {
			return errors.New("signed manifest output must not overwrite an input")
		}
	}
	raw, err := os.ReadFile(*payloadPath)
	if err != nil {
		return err
	}
	var payload control.Payload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return fmt.Errorf("parse channel payload: %w", err)
	}
	if err := control.ValidateDevelopmentPair(payload, *architecture, *packageArch); err != nil {
		return err
	}
	keyData, err := os.ReadFile(*privateKeyPath)
	if err != nil {
		return err
	}
	key, err := control.ParsePrivateKey(keyData)
	if err != nil {
		return err
	}
	signed, err := control.MarshalSigned(payload, key)
	if err != nil {
		return err
	}
	return os.WriteFile(output, signed, 0o644)
}
