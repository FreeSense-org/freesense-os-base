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
	PayloadSchema    = "freesense.channels/v3"
	ManifestKey      = "repos.manifest.json"
)

var fingerprintPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var releaseVersionPattern = regexp.MustCompile(`^([0-9]+)\.([0-9]+)\.([0-9]+)$`)

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
	Version      string     `json:"version"`
	PackageTrain string     `json:"package_train"`
	ABI          string     `json:"abi"`
	AltABI       string     `json:"altabi"`
	Default      bool       `json:"default"`
	System       *Component `json:"system,omitempty"`
	Packages     *Component `json:"packages,omitempty"`
}

type Component struct {
	Fingerprint        string    `json:"fingerprint"`
	SystemFingerprint  string    `json:"system_fingerprint,omitempty"`
	FreeBSDPinID       string    `json:"freebsd_pin_id"`
	BuiltAgainstSystem string    `json:"built_against_system,omitempty"`
	URL                string    `json:"url"`
	Generation         uint64    `json:"generation"`
	PublishedAt        time.Time `json:"published_at"`
	Verified           bool      `json:"verified"`
}

type UpdateOptions struct {
	Channel     string
	Component   string
	Fingerprint string
	// SystemFingerprint binds a packages component to the exact system it was
	// built and tested against. It must be empty for system publications.
	SystemFingerprint string
	FreeBSDPinID      string
	URL               string
	Generation        uint64
	Version           string
	PackageTrain      string
	ABI               string
	AltABI            string
	PublishedAt       time.Time
}

// SealStable publishes one complete immutable 1.0.x System/Packages pair. A
// later call may advance stable to a strictly newer patch, but can never rewrite
// a published version or move the channel backwards.
func SealStable(payload Payload, system, packages UpdateOptions) (Payload, error) {
	if system.Component != "system" || packages.Component != "packages" {
		return Payload{}, errors.New("stable release requires System and Packages components")
	}
	if system.PackageTrain != "1.0" || packages.PackageTrain != "1.0" {
		return Payload{}, errors.New("stable release must use package train 1.0")
	}
	if system.Version != packages.Version || !strings.HasPrefix(system.Version, "1.0.") ||
		!releaseVersionPattern.MatchString(system.Version) {
		return Payload{}, errors.New("stable release version must be an exact matching 1.0.x version")
	}
	if system.FreeBSDPinID != packages.FreeBSDPinID ||
		packages.SystemFingerprint != system.Fingerprint {
		return Payload{}, errors.New("sealed stable release has incompatible component bindings")
	}
	// An older channel cannot be mixed into the v3 envelope. Stable 1.0 is the
	// first v3 release, so replace the legacy development selection rather than
	// carrying unsigned compatibility assumptions into the new schema.
	if payload.SchemaVersion == "freesense.channels/v1" || payload.SchemaVersion == "freesense.channels/v2" {
		payload = Payload{SchemaVersion: PayloadSchema, Channels: map[string]Channel{}}
	}
	for _, channel := range payload.Channels {
		if channel.System != nil && !fingerprintPattern.MatchString(channel.System.FreeBSDPinID) {
			return Payload{}, errors.New("existing channels must migrate to FreeBSD pin identities before sealing stable")
		}
		if channel.Packages != nil &&
			(!fingerprintPattern.MatchString(channel.Packages.FreeBSDPinID) ||
				!fingerprintPattern.MatchString(channel.Packages.BuiltAgainstSystem)) {
			return Payload{}, errors.New("existing channels must migrate package compatibility before sealing stable")
		}
	}
	working := Payload{Channels: map[string]Channel{}}
	var err error
	working, err = Update(working, system)
	if err != nil {
		return Payload{}, err
	}
	working, err = Verify(working, "system", system.Fingerprint)
	if err != nil {
		return Payload{}, err
	}
	working, err = Update(working, packages)
	if err != nil {
		return Payload{}, err
	}
	working, err = Verify(working, "packages", packages.Fingerprint)
	if err != nil {
		return Payload{}, err
	}
	desired := working.Channels["devel"]
	desired.Name = "stable"
	desired.Description = "FreeSense " + system.Version + " stable"

	if payload.Channels == nil {
		payload.Channels = map[string]Channel{}
	}
	if existing, ok := payload.Channels["stable"]; ok {
		desired.Default = existing.Default
		if existing.Version == desired.Version && existing.Name == desired.Name && existing.Description == desired.Description &&
			existing.PackageTrain == desired.PackageTrain && existing.ABI == desired.ABI &&
			existing.AltABI == desired.AltABI && existing.Default == desired.Default &&
			componentIdentityEqual(existing.System, desired.System) &&
			componentIdentityEqual(existing.Packages, desired.Packages) &&
			existing.System.Verified && existing.Packages.Verified {
			return payload, nil
		}
		if existing.Version == desired.Version {
			return Payload{}, errors.New("an immutable stable version cannot be rewritten")
		}
		if !stablePatchAdvances(existing.Version, desired.Version) {
			return Payload{}, errors.New("stable release must advance to a newer 1.0 patch")
		}
	}
	desired.Default = true
	for _, channel := range payload.Channels {
		if channel.Default {
			desired.Default = false
			break
		}
	}
	payload.SchemaVersion = PayloadSchema
	payload.Channels["stable"] = desired
	return payload, nil
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
	if (payload.SchemaVersion != PayloadSchema && payload.SchemaVersion != "freesense.channels/v2" && payload.SchemaVersion != "freesense.channels/v1") || payload.Channels == nil {
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
	if !fingerprintPattern.MatchString(options.FreeBSDPinID) {
		return Payload{}, errors.New("component requires an exact FreeBSD pin identity")
	}
	if options.Component == "packages" && !fingerprintPattern.MatchString(options.SystemFingerprint) {
		return Payload{}, errors.New("packages publication requires an exact system fingerprint binding")
	}
	if options.Component == "system" && options.SystemFingerprint != "" {
		return Payload{}, errors.New("system publication must not have a system fingerprint binding")
	}
	if !regexp.MustCompile(`^[0-9]+\.[0-9]+$`).MatchString(options.PackageTrain) || options.ABI == "" || options.AltABI == "" {
		return Payload{}, errors.New("package train and ABI fields are required")
	}
	if !releaseVersionPattern.MatchString(options.Version) || !strings.HasPrefix(options.Version, options.PackageTrain+".") {
		return Payload{}, errors.New("release version must be exact and belong to the package train")
	}
	expectedPath := fmt.Sprintf("/v1/artifacts/system/%s/amd64", options.Fingerprint)
	if options.Component == "packages" {
		expectedPath = fmt.Sprintf("/v1/artifacts/packages/%s/%s/amd64", options.PackageTrain, options.Fingerprint)
	}
	parsedURL, err := url.Parse(options.URL)
	if err != nil || parsedURL.Scheme != "https" || parsedURL.Host != "pkg.freesense.org" ||
		parsedURL.Path != expectedPath || parsedURL.RawQuery != "" || parsedURL.Fragment != "" || parsedURL.User != nil {
		return Payload{}, errors.New("component URL must exactly match its immutable pkg.freesense.org artifact")
	}
	if options.PublishedAt.IsZero() {
		return Payload{}, errors.New("published time is required")
	}
	if payload.Channels == nil {
		payload.Channels = map[string]Channel{}
	}
	channel := payload.Channels[options.Channel]
	if options.Component == "packages" {
		if channel.System == nil || channel.System.Fingerprint != options.SystemFingerprint ||
			channel.System.FreeBSDPinID != options.FreeBSDPinID {
			return Payload{}, errors.New("packages publication is not bound to the current devel system")
		}
	}
	artifactURL := strings.TrimSuffix(options.URL, "/")
	if channelMetadataMatches(channel, options) {
		existing, err := componentOf(&channel, options.Component)
		if err != nil {
			return Payload{}, err
		}
		builtAgainstSystem := ""
		if options.Component == "packages" {
			builtAgainstSystem = options.SystemFingerprint
		}
		if componentIdentityMatches(existing, options.Fingerprint, options.SystemFingerprint,
			options.FreeBSDPinID, builtAgainstSystem, artifactURL, options.Generation) {
			// A retry of the same publication must not restart its soak or discard
			// successful integration verification.
			return payload, nil
		}
	}
	channel.Name = "devel"
	channel.Description = "Development version"
	channel.Version = options.Version
	channel.PackageTrain = options.PackageTrain
	channel.ABI = options.ABI
	channel.AltABI = options.AltABI
	channel.Default = true
	component := &Component{
		Fingerprint:       options.Fingerprint,
		SystemFingerprint: options.SystemFingerprint,
		FreeBSDPinID:      options.FreeBSDPinID,
		URL:               artifactURL,
		Generation:        options.Generation,
		PublishedAt:       options.PublishedAt.UTC(),
		Verified:          false,
	}
	if options.Component == "packages" {
		component.BuiltAgainstSystem = options.SystemFingerprint
	}
	if options.Component == "system" {
		previousSystem := channel.System
		previousPackages := channel.Packages
		channel.System = component
		// Optional packages are compatible with every System produced from the
		// same pinned FreeBSD closure. Keep the independently built repository and
		// move only its selected-system pointer; the original build System remains
		// recorded separately for auditability.
		if previousSystem != nil && previousPackages != nil &&
			previousPackages.FreeBSDPinID == component.FreeBSDPinID {
			copy := *previousPackages
			copy.SystemFingerprint = component.Fingerprint
			channel.Packages = &copy
		} else {
			channel.Packages = nil
		}
	} else {
		channel.Packages = component
	}
	payload.SchemaVersion = PayloadSchema
	payload.Channels[options.Channel] = channel
	// The first stable seal is initially the default. As soon as rolling 1.1 is
	// published, devel becomes the one default without changing stable artifacts.
	for name, other := range payload.Channels {
		if name != options.Channel && other.Default {
			other.Default = false
			payload.Channels[name] = other
		}
	}
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
	if component == "packages" {
		if err := validatePackageBinding(channel, target); err != nil {
			return Payload{}, err
		}
	}
	target.Verified = true
	payload.Channels["devel"] = channel
	return payload, nil
}

func Promote(payload Payload, component string, now time.Time, soak time.Duration) (Payload, error) {
	return Payload{}, errors.New("component promotion is disabled; publish a checked immutable 1.0.x pair")
}

func channelMetadataMatches(channel Channel, options UpdateOptions) bool {
	return channel.Name == "devel" &&
		channel.Description == "Development version" &&
		channel.Version == options.Version &&
		channel.PackageTrain == options.PackageTrain &&
		channel.ABI == options.ABI &&
		channel.AltABI == options.AltABI &&
		channel.Default
}

func stablePatchAdvances(current, next string) bool {
	currentMatch := releaseVersionPattern.FindStringSubmatch(current)
	nextMatch := releaseVersionPattern.FindStringSubmatch(next)
	if len(currentMatch) != 4 || len(nextMatch) != 4 || currentMatch[1] != "1" || currentMatch[2] != "0" ||
		nextMatch[1] != "1" || nextMatch[2] != "0" {
		return false
	}
	var currentPatch, nextPatch int
	if _, err := fmt.Sscanf(currentMatch[3], "%d", &currentPatch); err != nil {
		return false
	}
	if _, err := fmt.Sscanf(nextMatch[3], "%d", &nextPatch); err != nil {
		return false
	}
	return nextPatch > currentPatch
}

func componentIdentityMatches(component *Component, fingerprint, systemFingerprint, freeBSDPinID,
	builtAgainstSystem, artifactURL string, generation uint64) bool {
	return component != nil &&
		component.Fingerprint == fingerprint &&
		component.SystemFingerprint == systemFingerprint &&
		component.FreeBSDPinID == freeBSDPinID &&
		component.BuiltAgainstSystem == builtAgainstSystem &&
		component.URL == artifactURL &&
		component.Generation == generation
}

func componentIdentityEqual(left, right *Component) bool {
	return left != nil && right != nil &&
		componentIdentityMatches(left, right.Fingerprint, right.SystemFingerprint, right.FreeBSDPinID,
			right.BuiltAgainstSystem, right.URL, right.Generation)
}

func validatePackageBinding(channel Channel, packages *Component) error {
	if packages == nil || !fingerprintPattern.MatchString(packages.SystemFingerprint) ||
		!fingerprintPattern.MatchString(packages.BuiltAgainstSystem) ||
		!fingerprintPattern.MatchString(packages.FreeBSDPinID) {
		return errors.New("packages component has no valid compatibility binding")
	}
	if channel.System == nil || channel.System.Fingerprint != packages.SystemFingerprint {
		return errors.New("packages component is not bound to the channel system")
	}
	if channel.System.FreeBSDPinID != packages.FreeBSDPinID {
		return errors.New("packages component is not bound to the channel FreeBSD pin")
	}
	return nil
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
