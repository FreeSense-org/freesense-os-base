package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

type Local struct {
	root string
}

func NewLocal(root string) (*Local, error) {
	if root == "" {
		return nil, errors.New("local store root is required")
	}
	absolute, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve local store root: %w", err)
	}
	if err := os.MkdirAll(absolute, 0o755); err != nil {
		return nil, fmt.Errorf("create local store root: %w", err)
	}
	if err := os.MkdirAll(filepath.Join(absolute, ".fsbuild-locks"), 0o755); err != nil {
		return nil, fmt.Errorf("create local lock root: %w", err)
	}
	return &Local{root: absolute}, nil
}

func (local *Local) Get(ctx context.Context, key string) (Object, error) {
	filename, err := local.filename(key)
	if err != nil {
		return Object{}, err
	}
	if err := ctx.Err(); err != nil {
		return Object{}, err
	}
	data, err := os.ReadFile(filename)
	if errors.Is(err, os.ErrNotExist) {
		return Object{}, ErrNotFound
	}
	if err != nil {
		return Object{}, fmt.Errorf("read object %q: %w", key, err)
	}
	digest := etag(data)
	return Object{
		Key: key, Data: data, Size: int64(len(data)), ETag: digest, SHA256: digest,
	}, nil
}

func (local *Local) Head(ctx context.Context, key string) (ObjectInfo, error) {
	filename, err := local.filename(key)
	if err != nil {
		return ObjectInfo{}, err
	}
	if err := ctx.Err(); err != nil {
		return ObjectInfo{}, err
	}
	file, err := os.Open(filename)
	if errors.Is(err, os.ErrNotExist) {
		return ObjectInfo{}, ErrNotFound
	}
	if err != nil {
		return ObjectInfo{}, fmt.Errorf("open object %q: %w", key, err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return ObjectInfo{}, fmt.Errorf("stat object %q: %w", key, err)
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return ObjectInfo{}, fmt.Errorf("hash object %q: %w", key, err)
	}
	return ObjectInfo{
		Key: key, Size: info.Size(), ETag: hex.EncodeToString(hash.Sum(nil)),
		SHA256: hex.EncodeToString(hash.Sum(nil)),
	}, nil
}

func (local *Local) PutIfAbsent(ctx context.Context, key string, content Content) (ObjectInfo, bool, error) {
	if err := content.Validate(); err != nil {
		return ObjectInfo{}, false, err
	}
	filename, err := local.filename(key)
	if err != nil {
		return ObjectInfo{}, false, err
	}
	unlock, err := local.lock(ctx, key)
	if err != nil {
		return ObjectInfo{}, false, err
	}
	defer unlock()

	info, err := local.Head(ctx, key)
	if err == nil {
		return info, false, nil
	}
	if !errors.Is(err, ErrNotFound) {
		return ObjectInfo{}, false, err
	}
	if err := local.writeAtomic(filename, content); err != nil {
		return ObjectInfo{}, false, err
	}
	return ObjectInfo{
		Key: key, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256,
	}, true, nil
}

func (local *Local) CompareAndSwap(ctx context.Context, key, expectedETag string, content Content) (ObjectInfo, error) {
	if expectedETag == "" {
		return ObjectInfo{}, errors.New("expected ETag is required")
	}
	if err := content.Validate(); err != nil {
		return ObjectInfo{}, err
	}
	filename, err := local.filename(key)
	if err != nil {
		return ObjectInfo{}, err
	}
	unlock, err := local.lock(ctx, key)
	if err != nil {
		return ObjectInfo{}, err
	}
	defer unlock()

	current, err := local.Head(ctx, key)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return ObjectInfo{}, ErrPrecondition
		}
		return ObjectInfo{}, err
	}
	if current.ETag != expectedETag {
		return ObjectInfo{}, ErrPrecondition
	}
	if err := local.writeAtomic(filename, content); err != nil {
		return ObjectInfo{}, err
	}
	return ObjectInfo{
		Key: key, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256,
	}, nil
}

func (local *Local) filename(key string) (string, error) {
	if err := ValidateKey(key); err != nil {
		return "", err
	}
	return filepath.Join(local.root, filepath.FromSlash(key)), nil
}

func (local *Local) writeAtomic(filename string, content Content) error {
	if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
		return fmt.Errorf("create object directory: %w", err)
	}
	source, err := content.Open()
	if err != nil {
		return fmt.Errorf("open object content: %w", err)
	}
	defer source.Close()
	temp, err := os.CreateTemp(filepath.Dir(filename), ".fsbuild-object-*")
	if err != nil {
		return fmt.Errorf("create temporary object: %w", err)
	}
	tempName := temp.Name()
	defer os.Remove(tempName)

	hash := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(temp, hash), source)
	syncErr := temp.Sync()
	closeErr := temp.Close()
	if copyErr != nil {
		return fmt.Errorf("write temporary object: %w", copyErr)
	}
	if syncErr != nil {
		return fmt.Errorf("sync temporary object: %w", syncErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close temporary object: %w", closeErr)
	}
	if written != content.Size || hex.EncodeToString(hash.Sum(nil)) != content.SHA256 {
		return errors.New("content changed between hashing and object write")
	}
	if err := os.Rename(tempName, filename); err != nil {
		return fmt.Errorf("commit object: %w", err)
	}
	return nil
}

func (local *Local) lock(ctx context.Context, key string) (func(), error) {
	sum := sha256.Sum256([]byte(key))
	lockName := filepath.Join(local.root, ".fsbuild-locks", hex.EncodeToString(sum[:])+".lock")
	for {
		file, err := os.OpenFile(lockName, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err == nil {
			_, _ = fmt.Fprintf(file, "%d\n", time.Now().UTC().Unix())
			_ = file.Close()
			return func() { _ = os.Remove(lockName) }, nil
		}
		if !errors.Is(err, os.ErrExist) {
			return nil, fmt.Errorf("acquire object lock: %w", err)
		}
		if info, statErr := os.Stat(lockName); statErr == nil && time.Since(info.ModTime()) > time.Hour {
			_ = os.Remove(lockName)
			continue
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(25 * time.Millisecond):
		}
	}
}

func etag(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
