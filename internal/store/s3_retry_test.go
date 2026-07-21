package store

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestS3RetriesTransientResponseWithFreshSignedBody(t *testing.T) {
	var mutex sync.Mutex
	var bodies []string
	var dates []string
	var authorizations []string
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.Method == http.MethodHead {
			writer.WriteHeader(http.StatusNotFound)
			return
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			http.Error(writer, err.Error(), http.StatusInternalServerError)
			return
		}
		mutex.Lock()
		bodies = append(bodies, string(body))
		dates = append(dates, request.Header.Get("x-amz-date"))
		authorizations = append(
			authorizations,
			request.Header.Get("Authorization"),
		)
		attempt := len(bodies)
		mutex.Unlock()
		if attempt == 1 {
			http.Error(writer, "temporary gateway failure", http.StatusBadGateway)
			return
		}
		writer.Header().Set("ETag", `"created"`)
		writer.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	backend := newRetryTestS3(t, server.Client(), server.URL)
	var nowCalls int
	backend.retry.now = func() time.Time {
		now := time.Date(2026, 7, 19, 21, 0, nowCalls, 0, time.UTC)
		nowCalls++
		return now
	}
	var delays []time.Duration
	backend.retry.sleep = func(_ context.Context, delay time.Duration) error {
		delays = append(delays, delay)
		return nil
	}
	content := BytesContent([]byte("complete replayable payload"))
	originalOpen := content.Open
	openCount := 0
	content.Open = func() (io.ReadCloser, error) {
		openCount++
		return originalOpen()
	}

	info, created, err := backend.PutIfAbsent(
		context.Background(),
		"object",
		content,
	)
	if err != nil || !created {
		t.Fatalf("PutIfAbsent() created=%v info=%+v err=%v", created, info, err)
	}
	mutex.Lock()
	defer mutex.Unlock()
	if openCount != 2 {
		t.Fatalf("content open count = %d, want 2", openCount)
	}
	if len(bodies) != 2 ||
		bodies[0] != "complete replayable payload" ||
		bodies[1] != bodies[0] {
		t.Fatalf("request bodies = %#v", bodies)
	}
	if len(dates) != 2 || dates[0] == dates[1] ||
		len(authorizations) != 2 || authorizations[0] == authorizations[1] {
		t.Fatalf(
			"request signing was not rebuilt: dates=%#v authorizations=%#v",
			dates,
			authorizations,
		)
	}
	if len(delays) != 1 || delays[0] != defaultS3RetryDelay {
		t.Fatalf("retry delays = %v", delays)
	}
}

func TestS3RetriesTransportFailure(t *testing.T) {
	attempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		if attempts == 1 {
			return nil, errors.New("temporary connection reset")
		}
		return s3TestResponse(
			request,
			http.StatusOK,
			http.Header{
				"ETag":                      {`"etag"`},
				"Content-Length":            {"7"},
				"x-amz-meta-fsbuild-sha256": {strings.Repeat("a", 64)},
			},
			"",
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	backend.retry.sleep = noWaitS3Retry

	info, err := backend.Head(context.Background(), "object")
	if err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || info.ETag != "etag" || info.Size != 7 {
		t.Fatalf("Head() attempts=%d info=%+v", attempts, info)
	}
}

func TestS3HonorsRetryAfterForRateLimit(t *testing.T) {
	attempts := 0
	retryAt := time.Date(2026, 7, 19, 21, 0, 3, 0, time.UTC)
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		if attempts == 1 {
			return s3TestResponse(
				request,
				http.StatusTooManyRequests,
				http.Header{"Retry-After": {retryAt.Format(http.TimeFormat)}},
				"slow down",
			), nil
		}
		return s3TestResponse(
			request,
			http.StatusOK,
			http.Header{
				"ETag":                      {`"etag"`},
				"Content-Length":            {"0"},
				"x-amz-meta-fsbuild-sha256": {strings.Repeat("b", 64)},
			},
			"",
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	nowCalls := 0
	backend.retry.now = func() time.Time {
		nowCalls++
		switch nowCalls {
		case 1:
			// The request may have started long before the 429 arrived.
			return time.Date(2026, 7, 19, 20, 0, 0, 0, time.UTC)
		case 2:
			// Retry-After must be evaluated against this decision time.
			return time.Date(2026, 7, 19, 21, 0, 0, 0, time.UTC)
		default:
			return time.Date(2026, 7, 19, 21, 0, 3, 0, time.UTC)
		}
	}
	var delays []time.Duration
	backend.retry.sleep = func(_ context.Context, delay time.Duration) error {
		delays = append(delays, delay)
		return nil
	}

	if _, err := backend.Head(context.Background(), "object"); err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || len(delays) != 1 || delays[0] != 3*time.Second {
		t.Fatalf("attempts=%d retry delays=%v", attempts, delays)
	}
}

func TestS3DoesNotRetryTransientCompareAndSwapResponse(t *testing.T) {
	attempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		return s3TestResponse(
			request,
			http.StatusBadGateway,
			nil,
			"ambiguous response after possible commit",
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	sleepCalls := 0
	backend.retry.sleep = func(_ context.Context, _ time.Duration) error {
		sleepCalls++
		return nil
	}

	_, err := backend.CompareAndSwap(
		context.Background(),
		"object",
		"expected-etag",
		BytesContent([]byte("replacement")),
	)
	if err == nil || !strings.Contains(err.Error(), "502 Bad Gateway") {
		t.Fatalf("CompareAndSwap() error = %v", err)
	}
	if attempts != 1 || sleepCalls != 0 {
		t.Fatalf("attempts=%d sleep calls=%d", attempts, sleepCalls)
	}
}

func TestS3DoesNotRetryPermanentClientError(t *testing.T) {
	attempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		return s3TestResponse(
			request,
			http.StatusBadRequest,
			http.Header{"Content-Type": {"application/xml"}},
			`<Error><Code>InvalidArgument</Code><Message>permanent</Message></Error>`,
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	sleepCalls := 0
	backend.retry.sleep = func(_ context.Context, _ time.Duration) error {
		sleepCalls++
		return nil
	}

	_, _, err := backend.PutIfAbsent(
		context.Background(),
		"object",
		BytesContent([]byte("payload")),
	)
	if err == nil || !strings.Contains(err.Error(), "400 Bad Request") {
		t.Fatalf("PutIfAbsent() error = %v", err)
	}
	if attempts != 1 || sleepCalls != 0 {
		t.Fatalf("attempts=%d sleep calls=%d", attempts, sleepCalls)
	}
}

func TestS3RetryWaitHonorsContextCancellation(t *testing.T) {
	attempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		return s3TestResponse(
			request,
			http.StatusServiceUnavailable,
			nil,
			"retry later",
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	ctx, cancel := context.WithCancel(context.Background())
	backend.retry.sleep = func(waitCtx context.Context, _ time.Duration) error {
		cancel()
		<-waitCtx.Done()
		return waitCtx.Err()
	}

	_, err := backend.Head(ctx, "object")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Head() error = %v, want context canceled", err)
	}
	if attempts != 1 {
		t.Fatalf("request attempts = %d, want 1", attempts)
	}
}

func TestS3AmbiguousPutRetryResolvesThroughHeadWithoutDuplicate(t *testing.T) {
	var stored []byte
	putAttempts := 0
	headAttempts := 0
	writes := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		switch request.Method {
		case http.MethodPut:
			putAttempts++
			body, err := io.ReadAll(request.Body)
			if err != nil {
				return nil, err
			}
			if stored == nil {
				stored = append([]byte(nil), body...)
				writes++
				return s3TestResponse(
					request,
					http.StatusBadGateway,
					nil,
					"response lost after committed PUT",
				), nil
			}
			return s3TestResponse(
				request,
				http.StatusPreconditionFailed,
				nil,
				"already exists",
			), nil
		case http.MethodHead:
			headAttempts++
			return s3TestResponse(
				request,
				http.StatusOK,
				http.Header{
					"ETag":                      {`"stored-etag"`},
					"Content-Length":            {strconv.Itoa(len(stored))},
					"x-amz-meta-fsbuild-sha256": {BytesContent(stored).SHA256},
				},
				"",
			), nil
		default:
			return s3TestResponse(
				request,
				http.StatusMethodNotAllowed,
				nil,
				"",
			), nil
		}
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	backend.retry.sleep = noWaitS3Retry
	content := BytesContent([]byte("publish exactly once"))
	originalOpen := content.Open
	openCount := 0
	content.Open = func() (io.ReadCloser, error) {
		openCount++
		return originalOpen()
	}

	info, created, err := backend.PutIfAbsent(
		context.Background(),
		"object",
		content,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !created {
		t.Fatal("exact ambiguous committed PUT was not reconciled as success")
	}
	if putAttempts != 1 || headAttempts != 1 || writes != 1 || openCount != 1 {
		t.Fatalf(
			"put attempts=%d head attempts=%d writes=%d opens=%d",
			putAttempts,
			headAttempts,
			writes,
			openCount,
		)
	}
	if string(stored) != "publish exactly once" ||
		info.ETag != "stored-etag" ||
		info.SHA256 != content.SHA256 {
		t.Fatalf("stored=%q info=%+v", stored, info)
	}
}

func TestS3AmbiguousPutThenPreconditionReconcilesExactObject(t *testing.T) {
	content := BytesContent([]byte("committed before response was lost"))
	putAttempts := 0
	headAttempts := 0
	writes := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		switch request.Method {
		case http.MethodPut:
			putAttempts++
			_, _ = io.Copy(io.Discard, request.Body)
			if putAttempts == 1 {
				writes++
				return s3TestResponse(
					request,
					http.StatusBadGateway,
					nil,
					"response lost after commit",
				), nil
			}
			return s3TestResponse(
				request,
				http.StatusPreconditionFailed,
				nil,
				"already exists",
			), nil
		case http.MethodHead:
			headAttempts++
			if headAttempts <= defaultS3RetryAttempts {
				return s3TestResponse(
					request,
					http.StatusServiceUnavailable,
					nil,
					"probe temporarily unavailable",
				), nil
			}
			return s3TestResponse(
				request,
				http.StatusOK,
				http.Header{
					"ETag":                      {`"committed-etag"`},
					"Content-Length":            {strconv.FormatInt(content.Size, 10)},
					"x-amz-meta-fsbuild-sha256": {content.SHA256},
				},
				"",
			), nil
		default:
			return s3TestResponse(
				request,
				http.StatusMethodNotAllowed,
				nil,
				"",
			), nil
		}
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	backend.retry.sleep = noWaitS3Retry
	originalOpen := content.Open
	openCount := 0
	content.Open = func() (io.ReadCloser, error) {
		openCount++
		return originalOpen()
	}

	info, created, err := backend.PutIfAbsent(
		context.Background(),
		"object",
		content,
	)
	if err != nil || !created {
		t.Fatalf("PutIfAbsent() created=%v info=%+v err=%v", created, info, err)
	}
	if putAttempts != 2 ||
		headAttempts != defaultS3RetryAttempts+1 ||
		writes != 1 ||
		openCount != 2 {
		t.Fatalf(
			"put attempts=%d head attempts=%d writes=%d opens=%d",
			putAttempts,
			headAttempts,
			writes,
			openCount,
		)
	}
	if info.ETag != "committed-etag" || info.SHA256 != content.SHA256 {
		t.Fatalf("reconciled info = %+v", info)
	}
}

func TestS3AmbiguousPutWithDifferentExistingObjectIsNotCreated(t *testing.T) {
	existing := BytesContent([]byte("published by another writer"))
	putAttempts := 0
	headAttempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		switch request.Method {
		case http.MethodPut:
			putAttempts++
			_, _ = io.Copy(io.Discard, request.Body)
			return s3TestResponse(
				request,
				http.StatusBadGateway,
				nil,
				"ambiguous conditional create",
			), nil
		case http.MethodHead:
			headAttempts++
			return s3TestResponse(
				request,
				http.StatusOK,
				http.Header{
					"ETag":                      {`"other-etag"`},
					"Content-Length":            {strconv.FormatInt(existing.Size, 10)},
					"x-amz-meta-fsbuild-sha256": {existing.SHA256},
				},
				"",
			), nil
		default:
			return s3TestResponse(
				request,
				http.StatusMethodNotAllowed,
				nil,
				"",
			), nil
		}
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	backend.retry.sleep = noWaitS3Retry

	info, created, err := backend.PutIfAbsent(
		context.Background(),
		"object",
		BytesContent([]byte("our candidate")),
	)
	if err != nil {
		t.Fatal(err)
	}
	if created {
		t.Fatal("different existing object was reported as created")
	}
	if putAttempts != 1 || headAttempts != 2 {
		t.Fatalf("put attempts=%d head attempts=%d", putAttempts, headAttempts)
	}
	if info.ETag != "other-etag" || info.SHA256 != existing.SHA256 {
		t.Fatalf("existing object info = %+v", info)
	}
}

func TestRetryableS3Status(t *testing.T) {
	for _, status := range []int{
		http.StatusRequestTimeout,
		http.StatusTooEarly,
		http.StatusTooManyRequests,
		http.StatusInternalServerError,
		http.StatusBadGateway,
		http.StatusServiceUnavailable,
		http.StatusGatewayTimeout,
		520,
		521,
		522,
		523,
		524,
	} {
		if !retryableS3Status(http.MethodGet, "", status) {
			t.Errorf("retryableS3Status(%d) = false", status)
		}
	}
	for _, status := range []int{
		http.StatusBadRequest,
		http.StatusUnauthorized,
		http.StatusForbidden,
		http.StatusNotFound,
		http.StatusPreconditionFailed,
		519,
		525,
	} {
		if retryableS3Status(http.MethodGet, "", status) {
			t.Errorf("retryableS3Status(%d) = true", status)
		}
	}
	if !retryableS3Status(http.MethodPut, "*", http.StatusConflict) {
		t.Error("conditional create 409 is not retryable")
	}
	if retryableS3Status(
		http.MethodPut,
		`"expected-etag"`,
		http.StatusConflict,
	) || retryableS3Status(http.MethodGet, "", http.StatusConflict) {
		t.Error("409 is retryable outside conditional create")
	}
}

func TestS3RetryResponseDrainIsBoundedAndClosed(t *testing.T) {
	body := &countingRetryBody{}
	attempts := 0
	client := &http.Client{Transport: roundTripFunc(func(
		request *http.Request,
	) (*http.Response, error) {
		attempts++
		if attempts == 1 {
			return &http.Response{
				StatusCode: http.StatusBadGateway,
				Status:     "502 Bad Gateway",
				Header:     make(http.Header),
				Body:       body,
				Request:    request,
			}, nil
		}
		return s3TestResponse(
			request,
			http.StatusOK,
			http.Header{
				"ETag":                      {`"etag"`},
				"Content-Length":            {"0"},
				"x-amz-meta-fsbuild-sha256": {strings.Repeat("c", 64)},
			},
			"",
		), nil
	})}
	backend := newRetryTestS3(t, client, "https://example.invalid")
	backend.retry.sleep = noWaitS3Retry

	if _, err := backend.Head(context.Background(), "object"); err != nil {
		t.Fatal(err)
	}
	if !body.closed ||
		body.bytesRead == 0 ||
		body.bytesRead > maxS3RetryDrainSize {
		t.Fatalf(
			"retry body closed=%v bytes read=%d, maximum %d",
			body.closed,
			body.bytesRead,
			maxS3RetryDrainSize,
		)
	}
}

func newRetryTestS3(t *testing.T, client *http.Client, endpoint string) *S3 {
	t.Helper()
	backend, err := NewS3(S3Config{
		Endpoint:    endpoint,
		Region:      "auto",
		Bucket:      "bucket",
		Prefix:      "prefix",
		AccessKeyID: "access",
		SecretKey:   "secret",
		Client:      client,
	})
	if err != nil {
		t.Fatal(err)
	}
	return backend
}

func noWaitS3Retry(_ context.Context, _ time.Duration) error {
	return nil
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(
	request *http.Request,
) (*http.Response, error) {
	return function(request)
}

func s3TestResponse(
	request *http.Request,
	statusCode int,
	header http.Header,
	body string,
) *http.Response {
	canonicalHeader := make(http.Header)
	for name, values := range header {
		for _, value := range values {
			canonicalHeader.Add(name, value)
		}
	}
	contentLength := int64(len(body))
	if value := canonicalHeader.Get("Content-Length"); value != "" {
		if parsed, err := strconv.ParseInt(value, 10, 64); err == nil {
			contentLength = parsed
		}
	}
	return &http.Response{
		StatusCode:    statusCode,
		Status:        strconv.Itoa(statusCode) + " " + http.StatusText(statusCode),
		Header:        canonicalHeader,
		Body:          io.NopCloser(strings.NewReader(body)),
		ContentLength: contentLength,
		Request:       request,
	}
}

type countingRetryBody struct {
	bytesRead int64
	closed    bool
}

func (body *countingRetryBody) Read(buffer []byte) (int, error) {
	for index := range buffer {
		buffer[index] = 'x'
	}
	body.bytesRead += int64(len(buffer))
	return len(buffer), nil
}

func (body *countingRetryBody) Close() error {
	body.closed = true
	return nil
}
