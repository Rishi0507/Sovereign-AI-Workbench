package ringbuf

import "testing"

func TestBuffer(t *testing.T) {
	b := New(5)
	b.Write([]byte("abc"))
	if b.String() != "abc" || b.Truncated() {
		t.Fatal(b.String())
	}
	b.Write([]byte("defg"))
	if b.String() != "cdefg" || !b.Truncated() || b.Total() != 7 {
		t.Fatal(b.String(), b.Total())
	}
	b.Write([]byte("0123456789"))
	if b.String() != "56789" {
		t.Fatal(b.String())
	}
	if New(0).limit != 1 {
		t.Fatal("limit must be at least one")
	}
}
