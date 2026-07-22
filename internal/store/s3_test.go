package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestS3PresignGetBindsOneObjectAndTemporarySession(t *testing.T) {
	backend, err := NewS3(S3Config{
		Endpoint: "https://account.r2.cloudflarestorage.com/root",
		Region:   "auto", Bucket: "bucket", Prefix: "v1",
		AccessKeyID: "temporary-access", SecretKey: "temporary-secret",
		SessionToken: "temporary-session",
	})
	if err != nil {
		t.Fatal(err)
	}
	backend.retry.now = func() time.Time {
		return time.Date(2026, 7, 21, 12, 34, 56, 0, time.UTC)
	}
	rawURL, err := backend.PresignGet(
		"inputs/sha256/"+strings.Repeat("a", 64), 20*time.Minute,
	)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Path != "/root/bucket/v1/inputs/sha256/"+strings.Repeat("a", 64) {
		t.Fatalf("presigned path = %q", parsed.Path)
	}
	query := parsed.Query()
	for name, expected := range map[string]string{
		"X-Amz-Algorithm":      "AWS4-HMAC-SHA256",
		"X-Amz-Credential":     "temporary-access/20260721/auto/s3/aws4_request",
		"X-Amz-Date":           "20260721T123456Z",
		"X-Amz-Expires":        "1200",
		"X-Amz-SignedHeaders":  "host",
		"X-Amz-Security-Token": "temporary-session",
	} {
		if query.Get(name) != expected {
			t.Fatalf("%s = %q, want %q", name, query.Get(name), expected)
		}
	}
	if len(query.Get("X-Amz-Signature")) != 64 {
		t.Fatalf("signature = %q", query.Get("X-Amz-Signature"))
	}
	if strings.Contains(rawURL, "temporary-secret") {
		t.Fatal("presigned URL exposed the secret key")
	}
}

func TestS3PresignGetRejectsUnsafeKeyAndLifetime(t *testing.T) {
	backend, err := NewS3(S3Config{
		Endpoint: "https://example.invalid", Region: "auto", Bucket: "bucket",
		AccessKeyID: "access", SecretKey: "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := backend.PresignGet("../escape", time.Minute); err == nil {
		t.Fatal("unsafe key was accepted")
	}
	if _, err := backend.PresignGet("object", 8*24*time.Hour); err == nil {
		t.Fatal("overlong bearer URL was accepted")
	}
}

func TestS3ArtifactReadsAllowMissingMetadataWithoutWeakeningStrictReads(t *testing.T) {
	data := []byte("rclone-created artifact")
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Length", strconv.Itoa(len(data)))
		writer.Header().Set("ETag", `"rclone-etag"`)
		switch request.Method {
		case http.MethodGet:
			_, _ = writer.Write(data)
		case http.MethodHead:
			writer.WriteHeader(http.StatusOK)
		default:
			writer.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket",
		AccessKeyID: "access", SecretKey: "secret", Client: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := backend.Get(ctx, "artifact"); err == nil || !strings.Contains(err.Error(), "no valid fsbuild SHA-256 metadata") {
		t.Fatalf("strict Get error = %v", err)
	}
	if _, err := backend.Head(ctx, "artifact"); err == nil || !strings.Contains(err.Error(), "no valid fsbuild SHA-256 metadata") {
		t.Fatalf("strict Head error = %v", err)
	}

	object, err := GetArtifact(ctx, backend, "artifact")
	if err != nil {
		t.Fatalf("GetArtifact() error = %v", err)
	}
	wantSHA256 := BytesContent(data).SHA256
	if string(object.Data) != string(data) || object.Size != int64(len(data)) || object.SHA256 != wantSHA256 {
		t.Fatalf("GetArtifact() = %#v, want size %d and SHA-256 %s", object, len(data), wantSHA256)
	}
	info, err := HeadArtifact(ctx, backend, "artifact")
	if err != nil {
		t.Fatalf("HeadArtifact() error = %v", err)
	}
	if info.Size != int64(len(data)) || info.SHA256 != "" {
		t.Fatalf("HeadArtifact() = %#v, want size %d and an unknown SHA-256", info, len(data))
	}
}

func TestS3ArtifactReadsRejectBadMetadataWhenPresent(t *testing.T) {
	data := []byte("artifact")
	tests := []struct {
		name      string
		metadata  string
		wantError string
	}{
		{name: "malformed", metadata: "not-a-sha256", wantError: "invalid fsbuild SHA-256 metadata"},
		{name: "mismatch", metadata: strings.Repeat("0", 64), wantError: "failed SHA-256 verification"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				writer.Header().Set("x-amz-meta-fsbuild-sha256", test.metadata)
				writer.Header().Set("Content-Length", strconv.Itoa(len(data)))
				if request.Method == http.MethodGet {
					_, _ = writer.Write(data)
					return
				}
				writer.WriteHeader(http.StatusOK)
			}))
			defer server.Close()

			backend, err := NewS3(S3Config{
				Endpoint: server.URL, Region: "auto", Bucket: "bucket",
				AccessKeyID: "access", SecretKey: "secret", Client: server.Client(),
			})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := GetArtifact(context.Background(), backend, "artifact"); err == nil || !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("GetArtifact() error = %v, want substring %q", err, test.wantError)
			}
			if test.name == "malformed" {
				if _, err := HeadArtifact(context.Background(), backend, "artifact"); err == nil || !strings.Contains(err.Error(), test.wantError) {
					t.Fatalf("HeadArtifact() error = %v, want substring %q", err, test.wantError)
				}
			}
		})
	}
}

func TestS3ConditionalContract(t *testing.T) {
	var mutex sync.Mutex
	var data []byte
	var currentETag string
	var currentSHA256 string
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") == "" ||
			request.Header.Get("x-amz-content-sha256") == "" ||
			request.Header.Get("x-amz-date") == "" ||
			request.Header.Get("x-amz-security-token") != "temporary-session" ||
			!strings.Contains(
				request.Header.Get("Authorization"),
				"x-amz-security-token",
			) {
			http.Error(writer, "unsigned", http.StatusUnauthorized)
			return
		}
		if request.URL.Path != "/bucket/prefix/object" {
			http.Error(writer, "wrong path", http.StatusBadRequest)
			return
		}
		mutex.Lock()
		defer mutex.Unlock()
		switch request.Method {
		case http.MethodPut:
			if request.Header.Get("If-None-Match") == "*" && data != nil {
				writer.WriteHeader(http.StatusPreconditionFailed)
				return
			}
			if expected := normalizeETag(request.Header.Get("If-Match")); expected != "" && expected != currentETag {
				writer.WriteHeader(http.StatusPreconditionFailed)
				return
			}
			body, err := io.ReadAll(request.Body)
			if err != nil {
				t.Errorf("read request: %v", err)
				writer.WriteHeader(http.StatusInternalServerError)
				return
			}
			sum := sha256.Sum256(body)
			data = body
			currentETag = hex.EncodeToString(sum[:])
			currentSHA256 = request.Header.Get("x-amz-meta-fsbuild-sha256")
			if currentSHA256 != currentETag {
				http.Error(writer, "wrong SHA-256 metadata", http.StatusBadRequest)
				return
			}
			writer.Header().Set("ETag", `"`+currentETag+`"`)
			writer.WriteHeader(http.StatusOK)
		case http.MethodHead:
			if data == nil {
				writer.WriteHeader(http.StatusNotFound)
				return
			}
			writer.Header().Set("Content-Length", strconv.Itoa(len(data)))
			writer.Header().Set("ETag", `"`+currentETag+`"`)
			writer.Header().Set("x-amz-meta-fsbuild-sha256", currentSHA256)
			writer.WriteHeader(http.StatusOK)
		case http.MethodGet:
			if data == nil {
				writer.WriteHeader(http.StatusNotFound)
				return
			}
			writer.Header().Set("ETag", `"`+currentETag+`"`)
			writer.Header().Set("x-amz-meta-fsbuild-sha256", currentSHA256)
			_, _ = writer.Write(data)
		default:
			writer.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket", Prefix: "prefix",
		AccessKeyID: "access", SecretKey: "secret",
		SessionToken: "temporary-session",
		Client:       server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	first := BytesContent([]byte("first"))
	info, created, err := backend.PutIfAbsent(ctx, "object", first)
	if err != nil || !created {
		t.Fatalf("PutIfAbsent first created=%v err=%v", created, err)
	}
	object, err := backend.Get(ctx, "object")
	if err != nil {
		t.Fatal(err)
	}
	if string(object.Data) != "first" || object.SHA256 != first.SHA256 {
		t.Fatalf("verified S3 object = %#v", object)
	}
	if _, created, err := backend.PutIfAbsent(ctx, "object", BytesContent([]byte("ignored"))); err != nil || created {
		t.Fatalf("PutIfAbsent retry created=%v err=%v", created, err)
	}
	second := BytesContent([]byte("second"))
	_, err = backend.CompareAndSwap(ctx, "object", info.ETag, second)
	if err != nil {
		t.Fatal(err)
	}
	if object, err := backend.Get(ctx, "object"); err != nil ||
		string(object.Data) != "second" || object.SHA256 != second.SHA256 {
		t.Fatalf("object after CompareAndSwap = %#v, err=%v", object, err)
	}
}

func TestS3ErrorIncludesBoundedXMLCodeAndMessage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		_ *http.Request,
	) {
		writer.Header().Set("Content-Type", "application/xml")
		writer.WriteHeader(http.StatusBadRequest)
		_, _ = io.WriteString(writer, `<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>InvalidArgument</Code>
  <Message>  request   shape is unsupported  </Message>
</Error>`)
	}))
	defer server.Close()

	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket", Prefix: "prefix",
		AccessKeyID: "access", SecretKey: "secret", Client: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = backend.PutIfAbsent(
		context.Background(),
		"object",
		BytesContent([]byte("payload")),
	)
	if err == nil ||
		!strings.Contains(
			err.Error(),
			"400 Bad Request (InvalidArgument: request shape is unsupported)",
		) {
		t.Fatalf("S3 error = %v", err)
	}
}

func TestS3RejectsObjectsAboveR2AtomicPutLimitBeforeOpeningBody(t *testing.T) {
	backend, err := NewS3(S3Config{
		Endpoint: "https://example.invalid", Region: "auto", Bucket: "bucket",
		AccessKeyID: "access", SecretKey: "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	opened := false
	content := Content{
		Size:   maxR2ConditionalPutSize + 1,
		SHA256: strings.Repeat("0", sha256.Size*2),
		Open: func() (io.ReadCloser, error) {
			opened = true
			return io.NopCloser(strings.NewReader("")), nil
		},
	}
	if _, _, err := backend.PutIfAbsent(
		context.Background(), "oversized", content,
	); err == nil || !strings.Contains(err.Error(), "atomic single-part PUT") {
		t.Fatalf("PutIfAbsent oversized error = %v", err)
	}
	if _, err := backend.CompareAndSwap(
		context.Background(), "oversized", "etag", content,
	); err == nil || !strings.Contains(err.Error(), "atomic single-part PUT") {
		t.Fatalf("CompareAndSwap oversized error = %v", err)
	}
	if opened {
		t.Fatal("oversized content body was opened")
	}
}
