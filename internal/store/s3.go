package store

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"
)

type S3Config struct {
	Endpoint        string
	Region          string
	Bucket          string
	Prefix          string
	AccessKeyID     string
	SecretKey       string
	SessionToken    string
	ExclusiveDelete bool
	Client          *http.Client
}

// Cloudflare R2 keeps conditional single-part PUT atomic but caps each request
// at 5 GiB minus 5 MiB. fsbuild deliberately rejects larger objects instead of
// silently switching to multipart, whose final publication would need a
// different exactly-once protocol.
const maxR2ConditionalPutSize = int64(5*1024*1024*1024 - 5*1024*1024)
const maxS3ErrorBodySize = int64(16 * 1024)
const maxS3ErrorFieldRunes = 256
const maxS3RetryDrainSize = int64(64 * 1024)

const (
	defaultS3RetryAttempts = 3
	defaultS3RetryDelay    = time.Second
	defaultS3RetryMaxDelay = 30 * time.Second
)

type s3RetryPolicy struct {
	attempts int
	delay    time.Duration
	maxDelay time.Duration
	now      func() time.Time
	sleep    func(context.Context, time.Duration) error
}

// S3 implements the subset of the S3 API needed by fsbuild using SigV4 and
// path-style requests. It is compatible with Cloudflare R2.
type S3 struct {
	config   S3Config
	endpoint *url.URL
	client   *http.Client
	retry    s3RetryPolicy
}

func NewS3(config S3Config) (*S3, error) {
	if config.Endpoint == "" || config.Bucket == "" || config.AccessKeyID == "" || config.SecretKey == "" {
		return nil, errors.New("S3 endpoint, bucket, access key, and secret key are required")
	}
	endpoint, err := url.Parse(strings.TrimRight(config.Endpoint, "/"))
	if err != nil || endpoint.Scheme == "" || endpoint.Host == "" {
		return nil, errors.New("S3 endpoint must be an absolute HTTP(S) URL")
	}
	if config.Region == "" {
		config.Region = "auto"
	}
	client := config.Client
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Minute}
	}
	return &S3{
		config:   config,
		endpoint: endpoint,
		client:   client,
		retry: s3RetryPolicy{
			attempts: defaultS3RetryAttempts,
			delay:    defaultS3RetryDelay,
			maxDelay: defaultS3RetryMaxDelay,
			now:      time.Now,
			sleep:    sleepWithContext,
		},
	}, nil
}

func (s3 *S3) Get(ctx context.Context, key string) (Object, error) {
	return s3.get(ctx, key, true)
}

// GetArtifact accepts artifacts uploaded by rclone, which does not attach
// fsbuild's custom SHA-256 metadata. The downloaded bytes are always hashed;
// metadata is verified whenever it is present.
func (s3 *S3) GetArtifact(ctx context.Context, key string) (Object, error) {
	return s3.get(ctx, key, false)
}

func (s3 *S3) get(ctx context.Context, key string, requireMetadata bool) (Object, error) {
	response, err := s3.do(ctx, http.MethodGet, key, "", Content{})
	if err != nil {
		return Object{}, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return Object{}, fmt.Errorf("read S3 object %q: %w", key, err)
	}
	var expected string
	if requireMetadata {
		expected, err = responseSHA256(response, key)
	} else {
		expected, err = optionalResponseSHA256(response, key)
	}
	if err != nil {
		return Object{}, err
	}
	sum := sha256.Sum256(data)
	actual := hex.EncodeToString(sum[:])
	if expected != "" && actual != expected {
		return Object{}, fmt.Errorf("S3 object %q failed SHA-256 verification", key)
	}
	return Object{
		Key: key, Data: data, Size: int64(len(data)),
		ETag: normalizeETag(response.Header.Get("ETag")), SHA256: actual,
	}, nil
}

func (s3 *S3) Head(ctx context.Context, key string) (ObjectInfo, error) {
	return s3.head(ctx, key, true)
}

// HeadArtifact permits absent fsbuild metadata for rclone-created artifacts.
// If metadata exists it is still required to be a valid SHA-256 value.
func (s3 *S3) HeadArtifact(ctx context.Context, key string) (ObjectInfo, error) {
	return s3.head(ctx, key, false)
}

func (s3 *S3) head(ctx context.Context, key string, requireMetadata bool) (ObjectInfo, error) {
	response, err := s3.do(ctx, http.MethodHead, key, "", Content{})
	if err != nil {
		return ObjectInfo{}, err
	}
	defer response.Body.Close()
	var digest string
	if requireMetadata {
		digest, err = responseSHA256(response, key)
	} else {
		digest, err = optionalResponseSHA256(response, key)
	}
	if err != nil {
		return ObjectInfo{}, err
	}
	return ObjectInfo{
		Key: key, Size: response.ContentLength,
		ETag:         normalizeETag(response.Header.Get("ETag")),
		SHA256:       digest,
		LastModified: parseHTTPTime(response.Header.Get("Last-Modified")),
	}, nil
}

func (s3 *S3) List(ctx context.Context, prefix string) ([]ObjectInfo, error) {
	prefix = strings.TrimPrefix(prefix, "/")
	if strings.Contains(prefix, `\`) || strings.Contains(prefix, "..") {
		return nil, fmt.Errorf("invalid list prefix %q", prefix)
	}
	physicalPrefix := path.Join(s3.config.Prefix, prefix)
	if physicalPrefix != "" &&
		(prefix == "" || strings.HasSuffix(prefix, "/")) {
		physicalPrefix += "/"
	}
	var result []ObjectInfo
	continuation := ""
	for {
		query := url.Values{"list-type": {"2"}, "prefix": {physicalPrefix}}
		if continuation != "" {
			query.Set("continuation-token", continuation)
		}
		response, err := s3.doQuery(ctx, http.MethodGet, "", "", Content{}, query)
		if err != nil {
			return nil, err
		}
		var page struct {
			Truncated bool   `xml:"IsTruncated"`
			Next      string `xml:"NextContinuationToken"`
			Contents  []struct {
				Key          string    `xml:"Key"`
				Size         int64     `xml:"Size"`
				ETag         string    `xml:"ETag"`
				LastModified time.Time `xml:"LastModified"`
			} `xml:"Contents"`
		}
		decodeErr := xml.NewDecoder(response.Body).Decode(&page)
		closeErr := response.Body.Close()
		if decodeErr != nil {
			return nil, fmt.Errorf("decode S3 listing: %w", decodeErr)
		}
		if closeErr != nil {
			return nil, closeErr
		}
		for _, object := range page.Contents {
			key := object.Key
			if s3.config.Prefix != "" {
				key = strings.TrimPrefix(key, strings.Trim(s3.config.Prefix, "/")+"/")
			}
			result = append(result, ObjectInfo{
				Key: key, Size: object.Size, ETag: normalizeETag(object.ETag),
				LastModified: object.LastModified.UTC(),
			})
		}
		if !page.Truncated {
			break
		}
		if page.Next == "" {
			return nil, errors.New("S3 listing is truncated without a continuation token")
		}
		continuation = page.Next
	}
	return result, nil
}

func (s3 *S3) PutIfAbsent(ctx context.Context, key string, content Content) (ObjectInfo, bool, error) {
	if err := content.Validate(); err != nil {
		return ObjectInfo{}, false, err
	}
	if content.Size > maxR2ConditionalPutSize {
		return ObjectInfo{}, false, fmt.Errorf(
			"object %q is %d bytes; R2 atomic single-part PUT is limited to %d bytes",
			key, content.Size, maxR2ConditionalPutSize,
		)
	}
	response, err := s3.do(ctx, http.MethodPut, key, "*", content)
	if errors.Is(err, ErrPrecondition) {
		info, headErr := s3.Head(ctx, key)
		return info, false, headErr
	}
	if err != nil {
		return ObjectInfo{}, false, err
	}
	defer response.Body.Close()
	info := ObjectInfo{
		Key: key, Size: content.Size,
		ETag: normalizeETag(response.Header.Get("ETag")), SHA256: content.SHA256,
	}
	if info.ETag == "" {
		returnInfo, headErr := s3.Head(ctx, key)
		return returnInfo, true, headErr
	}
	return info, true, nil
}

func (s3 *S3) CompareAndSwap(ctx context.Context, key, expectedETag string, content Content) (ObjectInfo, error) {
	if expectedETag == "" {
		return ObjectInfo{}, errors.New("expected ETag is required")
	}
	if err := content.Validate(); err != nil {
		return ObjectInfo{}, err
	}
	if content.Size > maxR2ConditionalPutSize {
		return ObjectInfo{}, fmt.Errorf(
			"object %q is %d bytes; R2 atomic single-part PUT is limited to %d bytes",
			key, content.Size, maxR2ConditionalPutSize,
		)
	}
	response, err := s3.do(ctx, http.MethodPut, key, quoteETag(expectedETag), content)
	if err != nil {
		return ObjectInfo{}, err
	}
	defer response.Body.Close()
	info := ObjectInfo{
		Key: key, Size: content.Size,
		ETag: normalizeETag(response.Header.Get("ETag")), SHA256: content.SHA256,
	}
	if info.ETag == "" {
		return s3.Head(ctx, key)
	}
	return info, nil
}

func (s3 *S3) DeleteIfMatch(ctx context.Context, key, expectedETag string) error {
	if expectedETag == "" {
		return errors.New("expected ETag is required")
	}
	if !s3.config.ExclusiveDelete {
		return errors.New("S3 deletion requires an exclusive storage-maintenance lease")
	}
	current, err := s3.Head(ctx, key)
	if errors.Is(err, ErrNotFound) {
		return ErrPrecondition
	}
	if err != nil {
		return err
	}
	if current.ETag != expectedETag {
		return ErrPrecondition
	}
	// R2 documents DeleteObject but not conditional DeleteObject. The caller
	// must hold the repository-wide storage workflow lease, so after the
	// verified HEAD an unconditional delete cannot race another writer.
	response, err := s3.do(ctx, http.MethodDelete, key, "", Content{})
	if err != nil {
		return err
	}
	return response.Body.Close()
}

// PresignGet returns a query-signed GET URL for one exact object. It signs
// only the host header and uses UNSIGNED-PAYLOAD, matching the S3 presigned URL
// contract supported by Cloudflare R2. The caller must verify the object with
// Head before sharing this bearer URL.
func (s3 *S3) PresignGet(key string, validity time.Duration) (string, error) {
	if err := ValidateKey(key); err != nil {
		return "", err
	}
	if validity <= 0 || validity > 7*24*time.Hour {
		return "", errors.New("S3 presigned GET validity must be between one second and seven days")
	}
	now := s3.retry.now().UTC()
	date := now.Format("20060102")
	timestamp := now.Format("20060102T150405Z")
	scope := strings.Join([]string{date, s3.config.Region, "s3", "aws4_request"}, "/")

	requestURL := s3.objectURL(key)
	query := requestURL.Query()
	query.Set("X-Amz-Algorithm", "AWS4-HMAC-SHA256")
	query.Set("X-Amz-Credential", s3.config.AccessKeyID+"/"+scope)
	query.Set("X-Amz-Date", timestamp)
	query.Set("X-Amz-Expires", strconv.FormatInt(int64(validity/time.Second), 10))
	query.Set("X-Amz-SignedHeaders", "host")
	if s3.config.SessionToken != "" {
		query.Set("X-Amz-Security-Token", s3.config.SessionToken)
	}
	requestURL.RawQuery = query.Encode()
	canonicalRequest := strings.Join([]string{
		http.MethodGet,
		requestURL.EscapedPath(),
		requestURL.RawQuery,
		"host:" + requestURL.Host + "\n",
		"host",
		"UNSIGNED-PAYLOAD",
	}, "\n")
	canonicalSum := sha256.Sum256([]byte(canonicalRequest))
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256", timestamp, scope, hex.EncodeToString(canonicalSum[:]),
	}, "\n")
	dateKey := hmacSHA256([]byte("AWS4"+s3.config.SecretKey), date)
	regionKey := hmacSHA256(dateKey, s3.config.Region)
	serviceKey := hmacSHA256(regionKey, "s3")
	signingKey := hmacSHA256(serviceKey, "aws4_request")
	query.Set("X-Amz-Signature", hex.EncodeToString(hmacSHA256(signingKey, stringToSign)))
	requestURL.RawQuery = query.Encode()
	return requestURL.String(), nil
}

func (s3 *S3) objectURL(key string) url.URL {
	requestURL := *s3.endpoint
	segments := []string{strings.Trim(requestURL.Path, "/"), s3.config.Bucket}
	if key != "" {
		segments = append(segments, s3.config.Prefix, key)
	}
	requestURL.Path = "/" + path.Join(segments...)
	requestURL.RawQuery = ""
	requestURL.Fragment = ""
	return requestURL
}

func (s3 *S3) do(ctx context.Context, method, key, condition string, content Content) (*http.Response, error) {
	return s3.doQuery(ctx, method, key, condition, content, nil)
}

func (s3 *S3) doQuery(
	ctx context.Context,
	method, key, condition string,
	content Content,
	query url.Values,
) (*http.Response, error) {
	if key != "" {
		if err := ValidateKey(key); err != nil {
			return nil, err
		}
	}
	if key == "" && method != http.MethodGet {
		return nil, errors.New("empty S3 object key is valid only for listing")
	}
	if err := ctx.Err(); err != nil {
		return nil, ctx.Err()
	}
	requestURL := s3.objectURL(key)
	if query != nil {
		requestURL.RawQuery = query.Encode()
	}

	emptyHash := sha256.Sum256(nil)
	payloadHash := hex.EncodeToString(emptyHash[:])
	if method == http.MethodPut {
		payloadHash = content.SHA256
	}

	attempts := 1
	if s3ReplaySafe(method, condition) && s3.retry.attempts > attempts {
		attempts = s3.retry.attempts
	}
	unresolvedConditionalCreate := false

	for attempt := 0; attempt < attempts; attempt++ {
		request, err := s3.newRequest(
			ctx,
			method,
			requestURL.String(),
			condition,
			payloadHash,
			content,
		)
		if err != nil {
			return nil, err
		}
		response, err := s3.client.Do(request)
		if err != nil {
			if request.Body != nil {
				_ = request.Body.Close()
			}
			if response != nil {
				closeS3RetryResponse(response)
			}
			if ctxErr := ctx.Err(); ctxErr != nil {
				return nil, ctxErr
			}
			executeErr := fmt.Errorf("execute S3 %s %q: %w", method, key, err)
			if method == http.MethodPut && condition == "*" {
				unresolvedConditionalCreate = true
				reconciled, probeErr := s3.reconcileConditionalCreate(
					ctx,
					key,
					content,
				)
				if reconciled != nil {
					return reconciled, nil
				}
				if errors.Is(probeErr, ErrPrecondition) {
					return nil, ErrPrecondition
				}
				if probeErr == nil {
					unresolvedConditionalCreate = false
				} else if attempt+1 >= attempts {
					if ctxErr := ctx.Err(); ctxErr != nil {
						return nil, ctxErr
					}
					return nil, fmt.Errorf(
						"%w; conditional PUT outcome probe failed: %v",
						executeErr,
						probeErr,
					)
				}
			}
			if attempt+1 >= attempts {
				return nil, executeErr
			}
			if err := s3.waitToRetry(ctx, attempt, ""); err != nil {
				return nil, err
			}
			continue
		}
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			return response, nil
		}
		retryable := retryableS3Status(
			method,
			condition,
			response.StatusCode,
		)
		retryAfter := response.Header.Get("Retry-After")
		responseErr := s3ResponseError(method, key, response)
		if method == http.MethodPut &&
			condition == "*" &&
			(retryable || unresolvedConditionalCreate) {
			if retryable {
				unresolvedConditionalCreate = true
			}
			reconciled, probeErr := s3.reconcileConditionalCreate(
				ctx,
				key,
				content,
			)
			if reconciled != nil {
				return reconciled, nil
			}
			if errors.Is(probeErr, ErrPrecondition) {
				return nil, ErrPrecondition
			}
			if probeErr == nil {
				unresolvedConditionalCreate = false
			} else if !retryable || attempt+1 >= attempts {
				if ctxErr := ctx.Err(); ctxErr != nil {
					return nil, ctxErr
				}
				return nil, fmt.Errorf(
					"%w; conditional PUT outcome probe failed: %v",
					responseErr,
					probeErr,
				)
			}
		}
		if retryable {
			if attempt+1 >= attempts {
				return nil, responseErr
			}
			if err := s3.waitToRetry(ctx, attempt, retryAfter); err != nil {
				return nil, err
			}
			continue
		}
		return nil, responseErr
	}

	return nil, fmt.Errorf("execute S3 %s %q: retry attempts exhausted", method, key)
}

func (s3 *S3) reconcileConditionalCreate(
	ctx context.Context,
	key string,
	content Content,
) (*http.Response, error) {
	info, err := s3.Head(ctx, key)
	switch {
	case err == nil:
		if info.Size == content.Size && info.SHA256 == content.SHA256 {
			return reconciledS3PutResponse(info), nil
		}
		return nil, ErrPrecondition
	case errors.Is(err, ErrNotFound):
		return nil, nil
	default:
		return nil, err
	}
}

func reconciledS3PutResponse(info ObjectInfo) *http.Response {
	header := make(http.Header)
	if info.ETag != "" {
		header.Set("ETag", quoteETag(info.ETag))
	}
	// A strongly consistent HEAD proving the exact requested identity makes
	// the ambiguous conditional create a logical success. Returning the
	// equivalent response keeps PutIfAbsent's created=true lease semantics
	// without retransmitting the payload.
	return &http.Response{
		StatusCode:    http.StatusOK,
		Status:        "200 OK",
		Header:        header,
		Body:          http.NoBody,
		ContentLength: 0,
	}
}

func (s3 *S3) newRequest(
	ctx context.Context,
	method, requestURL, condition, payloadHash string,
	content Content,
) (*http.Request, error) {
	var body io.ReadCloser
	if method == http.MethodPut {
		var err error
		body, err = content.Open()
		if err != nil {
			return nil, fmt.Errorf("open S3 request content: %w", err)
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, requestURL, body)
	if err != nil {
		if body != nil {
			_ = body.Close()
		}
		return nil, fmt.Errorf("create S3 request: %w", err)
	}
	if method == http.MethodPut {
		request.ContentLength = content.Size
		request.Header.Set("x-amz-meta-fsbuild-sha256", content.SHA256)
	}
	if condition != "" {
		if condition == "*" {
			request.Header.Set("If-None-Match", "*")
		} else {
			request.Header.Set("If-Match", condition)
		}
	}
	now := s3.retry.now().UTC()
	s3.sign(request, payloadHash, now)
	return request, nil
}

func s3ReplaySafe(method, condition string) bool {
	switch method {
	case http.MethodGet, http.MethodHead:
		return true
	case http.MethodPut:
		// Before replaying If-None-Match, an ambiguous result is reconciled
		// through strongly consistent HEAD: exact requested identity is
		// success, a different object is a precondition failure, and only
		// confirmed absence permits replay. An ambiguous successful If-Match
		// attempt invalidates its own precondition, so CompareAndSwap remains
		// single-attempt and fail-closed.
		return condition == "*"
	default:
		return false
	}
}

func retryableS3Status(method, condition string, status int) bool {
	if status == http.StatusConflict {
		// R2 may use 409 while serializing a conditional create. Retrying
		// remains safe only for If-None-Match, whose 412 result is reconciled
		// by PutIfAbsent. Other 409 responses preserve their existing
		// precondition-failure behavior.
		return method == http.MethodPut && condition == "*"
	}
	switch status {
	case http.StatusRequestTimeout,
		http.StatusTooEarly,
		http.StatusTooManyRequests,
		http.StatusInternalServerError,
		http.StatusBadGateway,
		http.StatusServiceUnavailable,
		http.StatusGatewayTimeout:
		return true
	default:
		// Cloudflare also classifies its edge 520-524 response range as
		// transient. These are not assigned constants by net/http.
		return status >= 520 && status <= 524
	}
}

func (s3 *S3) waitToRetry(
	ctx context.Context,
	attempt int,
	retryAfter string,
) error {
	delay := exponentialRetryDelay(s3.retry.delay, s3.retry.maxDelay, attempt)
	if retryAfter != "" {
		// Evaluate an HTTP-date when the retry decision is made, not when
		// the request was signed: a multi-gigabyte PUT may take minutes.
		now := s3.retry.now().UTC()
		if headerDelay, ok := parseRetryAfter(retryAfter, now); ok &&
			headerDelay > delay {
			delay = headerDelay
		}
	}
	if delay > s3.retry.maxDelay {
		delay = s3.retry.maxDelay
	}
	if err := s3.retry.sleep(ctx, delay); err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
		return fmt.Errorf("wait to retry S3 request: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}

func exponentialRetryDelay(
	initial, maximum time.Duration,
	attempt int,
) time.Duration {
	if initial <= 0 || maximum <= 0 {
		return 0
	}
	delay := initial
	for retry := 0; retry < attempt && delay < maximum; retry++ {
		if delay > maximum/2 {
			return maximum
		}
		delay *= 2
	}
	if delay > maximum {
		return maximum
	}
	return delay
}

func parseRetryAfter(value string, now time.Time) (time.Duration, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	if seconds, err := strconv.ParseInt(value, 10, 64); err == nil {
		if seconds < 0 {
			return 0, false
		}
		if seconds > int64((time.Duration(1<<63-1))/time.Second) {
			return time.Duration(1<<63 - 1), true
		}
		return time.Duration(seconds) * time.Second, true
	}
	retryAt, err := http.ParseTime(value)
	if err != nil {
		return 0, false
	}
	delay := retryAt.Sub(now)
	if delay < 0 {
		delay = 0
	}
	return delay, true
}

func sleepWithContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func closeS3RetryResponse(response *http.Response) {
	if response == nil || response.Body == nil {
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, maxS3RetryDrainSize))
	_ = response.Body.Close()
}

func s3ResponseError(method, key string, response *http.Response) error {
	detail := readS3Error(response)
	switch response.StatusCode {
	case http.StatusNotFound:
		return ErrNotFound
	case http.StatusConflict, http.StatusPreconditionFailed:
		return ErrPrecondition
	default:
		if detail != "" {
			return fmt.Errorf(
				"S3 %s %q returned %s (%s)",
				method, key, response.Status, detail,
			)
		}
		return fmt.Errorf("S3 %s %q returned %s", method, key, response.Status)
	}
}

func readS3Error(response *http.Response) string {
	defer response.Body.Close()
	limited := &io.LimitedReader{
		R: response.Body,
		N: maxS3ErrorBodySize + 1,
	}
	data, err := io.ReadAll(limited)
	if err != nil || int64(len(data)) > maxS3ErrorBodySize {
		return ""
	}
	var payload struct {
		Code    string `xml:"Code"`
		Message string `xml:"Message"`
	}
	if err := xml.Unmarshal(data, &payload); err != nil {
		return ""
	}
	code := compactS3ErrorField(payload.Code)
	message := compactS3ErrorField(payload.Message)
	switch {
	case code != "" && message != "":
		return code + ": " + message
	case code != "":
		return code
	default:
		return message
	}
}

func compactS3ErrorField(value string) string {
	value = strings.Join(strings.Fields(value), " ")
	runes := []rune(value)
	if len(runes) > maxS3ErrorFieldRunes {
		value = string(runes[:maxS3ErrorFieldRunes]) + "..."
	}
	return value
}

func parseHTTPTime(value string) time.Time {
	parsed, err := http.ParseTime(value)
	if err != nil {
		return time.Time{}
	}
	return parsed.UTC()
}

func optionalResponseSHA256(response *http.Response, key string) (string, error) {
	const metadataName = "x-amz-meta-fsbuild-sha256"
	present := false
	for name := range response.Header {
		if strings.EqualFold(name, metadataName) {
			present = true
			break
		}
	}
	if !present {
		return "", nil
	}
	digest := strings.TrimSpace(response.Header.Get(metadataName))
	if !validSHA256(digest) {
		return "", fmt.Errorf("S3 object %q has invalid fsbuild SHA-256 metadata", key)
	}
	return digest, nil
}

func responseSHA256(response *http.Response, key string) (string, error) {
	digest, err := optionalResponseSHA256(response, key)
	if err != nil || digest == "" {
		return "", fmt.Errorf("S3 object %q has no valid fsbuild SHA-256 metadata", key)
	}
	return digest, nil
}

func (s3 *S3) sign(request *http.Request, payloadHash string, now time.Time) {
	const service = "s3"
	date := now.Format("20060102")
	timestamp := now.Format("20060102T150405Z")
	request.Header.Set("x-amz-content-sha256", payloadHash)
	request.Header.Set("x-amz-date", timestamp)
	if s3.config.SessionToken != "" {
		request.Header.Set("x-amz-security-token", s3.config.SessionToken)
	}

	headers := make([]string, 0, len(request.Header)+1)
	host := request.Host
	if host == "" {
		host = request.URL.Host
	}
	values := map[string]string{"host": host}
	for name, entries := range request.Header {
		lower := strings.ToLower(name)
		headers = append(headers, lower)
		values[lower] = strings.Join(entries, ",")
	}
	headers = append(headers, "host")
	sort.Strings(headers)
	headers = unique(headers)

	var canonicalHeaders strings.Builder
	for _, header := range headers {
		canonicalHeaders.WriteString(header)
		canonicalHeaders.WriteByte(':')
		canonicalHeaders.WriteString(strings.Join(strings.Fields(values[header]), " "))
		canonicalHeaders.WriteByte('\n')
	}
	signedHeaders := strings.Join(headers, ";")
	canonicalRequest := strings.Join([]string{
		request.Method,
		request.URL.EscapedPath(),
		request.URL.Query().Encode(),
		canonicalHeaders.String(),
		signedHeaders,
		payloadHash,
	}, "\n")
	scope := strings.Join([]string{date, s3.config.Region, service, "aws4_request"}, "/")
	canonicalSum := sha256.Sum256([]byte(canonicalRequest))
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256", timestamp, scope, hex.EncodeToString(canonicalSum[:]),
	}, "\n")
	dateKey := hmacSHA256([]byte("AWS4"+s3.config.SecretKey), date)
	regionKey := hmacSHA256(dateKey, s3.config.Region)
	serviceKey := hmacSHA256(regionKey, service)
	signingKey := hmacSHA256(serviceKey, "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(signingKey, stringToSign))
	request.Header.Set("Authorization", fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		s3.config.AccessKeyID, scope, signedHeaders, signature,
	))
}

func hmacSHA256(key []byte, value string) []byte {
	hash := hmac.New(sha256.New, key)
	_, _ = io.WriteString(hash, value)
	return hash.Sum(nil)
}

func unique(values []string) []string {
	if len(values) == 0 {
		return values
	}
	result := values[:1]
	for _, value := range values[1:] {
		if value != result[len(result)-1] {
			result = append(result, value)
		}
	}
	return result
}

func normalizeETag(value string) string {
	return strings.Trim(strings.TrimSpace(value), `"`)
}

func quoteETag(value string) string {
	return `"` + normalizeETag(value) + `"`
}
