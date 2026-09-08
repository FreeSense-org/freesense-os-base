package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMultiarchPreparationCannotOverwriteItsSigningKey(t *testing.T) {
	key := filepath.Join(t.TempDir(), "private.pem")
	const sentinel = "existing private material must remain unchanged"
	if err := os.WriteFile(key, []byte(sentinel), 0o600); err != nil {
		t.Fatal(err)
	}
	err := commandMultiarch(context.Background(), []string{"prepare", "--private-key", key, "--output", key, "--evidence", "evidence.json"})
	if err == nil || !strings.Contains(err.Error(), "overwrite") {
		t.Fatalf("unexpected result: %v", err)
	}
	data, err := os.ReadFile(key)
	if err != nil || string(data) != sentinel {
		t.Fatal("preparation changed its signing input")
	}
}
