package store

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Open constructs a backend from file:// or s3://bucket/prefix. S3-compatible
// endpoints (including R2) use FSBUILD_S3_ENDPOINT and the standard AWS
// credential environment variables.
func Open(rawURL string) (Backend, error) {
	if rawURL == "" {
		rawURL = os.Getenv("FSBUILD_STORE_URL")
	}
	if rawURL == "" {
		return nil, errors.New("store URL is required (flag or FSBUILD_STORE_URL)")
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("parse store URL: %w", err)
	}
	switch parsed.Scheme {
	case "file":
		root := parsed.Path
		if parsed.Host != "" {
			root = "//" + parsed.Host + parsed.Path
		}
		if len(root) >= 3 && root[0] == '/' && root[2] == ':' {
			root = root[1:]
		}
		root, err = url.PathUnescape(root)
		if err != nil {
			return nil, fmt.Errorf("unescape local store path: %w", err)
		}
		return NewLocal(filepath.FromSlash(root))
	case "s3":
		if parsed.Host == "" {
			return nil, errors.New("s3 store URL requires a bucket")
		}
		sessionToken := os.Getenv("AWS_SESSION_TOKEN")
		requireSessionToken, err := sessionTokenRequired()
		if err != nil {
			return nil, err
		}
		if requireSessionToken && sessionToken == "" {
			return nil, errors.New(
				"AWS_SESSION_TOKEN is required for S3 access in GitHub Actions and temporary-credential mode",
			)
		}
		exclusiveDelete := os.Getenv("FSBUILD_EXCLUSIVE_DELETE")
		if exclusiveDelete != "" && exclusiveDelete != "true" {
			return nil, errors.New("FSBUILD_EXCLUSIVE_DELETE must be exactly true when set")
		}
		return NewS3(S3Config{
			Endpoint:        firstEnvironment("FSBUILD_S3_ENDPOINT", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"),
			Region:          firstEnvironmentDefault("auto", "FSBUILD_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
			Bucket:          parsed.Host,
			Prefix:          strings.Trim(parsed.Path, "/"),
			AccessKeyID:     os.Getenv("AWS_ACCESS_KEY_ID"),
			SecretKey:       os.Getenv("AWS_SECRET_ACCESS_KEY"),
			SessionToken:    sessionToken,
			ExclusiveDelete: exclusiveDelete == "true",
		})
	default:
		return nil, fmt.Errorf("unsupported store URL scheme %q", parsed.Scheme)
	}
}

func sessionTokenRequired() (bool, error) {
	if os.Getenv("GITHUB_ACTIONS") == "true" {
		return true, nil
	}
	value := strings.TrimSpace(os.Getenv("FSBUILD_REQUIRE_SESSION_TOKEN"))
	if value == "" {
		return false, nil
	}
	required, err := strconv.ParseBool(value)
	if err != nil {
		return false, errors.New("FSBUILD_REQUIRE_SESSION_TOKEN must be true or false")
	}
	return required, nil
}

func firstEnvironment(names ...string) string {
	for _, name := range names {
		if value := os.Getenv(name); value != "" {
			return value
		}
	}
	return ""
}

func firstEnvironmentDefault(fallback string, names ...string) string {
	if value := firstEnvironment(names...); value != "" {
		return value
	}
	return fallback
}
