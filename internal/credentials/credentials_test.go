package credentials

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestGoClientMatchesSharedBrokerProtocolContract(t *testing.T) {
	var contract struct {
		SchemaVersion                 string   `json:"schema_version"`
		RequestSchema                 string   `json:"request_schema"`
		RequestFields                 []string `json:"request_fields"`
		ResponseSchema                string   `json:"response_schema"`
		ResponseFields                []string `json:"response_fields"`
		Prefix                        string   `json:"prefix"`
		Region                        string   `json:"region"`
		DefaultMinimumValiditySeconds int64    `json:"default_minimum_validity_seconds"`
		MaximumValiditySeconds        int64    `json:"maximum_validity_seconds"`
		Roles                         []string `json:"roles"`
	}
	data, err := os.ReadFile(filepath.Join("..", "..", "broker", "protocol-contract.json"))
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		t.Fatal(err)
	}
	if contract.SchemaVersion != "fsbuild.credential-broker-contract/v1" ||
		contract.RequestSchema != RequestSchema ||
		contract.ResponseSchema != ResponseSchema ||
		contract.Prefix != "v1" ||
		contract.Region != "auto" ||
		time.Duration(contract.DefaultMinimumValiditySeconds)*time.Second != defaultMinimumValidity ||
		time.Duration(contract.MaximumValiditySeconds)*time.Second != maximumValidity {
		t.Fatalf("shared broker contract does not match Go constants: %+v", contract)
	}
	if got := jsonFieldNames(credentialRequest{}); !reflect.DeepEqual(got, contract.RequestFields) {
		t.Fatalf("credential request fields = %v, want %v", got, contract.RequestFields)
	}
	if got := jsonFieldNames(TemporaryR2Credentials{}); !reflect.DeepEqual(got, contract.ResponseFields) {
		t.Fatalf("credential response fields = %v, want %v", got, contract.ResponseFields)
	}
	if len(contract.Roles) == 0 || !sort.StringsAreSorted(contract.Roles) {
		t.Fatalf("shared broker roles must be nonempty and sorted: %v", contract.Roles)
	}
}

func jsonFieldNames(value any) []string {
	typ := reflect.TypeOf(value)
	names := make([]string, 0, typ.NumField())
	for index := 0; index < typ.NumField(); index++ {
		name := strings.Split(typ.Field(index).Tag.Get("json"), ",")[0]
		if name != "" && name != "-" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

func TestAcquireAndExportUsesOIDCAndWritesOnlyGitHubEnvironment(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	oidcToken := testJWT(`{"alg":"RS256"}`, `{"aud":"broker"}`, "signature")
	temporary := validTemporaryCredentials(now)
	var oidcRequests atomic.Int32
	var brokerRequests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		writer.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/oidc":
			oidcRequests.Add(1)
			if request.Method != http.MethodGet ||
				request.URL.Query().Get("audience") != "https://"+request.Host ||
				request.Header.Get("Authorization") != "Bearer runner-request-token" {
				http.Error(writer, `{"error":"bad OIDC request"}`, http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(oidcResponse{Value: oidcToken})
		case "/v1/credentials":
			brokerRequests.Add(1)
			if request.Method != http.MethodPost ||
				request.Header.Get("Authorization") != "Bearer "+oidcToken {
				http.Error(writer, `{"error":"bad broker authorization"}`, http.StatusUnauthorized)
				return
			}
			var requestBody credentialRequest
			decoder := json.NewDecoder(request.Body)
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&requestBody); err != nil ||
				requestBody.SchemaVersion != RequestSchema ||
				requestBody.Role != "cas-worker" {
				http.Error(writer, `{"error":"bad request body"}`, http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(temporary)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	githubEnv := filepath.Join(t.TempDir(), "github-env")
	if err := os.WriteFile(githubEnv, []byte("EXISTING=value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var masks bytes.Buffer
	err := AcquireAndExport(context.Background(), Options{
		BrokerURL:        server.URL + "/v1/credentials",
		Audience:         server.URL,
		Role:             "cas-worker",
		ExpectedBucket:   "freesense-builds",
		ExpectedPrefix:   "v1",
		ExpectedEndpoint: "https://0123456789abcdef.r2.cloudflarestorage.com",
		GitHubEnv:        githubEnv,
		OIDCRequestURL:   server.URL + "/oidc?api-version=1",
		OIDCRequestToken: "runner-request-token",
		MinimumValidity:  10 * time.Minute,
		Client:           server.Client(),
		Now:              func() time.Time { return now },
		MaskWriter:       &masks,
	})
	if err != nil {
		t.Fatal(err)
	}
	if oidcRequests.Load() != 1 || brokerRequests.Load() != 1 {
		t.Fatalf(
			"request counts: oidc=%d broker=%d",
			oidcRequests.Load(),
			brokerRequests.Load(),
		)
	}
	for _, secret := range []string{
		temporary.AccessKeyID,
		temporary.SecretAccessKey,
		temporary.SessionToken,
	} {
		if !strings.Contains(masks.String(), "::add-mask::"+secret+"\n") {
			t.Fatalf("credential was not masked: masks=%q", masks.String())
		}
	}
	data, err := os.ReadFile(githubEnv)
	if err != nil {
		t.Fatal(err)
	}
	environment := string(data)
	for _, expected := range []string{
		"EXISTING=value\n",
		"AWS_ACCESS_KEY_ID=" + temporary.AccessKeyID + "\n",
		"AWS_SECRET_ACCESS_KEY=" + temporary.SecretAccessKey + "\n",
		"AWS_SESSION_TOKEN=" + temporary.SessionToken + "\n",
		"AWS_ENDPOINT_URL=https://0123456789abcdef.r2.cloudflarestorage.com\n",
		"AWS_ENDPOINT_URL_S3=https://0123456789abcdef.r2.cloudflarestorage.com\n",
		"AWS_REGION=auto\n",
		"AWS_CREDENTIAL_EXPIRATION=2026-07-17T13:00:00.000Z\n",
		"R2_BUCKET=freesense-builds\n",
		"FSBUILD_STORE_URL=s3://freesense-builds/v1\n",
		"FSBUILD_REQUIRE_SESSION_TOKEN=true\n",
	} {
		if !strings.Contains(environment, expected) {
			t.Fatalf("GITHUB_ENV is missing %q:\n%s", expected, environment)
		}
	}
}

func TestAcquireAndExportRejectsRedirects(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	oidcToken := testJWT(`{"alg":"RS256"}`, `{"aud":"broker"}`, "signature")
	var redirected atomic.Bool
	server := httptest.NewTLSServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		writer.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/oidc":
			_ = json.NewEncoder(writer).Encode(oidcResponse{Value: oidcToken})
		case "/v1/credentials":
			http.Redirect(writer, request, "/redirected", http.StatusTemporaryRedirect)
		case "/redirected":
			redirected.Store(true)
			_ = json.NewEncoder(writer).Encode(validTemporaryCredentials(now))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	githubEnv := filepath.Join(t.TempDir(), "github-env")
	if err := os.WriteFile(githubEnv, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	err := AcquireAndExport(context.Background(), Options{
		BrokerURL:        server.URL + "/v1/credentials",
		Audience:         server.URL,
		Role:             "cas-worker",
		ExpectedBucket:   "freesense-builds",
		ExpectedPrefix:   "v1",
		ExpectedEndpoint: "https://0123456789abcdef.r2.cloudflarestorage.com",
		GitHubEnv:        githubEnv,
		OIDCRequestURL:   server.URL + "/oidc",
		OIDCRequestToken: "runner-request-token",
		Client:           server.Client(),
		Now:              func() time.Time { return now },
		MaskWriter:       &bytes.Buffer{},
	})
	if err == nil || !strings.Contains(err.Error(), "307 Temporary Redirect") {
		t.Fatalf("redirect error = %v", err)
	}
	if redirected.Load() {
		t.Fatal("credential client followed a broker redirect")
	}
}

func TestPrepareOptionsRejectsNonHTTPSURLs(t *testing.T) {
	githubEnv := filepath.Join(t.TempDir(), "github-env")
	if err := os.WriteFile(githubEnv, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	options := Options{
		BrokerURL:        "http://broker.example.invalid/v1/credentials",
		Audience:         "https://broker.example.invalid",
		Role:             "cas-worker",
		ExpectedBucket:   "freesense-builds",
		ExpectedPrefix:   "v1",
		ExpectedEndpoint: "https://0123456789abcdef.r2.cloudflarestorage.com",
		GitHubEnv:        githubEnv,
		OIDCRequestURL:   "https://token.actions.githubusercontent.com/token",
		OIDCRequestToken: "runner-request-token",
	}
	if _, err := prepareOptions(options); err == nil ||
		!strings.Contains(err.Error(), "broker URL must be an absolute HTTPS URL") {
		t.Fatalf("HTTP broker error = %v", err)
	}
	options.BrokerURL = "https://broker.example.invalid/wrong"
	if _, err := prepareOptions(options); err == nil ||
		!strings.Contains(err.Error(), "path must be /v1/credentials") {
		t.Fatalf("broker path error = %v", err)
	}
	options.BrokerURL = "https://broker.example.invalid/v1/credentials"
	options.Audience = "https://other.example.invalid"
	if _, err := prepareOptions(options); err == nil ||
		!strings.Contains(err.Error(), "same HTTPS origin") {
		t.Fatalf("audience origin error = %v", err)
	}
	options.Audience = "https://broker.example.invalid"
	options.ExpectedEndpoint = "http://account.r2.cloudflarestorage.com"
	if _, err := prepareOptions(options); err == nil ||
		!strings.Contains(err.Error(), "absolute HTTPS URL") {
		t.Fatalf("HTTP endpoint error = %v", err)
	}
}

func TestValidateTemporaryCredentialsFailsClosed(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	options := Options{
		ExpectedBucket:   "freesense-builds",
		ExpectedPrefix:   "v1",
		ExpectedEndpoint: "https://0123456789abcdef.r2.cloudflarestorage.com",
		MinimumValidity:  10 * time.Minute,
		Now:              func() time.Time { return now },
	}
	tests := []struct {
		name   string
		mutate func(*TemporaryR2Credentials)
		match  string
	}{
		{"schema", func(value *TemporaryR2Credentials) {
			value.SchemaVersion = "unknown"
		}, "schema version"},
		{"access key", func(value *TemporaryR2Credentials) {
			value.AccessKeyID = "short"
		}, "access key ID"},
		{"secret key", func(value *TemporaryR2Credentials) {
			value.SecretAccessKey = strings.Repeat("g", 64)
		}, "secret access key"},
		{"session token", func(value *TemporaryR2Credentials) {
			value.SessionToken = base64.StdEncoding.EncodeToString([]byte("not-a-jwt"))
		}, "session token"},
		{"endpoint", func(value *TemporaryR2Credentials) {
			value.Endpoint = "https://other.r2.cloudflarestorage.com"
		}, "endpoint does not match"},
		{"bucket", func(value *TemporaryR2Credentials) {
			value.Bucket = "another-bucket"
		}, "bucket does not match"},
		{"prefix", func(value *TemporaryR2Credentials) {
			value.Prefix = "another/prefix"
		}, "prefix does not match"},
		{"region", func(value *TemporaryR2Credentials) {
			value.Region = "us-east-1"
		}, "region"},
		{"too short", func(value *TemporaryR2Credentials) {
			value.ExpiresAt = now.Add(9 * time.Minute).Format(time.RFC3339)
		}, "minimum validity"},
		{"too long", func(value *TemporaryR2Credentials) {
			value.ExpiresAt = now.Add(maximumValidity + time.Second).Format(time.RFC3339)
		}, "maximum"},
		{"noncanonical time", func(value *TemporaryR2Credentials) {
			value.ExpiresAt = "2026-07-17T15:00:00+02:00"
		}, "canonical UTC"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			value := validTemporaryCredentials(now)
			test.mutate(&value)
			err := validateTemporaryCredentials(value, options)
			if err == nil || !strings.Contains(err.Error(), test.match) {
				t.Fatalf("validation error = %v, want %q", err, test.match)
			}
		})
	}
}

func TestDecodeJSONResponseRejectsOversizeAndUnknownFields(t *testing.T) {
	oversized := httptest.NewRecorder()
	oversized.Header().Set("Content-Type", "application/json")
	_, _ = oversized.Write(bytes.Repeat([]byte("x"), int(maximumResponseBytes+1)))
	var target oidcResponse
	if err := decodeJSONResponse(
		oversized.Result(),
		&target,
		"test response",
	); err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("oversize error = %v", err)
	}

	unknown := httptest.NewRecorder()
	unknown.Header().Set("Content-Type", "application/json")
	_, _ = unknown.WriteString(`{"value":"a.b.c","extra":true}`)
	if err := decodeJSONResponse(
		unknown.Result(),
		&target,
		"test response",
	); err == nil || !strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("unknown-field error = %v", err)
	}
}

func validTemporaryCredentials(now time.Time) TemporaryR2Credentials {
	credentialJWT := testJWT(
		`{"alg":"HS256","typ":"JWT"}`,
		`{"bucket":"freesense-builds"}`,
		"signature",
	)
	return TemporaryR2Credentials{
		SchemaVersion:   ResponseSchema,
		AccessKeyID:     strings.Repeat("a", 32),
		SecretAccessKey: strings.Repeat("b", 64),
		SessionToken: base64.StdEncoding.EncodeToString(
			[]byte("jwt/" + credentialJWT),
		),
		Endpoint:  "https://0123456789abcdef.r2.cloudflarestorage.com",
		Region:    "auto",
		Bucket:    "freesense-builds",
		Prefix:    "v1",
		ExpiresAt: now.Add(time.Hour).Format("2006-01-02T15:04:05.000Z"),
	}
}

func testJWT(header, payload, signature string) string {
	encode := base64.RawURLEncoding.EncodeToString
	return encode([]byte(header)) + "." +
		encode([]byte(payload)) + "." +
		encode([]byte(signature))
}
