package store

import (
	"context"
	"errors"
	"testing"
)

func TestLocalConditionalOperations(t *testing.T) {
	ctx := context.Background()
	local, err := NewLocal(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	first := BytesContent([]byte("first"))
	info, created, err := local.PutIfAbsent(ctx, "objects/test", first)
	if err != nil || !created {
		t.Fatalf("first PutIfAbsent() = (%+v, %v, %v)", info, created, err)
	}
	if info.SHA256 != first.SHA256 {
		t.Fatalf("stored SHA-256 = %q, want %q", info.SHA256, first.SHA256)
	}
	_, created, err = local.PutIfAbsent(ctx, "objects/test", BytesContent([]byte("second")))
	if err != nil || created {
		t.Fatalf("second PutIfAbsent() created=%v err=%v", created, err)
	}
	if _, err := local.CompareAndSwap(ctx, "objects/test", "wrong", first); !errors.Is(err, ErrPrecondition) {
		t.Fatalf("CompareAndSwap wrong ETag error = %v", err)
	}
	second := BytesContent([]byte("second"))
	_, err = local.CompareAndSwap(ctx, "objects/test", info.ETag, second)
	if err != nil {
		t.Fatal(err)
	}
	object, err := local.Get(ctx, "objects/test")
	if err != nil {
		t.Fatal(err)
	}
	if string(object.Data) != "second" || object.SHA256 != second.SHA256 {
		t.Fatalf("object after CompareAndSwap = %#v", object)
	}
}

func TestValidateKey(t *testing.T) {
	for _, bad := range []string{"", "/root", "../escape", "a/../b", `a\b`} {
		if err := ValidateKey(bad); err == nil {
			t.Errorf("ValidateKey(%q) unexpectedly succeeded", bad)
		}
	}
}
