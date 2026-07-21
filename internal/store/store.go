// Package store supplies the immutable object and compare-and-swap primitives
// required by fsbuild. Backends must make PutIfAbsent atomic.
package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"strings"
	"time"
)

var (
	ErrNotFound     = errors.New("object not found")
	ErrPrecondition = errors.New("object precondition failed")
)

type Object struct {
	Key    string
	Data   []byte
	Size   int64
	ETag   string
	SHA256 string
}

type ObjectInfo struct {
	Key          string
	Size         int64
	ETag         string
	SHA256       string
	LastModified time.Time
}

// Lister is implemented by stores that support reachability collection.
// Keeping it separate from Backend lets small test doubles remain minimal.
type Lister interface {
	List(ctx context.Context, prefix string) ([]ObjectInfo, error)
}

// GetURLSigner is implemented by remote stores that can grant a short-lived,
// read-only URL for exactly one object. The URL is deliberately not persisted:
// callers pass it directly to the service that consumes the object.
type GetURLSigner interface {
	PresignGet(key string, validity time.Duration) (string, error)
}

// Content is replayable so an HTTP backend can create a fresh request body.
// SHA256 is lowercase hexadecimal without a prefix.
type Content struct {
	Size   int64
	SHA256 string
	Open   func() (io.ReadCloser, error)
}

func BytesContent(data []byte) Content {
	owned := append([]byte(nil), data...)
	sum := sha256.Sum256(owned)
	return Content{
		Size:   int64(len(owned)),
		SHA256: hex.EncodeToString(sum[:]),
		Open: func() (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(owned)), nil
		},
	}
}

func FileContent(filename string) (Content, error) {
	file, err := os.Open(filename)
	if err != nil {
		return Content{}, fmt.Errorf("open content file: %w", err)
	}
	info, err := file.Stat()
	if err != nil {
		_ = file.Close()
		return Content{}, fmt.Errorf("stat content file: %w", err)
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		_ = file.Close()
		return Content{}, fmt.Errorf("hash content file: %w", err)
	}
	if err := file.Close(); err != nil {
		return Content{}, fmt.Errorf("close content file: %w", err)
	}
	return Content{
		Size:   info.Size(),
		SHA256: hex.EncodeToString(hash.Sum(nil)),
		Open: func() (io.ReadCloser, error) {
			return os.Open(filename)
		},
	}, nil
}

func (content Content) Validate() error {
	if content.Size < 0 || content.Open == nil {
		return errors.New("content requires non-negative size and opener")
	}
	if !validSHA256(content.SHA256) {
		return errors.New("content requires a lowercase hexadecimal SHA-256")
	}
	return nil
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

type Backend interface {
	Get(ctx context.Context, key string) (Object, error)
	Head(ctx context.Context, key string) (ObjectInfo, error)
	PutIfAbsent(ctx context.Context, key string, content Content) (ObjectInfo, bool, error)
	CompareAndSwap(ctx context.Context, key, expectedETag string, content Content) (ObjectInfo, error)
	DeleteIfMatch(ctx context.Context, key, expectedETag string) error
}

// ArtifactReader is implemented by backends that need different compatibility
// handling for immutable build artifacts. Backend.Get and Backend.Head remain
// strict because input and control objects rely on fsbuild-owned metadata.
type ArtifactReader interface {
	GetArtifact(ctx context.Context, key string) (Object, error)
	HeadArtifact(ctx context.Context, key string) (ObjectInfo, error)
}

// GetArtifact reads an immutable build result. Backends without a distinct
// artifact path retain their normal verified Get behavior.
func GetArtifact(ctx context.Context, backend Backend, key string) (Object, error) {
	if reader, ok := backend.(ArtifactReader); ok {
		return reader.GetArtifact(ctx, key)
	}
	return backend.Get(ctx, key)
}

// HeadArtifact inspects an immutable build result. A remote artifact backend
// may return an empty SHA256 when the uploader did not attach digest metadata.
func HeadArtifact(ctx context.Context, backend Backend, key string) (ObjectInfo, error) {
	if reader, ok := backend.(ArtifactReader); ok {
		return reader.HeadArtifact(ctx, key)
	}
	return backend.Head(ctx, key)
}

func ValidateKey(key string) error {
	if key == "" || strings.HasPrefix(key, "/") || strings.Contains(key, `\`) {
		return fmt.Errorf("invalid object key %q", key)
	}
	cleaned := path.Clean(key)
	if cleaned != key || cleaned == "." || cleaned == ".." || strings.HasPrefix(cleaned, "../") {
		return fmt.Errorf("invalid object key %q", key)
	}
	return nil
}
