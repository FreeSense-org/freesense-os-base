package control

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"errors"
	"testing"
	"time"

	"github.com/FreeSense-org/freesense-os-base/internal/store"
)

type memoryStore struct{ objects map[string]store.Object }

func newMemoryStore() *memoryStore { return &memoryStore{objects: map[string]store.Object{}} }
func (m *memoryStore) Get(_ context.Context, key string) (store.Object, error) {
	object, ok := m.objects[key]
	if !ok {
		return store.Object{}, store.ErrNotFound
	}
	return object, nil
}
func (m *memoryStore) Head(ctx context.Context, key string) (store.ObjectInfo, error) {
	object, err := m.Get(ctx, key)
	return store.ObjectInfo{Key: key, Size: object.Size, ETag: object.ETag, SHA256: object.SHA256}, err
}
func (m *memoryStore) PutIfAbsent(_ context.Context, key string, content store.Content) (store.ObjectInfo, bool, error) {
	if object, ok := m.objects[key]; ok {
		return store.ObjectInfo{Key: key, Size: object.Size, ETag: object.ETag, SHA256: object.SHA256}, false, nil
	}
	reader, _ := content.Open()
	defer reader.Close()
	data := make([]byte, content.Size)
	_, _ = reader.Read(data)
	m.objects[key] = store.Object{Key: key, Data: data, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256}
	return store.ObjectInfo{Key: key, Size: content.Size, ETag: content.SHA256, SHA256: content.SHA256}, true, nil
}
func (m *memoryStore) CompareAndSwap(context.Context, string, string, store.Content) (store.ObjectInfo, error) {
	return store.ObjectInfo{}, errors.New("unused")
}
func (m *memoryStore) DeleteIfMatch(context.Context, string, string) error {
	return errors.New("unused")
}

func TestGenerationReservationIsStableAcrossRetries(t *testing.T) {
	backend := newMemoryStore()
	fingerprint := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	first, created, err := ReserveGeneration(context.Background(), backend, fingerprint, 17)
	if err != nil || !created || first.Generation != 17 {
		t.Fatalf("first reservation: %#v %v %v", first, created, err)
	}
	second, created, err := ReserveGeneration(context.Background(), backend, fingerprint, 99)
	if err != nil || created || second.Generation != 17 {
		t.Fatalf("retry reservation: %#v %v %v", second, created, err)
	}
}

func TestSignedChannelUpdateVerifyAndPromote(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 7, 21, 12, 0, 0, 0, time.UTC)
	fingerprint := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	payload, err := Update(Payload{}, UpdateOptions{
		Channel: "devel", Component: "system", Fingerprint: fingerprint,
		URL:        "https://pkg.freesense.org/v1/artifacts/system/" + fingerprint + "/amd64",
		Generation: 4, PackageTrain: "1.1", ABI: "FreeBSD:16:amd64", AltABI: "freebsd:16:x86:64", PublishedAt: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := MarshalSigned(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := ParseSigned(encoded, &key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Promote(decoded, "system", now.Add(8*24*time.Hour), 7*24*time.Hour); err == nil {
		t.Fatal("unverified component promoted")
	}
	decoded, err = Verify(decoded, "system", fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err = Promote(decoded, "system", now.Add(8*24*time.Hour), 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if decoded.Channels["stable"].System.Fingerprint != fingerprint {
		t.Fatal("stable did not receive exact tested system")
	}
}

func TestManifestRejectsTampering(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	encoded, _ := MarshalSigned(Payload{}, key)
	encoded[len(encoded)-3] ^= 1
	if _, err := ParseSigned(encoded, &key.PublicKey); err == nil {
		t.Fatal("tampered envelope accepted")
	}
}
