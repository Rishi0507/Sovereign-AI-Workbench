// Package udsserver serves HTTP on a Unix domain socket (or, where the client platform lacks
// AF_UNIX, on an ephemeral loopback port protected by a bearer token), with request IDs, JSON
// errors and graceful shutdown.
package udsserver

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// Options configures a listener.
type Options struct {
	Name      string // service name, used for the .addr and .token files in tcp mode
	Socket    string // Unix socket path
	Transport string // "unix" or "tcp"
	RunDir    string // folder for .addr and .token files in tcp mode
}

// Listener is a bound listener plus the token clients must present in tcp mode.
type Listener struct {
	net.Listener
	Token   string
	Address string
	opts    Options
}

// Listen binds the socket, removing a stale socket file first and restricting permissions.
func Listen(opts Options) (*Listener, error) {
	switch opts.Transport {
	case "", "unix":
		if err := os.MkdirAll(filepath.Dir(opts.Socket), 0o750); err != nil {
			return nil, err
		}
		if fi, err := os.Lstat(opts.Socket); err == nil {
			if fi.Mode()&os.ModeSocket == 0 && fi.Mode().IsRegular() && fi.Size() > 0 {
				return nil, fmt.Errorf("%s exists and is not a socket", opts.Socket)
			}
			if err := os.Remove(opts.Socket); err != nil {
				return nil, fmt.Errorf("remove stale socket: %w", err)
			}
		}
		ln, err := net.Listen("unix", opts.Socket)
		if err != nil {
			return nil, err
		}
		if err := os.Chmod(opts.Socket, 0o660); err != nil {
			ln.Close()
			return nil, err
		}
		return &Listener{Listener: ln, Address: opts.Socket, opts: opts}, nil
	case "tcp":
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return nil, err
		}
		buf := make([]byte, 24)
		if _, err := rand.Read(buf); err != nil {
			ln.Close()
			return nil, err
		}
		token := hex.EncodeToString(buf)
		if err := os.MkdirAll(opts.RunDir, 0o750); err != nil {
			ln.Close()
			return nil, err
		}
		if err := writeFile(filepath.Join(opts.RunDir, opts.Name+".token"), token); err != nil {
			ln.Close()
			return nil, err
		}
		if err := writeFile(filepath.Join(opts.RunDir, opts.Name+".addr"), ln.Addr().String()); err != nil {
			ln.Close()
			return nil, err
		}
		return &Listener{Listener: ln, Token: token, Address: ln.Addr().String(), opts: opts}, nil
	default:
		return nil, fmt.Errorf("unknown transport %q", opts.Transport)
	}
}

func writeFile(path, value string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(value), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Cleanup removes the socket or the address and token files.
func (l *Listener) Cleanup() {
	if l.opts.Transport == "tcp" {
		os.Remove(filepath.Join(l.opts.RunDir, l.opts.Name+".addr"))
		os.Remove(filepath.Join(l.opts.RunDir, l.opts.Name+".token"))
		return
	}
	os.Remove(l.opts.Socket)
}

type ctxKey struct{}

// RequestID returns the request id attached by the middleware.
func RequestID(ctx context.Context) string {
	id, _ := ctx.Value(ctxKey{}).(string)
	return id
}

var counter atomic.Uint64

// Middleware adds request ids, optional bearer-token checks and access logging.
func Middleware(log *slog.Logger, token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := fmt.Sprintf("req-%d-%d", time.Now().UnixNano()%1e9, counter.Add(1))
		w.Header().Set("X-Request-Id", id)
		if token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
				WriteError(w, http.StatusUnauthorized, "unauthorized", "missing or wrong bearer token")
				return
			}
		}
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r.WithContext(context.WithValue(r.Context(), ctxKey{}, id)))
		log.Info("request", "id", id, "method", r.Method, "path", r.URL.Path, "status", rec.status,
			"ms", time.Since(start).Milliseconds())
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// WriteJSON writes v with the given status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// ErrorBody is the JSON error shape: {"error": {"code": ..., "message": ...}}.
type ErrorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
	RetryAfterS int `json:"retry_after_s,omitempty"`
}

// WriteError writes a JSON error.
func WriteError(w http.ResponseWriter, status int, code, message string) {
	var body ErrorBody
	body.Error.Code = code
	body.Error.Message = message
	WriteJSON(w, status, body)
}

// Serve runs the server until SIGINT/SIGTERM or ctx is done, then drains for up to 10 s.
func Serve(ctx context.Context, log *slog.Logger, ln *Listener, handler http.Handler) error {
	srv := &http.Server{
		Handler:           Middleware(log, ln.Token, handler),
		ReadHeaderTimeout: 10 * time.Second,
	}
	ctx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	log.Info("listening", "address", ln.Address, "transport", ln.opts.Transport)
	select {
	case err := <-errc:
		ln.Cleanup()
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
	}
	log.Info("shutting down")
	drain, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := srv.Shutdown(drain)
	ln.Cleanup()
	return err
}
