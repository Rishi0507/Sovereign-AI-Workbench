package config

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type sample struct {
	Name string `json:"name"`
	N    int    `json:"n"`
}

func (s *sample) Validate() error {
	if s.N < 0 {
		return errors.New("n must be positive")
	}
	return nil
}

func TestDecode(t *testing.T) {
	var s sample
	if err := Decode([]byte(`{"name":"x","n":2}`), &s); err != nil || s.N != 2 {
		t.Fatalf("decode: %v %+v", err, s)
	}
	cases := map[string]string{
		`{"name":"x","extra":1}`: "unknown field",
		`{"n":-1}`:               "n must be positive",
		`{"n":1}{"n":2}`:         "trailing data",
		`{"n":`:                  "decode config",
	}
	for in, want := range cases {
		var v sample
		err := Decode([]byte(in), &v)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v, want %q", in, err, want)
		}
	}
}

func TestLoad(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "c.json")
	if err := os.WriteFile(p, []byte(`{"name":"ok","n":1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	var s sample
	if err := Load(p, &s); err != nil || s.Name != "ok" {
		t.Fatalf("load: %v", err)
	}
	if err := Load(filepath.Join(dir, "missing.json"), &s); err == nil {
		t.Fatal("expected error for a missing file")
	}
}
