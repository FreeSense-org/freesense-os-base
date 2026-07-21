// Package credentials exchanges a GitHub Actions OIDC identity for
// short-lived, narrowly scoped Cloudflare R2 credentials.
package credentials

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	RequestSchema  = "fsbuild.credential-request/v1"
	ResponseSchema = "fsbuild.temporary-r2-credentials/v1"

	maximumResponseBytes   = int64(64 * 1024)
	maximumOIDCTokenSize   = 32 * 1024
	maximumValidity        = 6 * time.Hour
	defaultMinimumValidity = 5 * time.Minute
	defaultHTTPTimeout     = 30 * time.Second
)

var (
	rolePattern      = regexp.MustCompile(`^[a-z][a-z0-9-]{0,63}$`)
	bucketPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`)
	accessKeyPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	secretKeyPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	utcExpiryPattern = regexp.MustCompile(
		`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z$`,
	)
)

type Options struct {
	BrokerURL        string
	Audience         string
	Role             string
	ExpectedBucket   string
	ExpectedPrefix   string
	ExpectedEndpoint string
	GitHubEnv        string
	OIDCRequestURL   string
	OIDCRequestToken string
	MinimumValidity  time.Duration

	Client     *http.Client
	Now        func() time.Time
	MaskWriter io.Writer
}

type credentialRequest struct {
	SchemaVersion string `json:"schema_version"`
	Role          string `json:"role"`
}

type oidcResponse struct {
	Value string `json:"value"`
}

type TemporaryR2Credentials struct {
	SchemaVersion   string `json:"schema_version"`
	AccessKeyID     string `json:"access_key_id"`
	SecretAccessKey string `json:"secret_access_key"`
	SessionToken    string `json:"session_token"`
	Endpoint        string `json:"endpoint"`
	Region          string `json:"region"`
	Bucket          string `json:"bucket"`
	Prefix          string `json:"prefix"`
	ExpiresAt       string `json:"expires_at"`
}

// AcquireAndExport obtains an OIDC token from the GitHub runner, exchanges it
// with the configured broker, validates the complete response, masks all
// bearer credential material, and appends the resulting environment to
// GitHub's per-job environment file.
func AcquireAndExport(ctx context.Context, options Options) error {
	prepared, err := prepareOptions(options)
	if err != nil {
		return err
	}
	token, err := acquireOIDCToken(ctx, prepared)
	if err != nil {
		return err
	}
	temporary, err := exchangeToken(ctx, prepared, token)
	if err != nil {
		return err
	}
	if err := validateTemporaryCredentials(temporary, prepared); err != nil {
		return fmt.Errorf("validate broker response: %w", err)
	}
	if err := exportGitHubEnvironment(temporary, prepared); err != nil {
		return fmt.Errorf("export temporary credentials: %w", err)
	}
	return nil
}

func prepareOptions(options Options) (Options, error) {
	if options.Now == nil {
		options.Now = func() time.Time { return time.Now().UTC() }
	}
	if options.MaskWriter == nil {
		options.MaskWriter = os.Stdout
	}
	if options.Client == nil {
		options.Client = &http.Client{Timeout: defaultHTTPTimeout}
	}
	if options.Client.Timeout == 0 {
		copy := *options.Client
		copy.Timeout = defaultHTTPTimeout
		options.Client = &copy
	}
	if options.MinimumValidity <= 0 {
		options.MinimumValidity = defaultMinimumValidity
	}
	if options.MinimumValidity > maximumValidity {
		return Options{}, fmt.Errorf(
			"minimum validity %s exceeds maximum broker validity %s",
			options.MinimumValidity,
			maximumValidity,
		)
	}
	brokerURL, err := validateHTTPSURL("broker URL", options.BrokerURL, false, false)
	if err != nil {
		return Options{}, err
	}
	if brokerURL.Path != "/v1/credentials" {
		return Options{}, errors.New("broker URL path must be /v1/credentials")
	}
	if _, err := validateHTTPSURL("GitHub OIDC request URL", options.OIDCRequestURL, true, false); err != nil {
		return Options{}, err
	}
	audienceURL, err := validateHTTPSURL("OIDC audience", options.Audience, false, true)
	if err != nil {
		return Options{}, err
	}
	if !strings.EqualFold(brokerURL.Host, audienceURL.Host) {
		return Options{}, errors.New("broker URL and OIDC audience must have the same HTTPS origin")
	}
	if _, err := canonicalEndpoint(options.ExpectedEndpoint); err != nil {
		return Options{}, fmt.Errorf("expected R2 endpoint: %w", err)
	}
	if !rolePattern.MatchString(options.Role) {
		return Options{}, errors.New("broker role must be lowercase alphanumeric with optional internal hyphens")
	}
	if err := validatePlainValue("GitHub OIDC request token", options.OIDCRequestToken, 16*1024); err != nil {
		return Options{}, err
	}
	if !bucketPattern.MatchString(options.ExpectedBucket) {
		return Options{}, errors.New("expected R2 bucket has an invalid name")
	}
	if err := validatePrefix(options.ExpectedPrefix); err != nil {
		return Options{}, fmt.Errorf("expected R2 prefix: %w", err)
	}
	if err := validateGitHubEnvPath(options.GitHubEnv); err != nil {
		return Options{}, err
	}
	return options, nil
}

func acquireOIDCToken(ctx context.Context, options Options) (string, error) {
	requestURL, err := url.Parse(options.OIDCRequestURL)
	if err != nil {
		return "", fmt.Errorf("parse GitHub OIDC request URL: %w", err)
	}
	query := requestURL.Query()
	query.Set("audience", options.Audience)
	requestURL.RawQuery = query.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL.String(), nil)
	if err != nil {
		return "", fmt.Errorf("create GitHub OIDC request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Authorization", "Bearer "+options.OIDCRequestToken)
	response, err := noRedirectClient(options.Client).Do(request)
	if err != nil {
		return "", fmt.Errorf("request GitHub OIDC token: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("GitHub OIDC endpoint returned %s", response.Status)
	}
	var envelope oidcResponse
	if err := decodeJSONResponse(response, &envelope, "GitHub OIDC response"); err != nil {
		return "", err
	}
	if err := validateJWT(envelope.Value, maximumOIDCTokenSize); err != nil {
		return "", fmt.Errorf("GitHub OIDC response contains an invalid token: %w", err)
	}
	return envelope.Value, nil
}

func exchangeToken(
	ctx context.Context,
	options Options,
	oidcToken string,
) (TemporaryR2Credentials, error) {
	payload, err := json.Marshal(credentialRequest{
		SchemaVersion: RequestSchema,
		Role:          options.Role,
	})
	if err != nil {
		return TemporaryR2Credentials{}, err
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		options.BrokerURL,
		bytes.NewReader(payload),
	)
	if err != nil {
		return TemporaryR2Credentials{}, fmt.Errorf("create credential broker request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Authorization", "Bearer "+oidcToken)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", "freesense-fsbuild/credentials-v1")
	response, err := noRedirectClient(options.Client).Do(request)
	if err != nil {
		return TemporaryR2Credentials{}, fmt.Errorf("request temporary R2 credentials: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return TemporaryR2Credentials{}, fmt.Errorf("credential broker returned %s", response.Status)
	}
	var temporary TemporaryR2Credentials
	if err := decodeJSONResponse(response, &temporary, "credential broker response"); err != nil {
		return TemporaryR2Credentials{}, err
	}
	return temporary, nil
}

func validateTemporaryCredentials(
	temporary TemporaryR2Credentials,
	options Options,
) error {
	if temporary.SchemaVersion != ResponseSchema {
		return fmt.Errorf("unexpected schema version %q", temporary.SchemaVersion)
	}
	if !accessKeyPattern.MatchString(temporary.AccessKeyID) {
		return errors.New("access key ID is not 32 lowercase hexadecimal characters")
	}
	if !secretKeyPattern.MatchString(temporary.SecretAccessKey) {
		return errors.New("secret access key is not 64 lowercase hexadecimal characters")
	}
	decodedSession, err := base64.StdEncoding.DecodeString(temporary.SessionToken)
	if err != nil || len(decodedSession) <= len("jwt/") ||
		!strings.HasPrefix(string(decodedSession), "jwt/") {
		return errors.New("session token is not a locally signed R2 temporary credential")
	}
	if err := validateJWT(string(decodedSession[len("jwt/"):]), maximumOIDCTokenSize); err != nil {
		return fmt.Errorf("session token contains an invalid credential JWT: %w", err)
	}
	actualEndpoint, err := canonicalEndpoint(temporary.Endpoint)
	if err != nil {
		return fmt.Errorf("R2 endpoint: %w", err)
	}
	expectedEndpoint, err := canonicalEndpoint(options.ExpectedEndpoint)
	if err != nil {
		return fmt.Errorf("expected R2 endpoint: %w", err)
	}
	if actualEndpoint != expectedEndpoint {
		return errors.New("R2 endpoint does not match the protected expected endpoint")
	}
	if temporary.Region != "auto" {
		return fmt.Errorf("R2 region is %q, expected %q", temporary.Region, "auto")
	}
	if temporary.Bucket != options.ExpectedBucket {
		return errors.New("R2 bucket does not match the protected expected bucket")
	}
	if temporary.Prefix != options.ExpectedPrefix {
		return errors.New("R2 prefix does not match the protected expected prefix")
	}
	expiresAt, err := time.Parse(time.RFC3339Nano, temporary.ExpiresAt)
	if err != nil || !utcExpiryPattern.MatchString(temporary.ExpiresAt) {
		return errors.New("credential expiration must be canonical UTC RFC3339")
	}
	now := options.Now().UTC()
	if expiresAt.Before(now.Add(options.MinimumValidity)) {
		return fmt.Errorf(
			"temporary credential expires before the required minimum validity of %s",
			options.MinimumValidity,
		)
	}
	if expiresAt.After(now.Add(maximumValidity)) {
		return fmt.Errorf(
			"temporary credential validity exceeds the maximum of %s",
			maximumValidity,
		)
	}
	return nil
}

func exportGitHubEnvironment(
	temporary TemporaryR2Credentials,
	options Options,
) error {
	for _, secret := range []string{
		temporary.AccessKeyID,
		temporary.SecretAccessKey,
		temporary.SessionToken,
	} {
		if _, err := fmt.Fprintf(options.MaskWriter, "::add-mask::%s\n", secret); err != nil {
			return fmt.Errorf("register GitHub secret mask: %w", err)
		}
	}
	endpoint, _ := canonicalEndpoint(temporary.Endpoint)
	storeURL := "s3://" + temporary.Bucket + "/" + temporary.Prefix
	values := []struct {
		Name  string
		Value string
	}{
		{"AWS_ACCESS_KEY_ID", temporary.AccessKeyID},
		{"AWS_SECRET_ACCESS_KEY", temporary.SecretAccessKey},
		{"AWS_SESSION_TOKEN", temporary.SessionToken},
		{"AWS_ENDPOINT_URL", endpoint},
		{"AWS_ENDPOINT_URL_S3", endpoint},
		{"AWS_REGION", temporary.Region},
		{"AWS_DEFAULT_REGION", temporary.Region},
		{"AWS_CREDENTIAL_EXPIRATION", temporary.ExpiresAt},
		{"R2_BUCKET", temporary.Bucket},
		{"FSBUILD_S3_ENDPOINT", endpoint},
		{"FSBUILD_S3_REGION", temporary.Region},
		{"FSBUILD_STORE_PREFIX", temporary.Prefix},
		{"FSBUILD_STORE_URL", storeURL},
		{"FSBUILD_REQUIRE_SESSION_TOKEN", "true"},
	}
	var environment strings.Builder
	for _, item := range values {
		if err := validateEnvironmentAssignment(item.Name, item.Value); err != nil {
			return err
		}
		environment.WriteString(item.Name)
		environment.WriteByte('=')
		environment.WriteString(item.Value)
		environment.WriteByte('\n')
	}
	file, err := os.OpenFile(options.GitHubEnv, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		return fmt.Errorf("open GITHUB_ENV: %w", err)
	}
	if _, err := io.WriteString(file, environment.String()); err != nil {
		_ = file.Close()
		return fmt.Errorf("append GITHUB_ENV: %w", err)
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("sync GITHUB_ENV: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close GITHUB_ENV: %w", err)
	}
	return nil
}

func noRedirectClient(client *http.Client) *http.Client {
	copy := *client
	copy.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &copy
}

func decodeJSONResponse(response *http.Response, target any, label string) error {
	mediaType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		return fmt.Errorf("%s must have Content-Type application/json", label)
	}
	limited := &io.LimitedReader{R: response.Body, N: maximumResponseBytes + 1}
	data, err := io.ReadAll(limited)
	if err != nil {
		return fmt.Errorf("read %s: %w", label, err)
	}
	if int64(len(data)) > maximumResponseBytes {
		return fmt.Errorf("%s exceeds %d bytes", label, maximumResponseBytes)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return fmt.Errorf("decode %s: %w", label, err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return fmt.Errorf("%s contains multiple JSON values", label)
		}
		return fmt.Errorf("decode trailing %s data: %w", label, err)
	}
	return nil
}

func validateJWT(value string, maximum int) error {
	if value == "" || len(value) > maximum {
		return errors.New("JWT length is outside the accepted range")
	}
	parts := strings.Split(value, ".")
	if len(parts) != 3 {
		return errors.New("JWT must contain exactly three segments")
	}
	for index, part := range parts {
		if part == "" {
			return fmt.Errorf("JWT segment %d is empty", index+1)
		}
		decoded, err := base64.RawURLEncoding.DecodeString(part)
		if err != nil || len(decoded) == 0 {
			return fmt.Errorf("JWT segment %d is not valid base64url", index+1)
		}
	}
	return nil
}

func validateHTTPSURL(
	name, value string,
	allowQuery, endpointOnly bool,
) (*url.URL, error) {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" {
		return nil, fmt.Errorf("%s must be an absolute HTTPS URL", name)
	}
	if parsed.User != nil || parsed.Fragment != "" {
		return nil, fmt.Errorf("%s cannot contain user information or a fragment", name)
	}
	if !allowQuery && parsed.RawQuery != "" {
		return nil, fmt.Errorf("%s cannot contain a query", name)
	}
	if endpointOnly && parsed.Path != "" && parsed.Path != "/" {
		return nil, fmt.Errorf("%s cannot contain a path", name)
	}
	return parsed, nil
}

func canonicalEndpoint(value string) (string, error) {
	parsed, err := validateHTTPSURL("endpoint", value, false, true)
	if err != nil {
		return "", err
	}
	return "https://" + strings.ToLower(parsed.Host), nil
}

func validatePlainValue(name, value string, maximum int) error {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) ||
		strings.TrimSpace(value) != value ||
		strings.ContainsAny(value, "\x00\r\n") {
		return fmt.Errorf("%s is empty, malformed, or too long", name)
	}
	return nil
}

func validatePrefix(value string) error {
	if err := validatePlainValue("prefix", value, 512); err != nil {
		return err
	}
	if strings.HasPrefix(value, "/") || strings.HasSuffix(value, "/") ||
		strings.Contains(value, `\`) ||
		path.Clean(value) != value ||
		value == "." || value == ".." || strings.HasPrefix(value, "../") {
		return errors.New("prefix must be a canonical relative object path")
	}
	return nil
}

func validateGitHubEnvPath(filename string) error {
	if filename == "" || !filepath.IsAbs(filename) ||
		filepath.Clean(filename) != filename {
		return errors.New("GITHUB_ENV must be a clean absolute path")
	}
	info, err := os.Lstat(filename)
	if err != nil {
		return fmt.Errorf("inspect GITHUB_ENV: %w", err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return errors.New("GITHUB_ENV must be an existing regular file, not a symbolic link")
	}
	return nil
}

func validateEnvironmentAssignment(name, value string) error {
	if name == "" || strings.ContainsAny(name, "=\r\n\x00") {
		return errors.New("invalid environment variable name")
	}
	if err := validatePlainValue("environment value for "+name, value, 64*1024); err != nil {
		return err
	}
	return nil
}
