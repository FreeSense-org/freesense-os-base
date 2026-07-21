package store

import (
	"strings"
	"testing"
)

func TestOpenRequiresSessionTokenInGitHubActions(t *testing.T) {
	t.Setenv("GITHUB_ACTIONS", "true")
	t.Setenv("AWS_ACCESS_KEY_ID", "access")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "secret")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_ENDPOINT_URL_S3", "https://example.invalid")
	if _, err := Open("s3://bucket/prefix"); err == nil ||
		!strings.Contains(err.Error(), "AWS_SESSION_TOKEN is required") {
		t.Fatalf("missing session token error = %v", err)
	}

	t.Setenv("AWS_SESSION_TOKEN", "temporary-session")
	t.Setenv("FSBUILD_EXCLUSIVE_DELETE", "true")
	backend, err := Open("s3://bucket/prefix")
	if err != nil {
		t.Fatal(err)
	}
	s3, ok := backend.(*S3)
	if !ok {
		t.Fatalf("backend type = %T, want *S3", backend)
	}
	if s3.config.SessionToken != "temporary-session" {
		t.Fatalf("session token = %q", s3.config.SessionToken)
	}
	if !s3.config.ExclusiveDelete {
		t.Fatal("exclusive delete mode was not preserved")
	}

	t.Setenv("FSBUILD_EXCLUSIVE_DELETE", "yes")
	if _, err := Open("s3://bucket/prefix"); err == nil ||
		!strings.Contains(err.Error(), "must be exactly true") {
		t.Fatalf("invalid exclusive delete mode error = %v", err)
	}
}

func TestOpenExplicitTemporaryCredentialModeFailsClosed(t *testing.T) {
	t.Setenv("GITHUB_ACTIONS", "")
	t.Setenv("FSBUILD_REQUIRE_SESSION_TOKEN", "true")
	t.Setenv("AWS_ACCESS_KEY_ID", "access")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "secret")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_ENDPOINT_URL_S3", "https://example.invalid")
	if _, err := Open("s3://bucket/prefix"); err == nil ||
		!strings.Contains(err.Error(), "AWS_SESSION_TOKEN is required") {
		t.Fatalf("missing explicit-mode session token error = %v", err)
	}

	t.Setenv("FSBUILD_REQUIRE_SESSION_TOKEN", "not-a-boolean")
	if _, err := Open("s3://bucket/prefix"); err == nil ||
		!strings.Contains(err.Error(), "must be true or false") {
		t.Fatalf("invalid mode error = %v", err)
	}
}
