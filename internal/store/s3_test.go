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
		case http.MethodDelete:
			if request.Header.Get("If-Match") != "" {
				http.Error(writer, "conditional delete is not supported by R2", http.StatusBadRequest)
				return
			}
			data = nil
			currentETag = ""
			currentSHA256 = ""
			writer.WriteHeader(http.StatusNoContent)
		default:
			writer.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket", Prefix: "prefix",
		AccessKeyID: "access", SecretKey: "secret",
		SessionToken: "temporary-session", ExclusiveDelete: true,
		Client: server.Client(),
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
	updated, err := backend.CompareAndSwap(ctx, "object", info.ETag, BytesContent([]byte("second")))
	if err != nil {
		t.Fatal(err)
	}
	if err := backend.DeleteIfMatch(ctx, "object", info.ETag); err != ErrPrecondition {
		t.Fatalf("stale DeleteIfMatch error = %v", err)
	}
	if object, err := backend.Get(ctx, "object"); err != nil ||
		string(object.Data) != "second" {
		t.Fatalf("stale delete changed object = %#v, err=%v", object, err)
	}
	if err := backend.DeleteIfMatch(ctx, "object", updated.ETag); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Head(ctx, "object"); err != ErrNotFound {
		t.Fatalf("Head after delete error = %v", err)
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

func TestS3DeleteRequiresExclusiveWorkflowLease(t *testing.T) {
	backend, err := NewS3(S3Config{
		Endpoint: "https://example.invalid", Region: "auto", Bucket: "bucket",
		AccessKeyID: "access", SecretKey: "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	err = backend.DeleteIfMatch(context.Background(), "object", "etag")
	if err == nil || !strings.Contains(err.Error(), "exclusive storage-maintenance lease") {
		t.Fatalf("delete without exclusive lease error = %v", err)
	}
}

func TestS3ListUsesBucketRootAndStripsConfiguredPrefix(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") == "" {
			http.Error(writer, "unsigned", http.StatusUnauthorized)
			return
		}
		if request.Method != http.MethodGet || request.URL.Path != "/bucket" {
			http.Error(writer, "wrong list request path", http.StatusBadRequest)
			return
		}
		if request.URL.Query().Get("list-type") != "2" ||
			request.URL.Query().Get("prefix") != "prefix/cas/" {
			http.Error(writer, "wrong list query", http.StatusBadRequest)
			return
		}
		writer.Header().Set("Content-Type", "application/xml")
		_, _ = io.WriteString(writer, `<ListBucketResult>
<IsTruncated>false</IsTruncated>
<Contents>
<Key>prefix/cas/object</Key>
<LastModified>2026-07-17T12:00:00Z</LastModified>
<ETag>"abc123"</ETag>
<Size>5</Size>
</Contents>
</ListBucketResult>`)
	}))
	defer server.Close()
	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket", Prefix: "prefix",
		AccessKeyID: "access", SecretKey: "secret", Client: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	objects, err := backend.List(context.Background(), "cas/")
	if err != nil {
		t.Fatal(err)
	}
	if len(objects) != 1 || objects[0].Key != "cas/object" ||
		objects[0].ETag != "abc123" || objects[0].Size != 5 {
		t.Fatalf("list objects = %#v", objects)
	}
}

func TestS3RootListPreservesConfiguredNamespaceBoundary(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet ||
			request.URL.Path != "/bucket" ||
			request.URL.Query().Get("list-type") != "2" ||
			request.URL.Query().Get("prefix") != "v1/" {
			http.Error(writer, "root list escaped configured namespace", http.StatusBadRequest)
			return
		}
		writer.Header().Set("Content-Type", "application/xml")
		_, _ = io.WriteString(writer, `<ListBucketResult>
<IsTruncated>false</IsTruncated>
<Contents>
<Key>v1/smoke/object</Key>
<LastModified>2026-07-17T12:00:00Z</LastModified>
<ETag>"abc123"</ETag>
<Size>5</Size>
</Contents>
</ListBucketResult>`)
	}))
	defer server.Close()
	backend, err := NewS3(S3Config{
		Endpoint: server.URL, Region: "auto", Bucket: "bucket",
		Prefix: "v1", AccessKeyID: "access",
		SecretKey: "secret", Client: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	objects, err := backend.List(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if len(objects) != 1 || objects[0].Key != "smoke/object" {
		t.Fatalf("root list objects = %#v", objects)
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
