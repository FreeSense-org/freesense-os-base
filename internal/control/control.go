// Package control owns the small mutable control plane above immutable R2
// artifacts: build generations and the signed repository-channel manifest.
package control

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

const (
	GenerationSchema = "freesense.generation/v1"
	EnvelopeSchema   = "freesense.repositories/v3"
	PayloadSchema    = "freesense.channels/v1"
	ManifestKey      = "repos.manifest.json"
)

var fingerprintPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

type Generation struct {
	SchemaVersion string `json:"schema_version"`
	Fingerprint   string `json:"fingerprint"`
	Generation    uint64 `json:"generation"`
}

type Envelope struct {
	SchemaVersion string `json:"schema_version"`
	Payload       string `json:"payload"`
	Signature     string `json:"signature"`
}

type Payload struct {
	SchemaVersion string             `json:"schema_version"`
	Channels      map[string]Channel `json:"channels"`
}

type Channel struct {
	Name         string     `json:"name"`
	Description  string     `json:"description"`
	PackageTrain string     `json:"package_train"`
	ABI          string     `json:"abi"`
	AltABI       string     `json:"altabi"`
	Default      bool       `json:"default"`
	System       *Component `json:"system,omitempty"`
	Packages     *Component `json:"packages,omitempty"`
}

type Component struct {
	Fingerprint string    `json:"fingerprint"`
	URL         string    `json:"url"`
	Generation  uint64    `json:"generation"`
	PublishedAt time.Time `json:"published_at"`
	Verified    bool      `json:"verified"`
}

type UpdateOptions struct {
	Channel      string
	Component    string
	Fingerprint  string
	URL          string
	Generation   uint64
	PackageTrain string
	ABI          string
	AltABI       string
	PublishedAt  time.Time
}

func ReserveGeneration(ctx context.Context, backend store.Backend, fingerprint string, proposed uint64) (Generation, bool, error) {
	if !fingerprintPattern.MatchString(fingerprint) || proposed == 0 {
		return Generation{}, false, errors.New("generation requires a SHA-256 fingerprint and a positive proposed value")
	}
	wanted := Generation{SchemaVersion: GenerationSchema, Fingerprint: fingerprint, Generation: proposed}
	data, err := json.Marshal(wanted)
	if err != nil {
		return Generation{}, false, err
	}
	key := "state/generations/" + fingerprint + ".json"
	_, created, err := backend.PutIfAbsent(ctx, key, store.BytesContent(append(data, '\n')))
	if err != nil {
		return Generation{}, false, err
	}
	if created {
		return wanted, true, nil
	}
	existing, err := backend.Get(ctx, key)
	if err != nil {
		return Generation{}, false, err
	}
	var got Generation
	if err := json.Unmarshal(existing.Data, &got); err != nil {
		return Generation{}, false, fmt.Errorf("decode existing generation: %w", err)
	}
	if got.SchemaVersion != GenerationSchema || got.Fingerprint != fingerprint || got.Generation == 0 {
		return Generation{}, false, errors.New("existing generation record conflicts with its fingerprint")
	}
	return got, false, nil
}

func ParsePrivateKey(data []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("signing key is not PEM")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, errors.New("signing key is neither PKCS#1 nor PKCS#8 RSA")
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("channel signing key must be RSA")
	}
	return key, nil
}

func MarshalSigned(payload Payload, privateKey *rsa.PrivateKey) ([]byte, error) {
	if privateKey == nil {
		return nil, errors.New("private signing key is required")
	}
	payload.SchemaVersion = PayloadSchema
	if payload.Channels == nil {
		payload.Channels = map[string]Channel{}
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(raw)
	signature, err := rsa.SignPKCS1v15(rand.Reader, privateKey, crypto.SHA256, digest[:])
	if err != nil {
		return nil, err
	}
	envelope := Envelope{
		SchemaVersion: EnvelopeSchema,
		Payload:       base64.StdEncoding.EncodeToString(raw),
		Signature:     base64.StdEncoding.EncodeToString(signature),
	}
	encoded, err := json.MarshalIndent(envelope, "", "  ")
	return append(encoded, '\n'), err
}

func ParseSigned(data []byte, publicKey *rsa.PublicKey) (Payload, error) {
	if publicKey == nil {
		return Payload{}, errors.New("public verification key is required")
	}
	var envelope Envelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		return Payload{}, fmt.Errorf("decode channel envelope: %w", err)
	}
	if envelope.SchemaVersion != EnvelopeSchema {
		return Payload{}, fmt.Errorf("unsupported channel envelope %q", envelope.SchemaVersion)
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(envelope.Payload)
	if err != nil {
		return Payload{}, errors.New("channel payload is not canonical base64")
	}
	signature, err := base64.StdEncoding.Strict().DecodeString(envelope.Signature)
	if err != nil {
		return Payload{}, errors.New("channel signature is not canonical base64")
	}
	digest := sha256.Sum256(raw)
	if err := rsa.VerifyPKCS1v15(publicKey, crypto.SHA256, digest[:], signature); err != nil {
		return Payload{}, errors.New("channel manifest signature is invalid")
	}
	var payload Payload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return Payload{}, fmt.Errorf("decode signed channel payload: %w", err)
	}
	if payload.SchemaVersion != PayloadSchema || payload.Channels == nil {
		return Payload{}, errors.New("signed channel payload has an unsupported schema")
	}
	return payload, nil
}

func Update(payload Payload, options UpdateOptions) (Payload, error) {
	if options.Channel != "devel" {
		return Payload{}, errors.New("build publication may update only devel")
	}
	if options.Component != "system" && options.Component != "packages" {
		return Payload{}, errors.New("component must be system or packages")
	}
	if !fingerprintPattern.MatchString(options.Fingerprint) || options.Generation == 0 {
		return Payload{}, errors.New("component identity is invalid")
	}
	parsedURL, err := url.Parse(options.URL)
	if err != nil || parsedURL.Scheme != "https" || parsedURL.Host != "pkg.freesense.org" || !strings.HasPrefix(parsedURL.Path, "/v1/artifacts/") {
		return Payload{}, errors.New("component URL must be an immutable pkg.freesense.org artifact URL")
	}
	if !regexp.MustCompile(`^[0-9]+\.[0-9]+$`).MatchString(options.PackageTrain) || options.ABI == "" || options.AltABI == "" {
		return Payload{}, errors.New("package train and ABI fields are required")
	}
	if options.PublishedAt.IsZero() {
		return Payload{}, errors.New("published time is required")
	}
	if payload.Channels == nil {
		payload.Channels = map[string]Channel{}
	}
	channel := payload.Channels[options.Channel]
	channel.Name = "devel"
	channel.Description = "Development version"
	channel.PackageTrain = options.PackageTrain
	channel.ABI = options.ABI
	channel.AltABI = options.AltABI
	channel.Default = true
	component := &Component{
		Fingerprint: options.Fingerprint,
		URL:         strings.TrimSuffix(options.URL, "/"),
		Generation:  options.Generation,
		PublishedAt: options.PublishedAt.UTC(),
		Verified:    false,
	}
	if options.Component == "system" {
		channel.System = component
	} else {
		channel.Packages = component
	}
	payload.SchemaVersion = PayloadSchema
	payload.Channels[options.Channel] = channel
	return payload, nil
}

func Verify(payload Payload, component, fingerprint string) (Payload, error) {
	channel, ok := payload.Channels["devel"]
	if !ok {
		return Payload{}, errors.New("devel channel does not exist")
	}
	target, err := componentOf(&channel, component)
	if err != nil {
		return Payload{}, err
	}
	if target == nil || target.Fingerprint != fingerprint {
		return Payload{}, errors.New("verification target is no longer current")
	}
	target.Verified = true
	payload.Channels["devel"] = channel
	return payload, nil
}

func Promote(payload Payload, component string, now time.Time, soak time.Duration) (Payload, error) {
	devel, ok := payload.Channels["devel"]
	if !ok {
		return Payload{}, errors.New("devel channel does not exist")
	}
	target, err := componentOf(&devel, component)
	if err != nil {
		return Payload{}, err
	}
	if target == nil || !target.Verified {
		return Payload{}, errors.New("current devel component has not passed integration verification")
	}
	if now.UTC().Before(target.PublishedAt.Add(soak)) {
		return Payload{}, errors.New("current devel component has not completed its soak")
	}
	stable := payload.Channels["stable"]
	stable.Name = "stable"
	stable.Description = "Stable version"
	stable.PackageTrain = devel.PackageTrain
	stable.ABI = devel.ABI
	stable.AltABI = devel.AltABI
	stable.Default = false
	copy := *target
	if component == "system" {
		stable.System = &copy
	} else {
		stable.Packages = &copy
	}
	payload.Channels["stable"] = stable
	return payload, nil
}

func componentOf(channel *Channel, component string) (*Component, error) {
	switch component {
	case "system":
		return channel.System, nil
	case "packages":
		return channel.Packages, nil
	default:
		return nil, errors.New("component must be system or packages")
	}
}
