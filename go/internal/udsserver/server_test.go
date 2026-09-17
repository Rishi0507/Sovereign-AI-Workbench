package udsserver_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"workbench.local/sovereign/internal/udsclient"
	"workbench.local/sovereign/internal/udsserver"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func serve(t *testing.T, opts udsserver.Options) (udsclient.Endpoint, context.CancelFunc, chan error) {
	t.Helper()
	ln, err := udsserver.Listen(opts)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		udsserver.WriteJSON(w, 200, map[string]string{"status": "ok", "id": udsserver.RequestID(r.Context())})
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- udsserver.Serve(ctx, quiet(), ln, mux) }()
	return udsclient.FromSocket(opts.Socket, opts.Transport, opts.RunDir), cancel, done
}

func check(t *testing.T, ep udsclient.Endpoint) {
	t.Helper()
	c := udsclient.New(ep, 5*time.Second)
	resp, err := c.Get(udsclient.URL("/v1/health"))
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil || body["status"] != "ok" || body["id"] == "" {
		t.Fatalf("body %v err %v", body, err)
	}
	if resp.Header.Get("X-Request-Id") == "" {
		t.Fatal("missing request id")
	}
}

func TestUnixSocketRoundTrip(t *testing.T) {
	dir := t.TempDir()
	sock := filepath.Join(dir, "svc.sock")
	if err := os.WriteFile(sock, nil, 0o600); err != nil { // an empty stale file is replaced
		t.Fatal(err)
	}
	ep, cancel, done := serve(t, udsserver.Options{Name: "svc", Socket: sock, Transport: "unix", RunDir: dir})
	check(t, ep)
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("serve: %v", err)
	}
	if _, err := os.Stat(sock); !os.IsNotExist(err) {
		t.Fatalf("socket not removed: %v", err)
	}
}

func TestTCPTokenRequired(t *testing.T) {
	dir := t.TempDir()
	ep, cancel, done := serve(t, udsserver.Options{Name: "svc", Socket: filepath.Join(dir, "svc.sock"),
		Transport: "tcp", RunDir: dir})
	defer func() { cancel(); <-done }()
	check(t, ep)
	addr, _ := os.ReadFile(filepath.Join(dir, "svc.addr"))
	resp, err := http.Get("http://" + string(addr) + "/v1/health") //nolint:noctx // test-only loopback probe
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status %d, want 401", resp.StatusCode)
	}
}

func TestListenErrors(t *testing.T) {
	dir := t.TempDir()
	if _, err := udsserver.Listen(udsserver.Options{Transport: "carrier-pigeon"}); err == nil {
		t.Fatal("expected unknown transport error")
	}
	full := filepath.Join(dir, "data.sock")
	if err := os.WriteFile(full, []byte("important"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := udsserver.Listen(udsserver.Options{Socket: full, Transport: "unix"}); err == nil {
		t.Fatal("expected refusal to replace a non-empty regular file")
	}
	ep := udsclient.FromSocket(filepath.Join(dir, "none.sock"), "tcp", dir)
	if _, err := udsclient.New(ep, time.Second).Get(udsclient.URL("/x")); err == nil {
		t.Fatal("expected error for a service that is not running")
	}
	if err := os.WriteFile(filepath.Join(dir, "none.addr"), []byte("10.0.0.1:80"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := udsclient.New(ep, time.Second).Get(udsclient.URL("/x")); err == nil {
		t.Fatal("expected refusal of a non-loopback address")
	}
}

func TestWriteError(t *testing.T) {
	rec := &recorder{header: http.Header{}}
	udsserver.WriteError(rec, 400, "bad", "nope")
	var body udsserver.ErrorBody
	if err := json.Unmarshal(rec.body, &body); err != nil || body.Error.Code != "bad" || rec.status != 400 {
		t.Fatalf("got %s %d", rec.body, rec.status)
	}
}

type recorder struct {
	header http.Header
	body   []byte
	status int
}

func (r *recorder) Header() http.Header         { return r.header }
func (r *recorder) Write(b []byte) (int, error) { r.body = append(r.body, b...); return len(b), nil }
func (r *recorder) WriteHeader(code int)        { r.status = code }
