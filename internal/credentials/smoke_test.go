package credentials

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

func TestSmokePublishesOneDeterministicMarkerAcrossRetries(t *testing.T) {
	backend, err := store.NewLocal(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	options := SmokeOptions{
		Backend:       backend,
		DeploymentSHA: "0123456789abcdef0123456789abcdef01234567",
	}
	first, err := Smoke(context.Background(), options)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Created {
		t.Fatal("first smoke marker was not created")
	}
	second, err := Smoke(context.Background(), options)
	if err != nil {
		t.Fatal(err)
	}
	if second.Created {
		t.Fatal("retry created a duplicate smoke marker")
	}
	if second.Key != first.Key || second.SHA256 != first.SHA256 {
		t.Fatalf("retry result = %+v, want identity %+v", second, first)
	}
}

func TestSmokeRejectsConflictingExistingMarker(t *testing.T) {
	root := t.TempDir()
	backend, err := store.NewLocal(root)
	if err != nil {
		t.Fatal(err)
	}
	key := filepath.Join(
		root,
		"smoke",
		"broker",
		"0123456789abcdef0123456789abcdef01234567.json",
	)
	if err := os.MkdirAll(filepath.Dir(key), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(key, []byte("conflict\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = Smoke(context.Background(), SmokeOptions{
		Backend:       backend,
		DeploymentSHA: "0123456789abcdef0123456789abcdef01234567",
	})
	if err == nil {
		t.Fatal("conflicting marker unexpectedly passed")
	}
}

func TestSmokeRejectsUntrustedIdentityInputs(t *testing.T) {
	backend, err := store.NewLocal(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, options := range []SmokeOptions{
		{Backend: backend, DeploymentSHA: "../main"},
		{Backend: backend, DeploymentSHA: "main"},
	} {
		if _, err := Smoke(context.Background(), options); err == nil {
			t.Fatalf("invalid options unexpectedly passed: %+v", options)
		}
	}
}
