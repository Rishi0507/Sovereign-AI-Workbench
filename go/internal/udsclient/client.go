// Package udsclient is the only HTTP client in the Go services. Its transport always dials the
// configured local endpoint (a Unix socket, or a loopback address read from a run file) and
// ignores the host in the request URL, so it cannot reach the network.
package udsclient

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Endpoint names a local service.
type Endpoint struct {
	Name      string
	Socket    string
	Transport string
	RunDir    string
}

// FromSocket derives an endpoint from a socket path such as run/egressd.sock.
func FromSocket(socket, transport, runDir string) Endpoint {
	name := strings.TrimSuffix(filepath.Base(socket), ".sock")
	if runDir == "" {
		runDir = filepath.Dir(socket)
	}
	return Endpoint{Name: name, Socket: socket, Transport: transport, RunDir: runDir}
}

func (e Endpoint) target() (network, address, token string, err error) {
	if e.Transport == "" || e.Transport == "unix" {
		return "unix", e.Socket, "", nil
	}
	raw, err := os.ReadFile(filepath.Join(e.RunDir, e.Name+".addr"))
	if err != nil {
		return "", "", "", fmt.Errorf("%s is not running: %w", e.Name, err)
	}
	addr := strings.TrimSpace(string(raw))
	ap, err := netip.ParseAddrPort(addr)
	if err != nil || !ap.Addr().IsLoopback() {
		return "", "", "", fmt.Errorf("%s address %q is not loopback", e.Name, addr)
	}
	tok, _ := os.ReadFile(filepath.Join(e.RunDir, e.Name+".token"))
	return "tcp", addr, strings.TrimSpace(string(tok)), nil
}

type transport struct {
	base *http.Transport
	ep   Endpoint
}

func (t transport) RoundTrip(r *http.Request) (*http.Response, error) {
	_, _, token, err := t.ep.target()
	if err != nil {
		return nil, err
	}
	r = r.Clone(r.Context())
	r.URL.Scheme = "http"
	r.URL.Host = "local"
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	return t.base.RoundTrip(r)
}

// New returns an HTTP client bound to the endpoint.
func New(ep Endpoint, timeout time.Duration) *http.Client {
	dialer := &net.Dialer{Timeout: 5 * time.Second}
	base := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			network, address, _, err := ep.target()
			if err != nil {
				return nil, err
			}
			if network != "unix" && network != "tcp" {
				return nil, errors.New("unsupported network")
			}
			return dialer.DialContext(ctx, network, address)
		},
		MaxIdleConns:        4,
		IdleConnTimeout:     30 * time.Second,
		DisableCompression:  true,
		TLSHandshakeTimeout: time.Second,
	}
	return &http.Client{Timeout: timeout, Transport: transport{base: base, ep: ep}}
}

// URL builds a request URL for a path on the local service.
func URL(path string) string {
	return "http://local" + path
}
