package egress

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"workbench.local/sovereign/internal/udsclient"
)

// This file holds the only IP-dialing code in the Go services (the self-test).

// Dialer opens connections.
type Dialer interface {
	DialContext(ctx context.Context, network, address string) (net.Conn, error)
}

// Resolver resolves host names.
type Resolver interface {
	LookupHost(ctx context.Context, host string) ([]string, error)
}

// SandboxRunner asks sandboxd to run the probe script.
type SandboxRunner interface {
	RunProbe(ctx context.Context, jobDir, script, runID string) (attempts int, stdout string, err error)
}

// ErrRefused is returned by the guarded dialer: the attempt is counted and no syscall is made.
var ErrRefused = errors.New("refused by the guarded dialer before any system call")

// GuardedDialer is used in dev mode. It never dials; it records a host event and refuses.
type GuardedDialer struct{ State *State }

// DialContext implements Dialer.
func (g GuardedDialer) DialContext(_ context.Context, network, address string) (net.Conn, error) {
	g.State.Record(Event{Origin: "host", Addr: address, PID: 0, Source: "selftest-guard"})
	return nil, fmt.Errorf("%s %s: %w", network, address, ErrRefused)
}

// RealDialer returns the dialer used in enforced mode.
func RealDialer() Dialer { return &net.Dialer{Timeout: 5 * time.Second} }

// SimulatedResolver is used in dev mode: no real DNS lookup is ever made.
type SimulatedResolver struct{}

// LookupHost implements Resolver.
func (SimulatedResolver) LookupHost(context.Context, string) ([]string, error) {
	return nil, errors.New("no nameserver configured (simulated)")
}

// RealResolver returns the pure-Go resolver used in enforced mode.
func RealResolver() Resolver { return &net.Resolver{PreferGo: true} }

// Check is one self-test result.
type Check struct {
	Name           string         `json:"name"`
	Expected       string         `json:"expected"`
	Observed       string         `json:"observed"`
	Simulated      bool           `json:"simulated,omitempty"`
	CountersBefore map[string]any `json:"counters_before"`
	CountersAfter  map[string]any `json:"counters_after"`
	Pass           bool           `json:"pass"`
}

// Result is the POST /v1/test response.
type Result struct {
	Mode   string  `json:"mode"`
	Checks []Check `json:"checks"`
	Pass   bool    `json:"pass"`
	Breach bool    `json:"breach"`
}

// Tester runs the three-part egress test.
type Tester struct {
	Mode     string
	State    *State
	Dialer   Dialer
	Resolver Resolver
	Sandbox  SandboxRunner
	ProbeDir string
	Wait     time.Duration
	Target   string
	Host     string
}

func counters(st *State) map[string]any {
	h, s, p := st.Counters()
	out := map[string]any{"blocked_connect_host": h, "blocked_connect_sandbox": s, "blocked_packets": nil}
	if p != nil {
		out["blocked_packets"] = *p
	}
	return out
}

func asInt(v any) int64 {
	n, _ := v.(int64)
	return n
}

func (t *Tester) waitFor(ctx context.Context, cond func() bool) bool {
	deadline := time.Now().Add(t.Wait)
	for {
		if cond() {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(100 * time.Millisecond):
		}
	}
}

// Run executes host_raw_ip, host_dns and sandbox.
func (t *Tester) Run(ctx context.Context) Result {
	if t.Target == "" {
		t.Target = "1.1.1.1:443"
	}
	if t.Host == "" {
		t.Host = "example.com"
	}
	res := Result{Mode: t.Mode}
	res.Checks = append(res.Checks, t.hostRawIP(ctx, &res), t.hostDNS(ctx), t.sandbox(ctx))
	res.Pass = true
	for _, c := range res.Checks {
		res.Pass = res.Pass && c.Pass
	}
	return res
}

func (t *Tester) hostRawIP(ctx context.Context, res *Result) Check {
	before := counters(t.State)
	c := Check{Name: "host_raw_ip", CountersBefore: before}
	dctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	conn, err := t.Dialer.DialContext(dctx, "tcp", t.Target)
	cancel()
	if err == nil {
		conn.Close()
		t.State.SetBreach()
		res.Breach = true
		c.Expected, c.Observed = "connection fails", "BREACH: the connection succeeded"
		c.CountersAfter = counters(t.State)
		return c
	}
	c.Observed = err.Error()
	if t.Mode == "dev" {
		c.Expected = "refused by the guarded dialer and counted as a host attempt"
		c.Pass = t.waitFor(ctx, func() bool {
			return asInt(counters(t.State)["blocked_connect_host"]) == asInt(before["blocked_connect_host"])+1
		})
	} else {
		c.Expected = "connection fails; blocked packets and host connect() both rise by one"
		c.Pass = t.waitFor(ctx, func() bool {
			now := counters(t.State)
			return asInt(now["blocked_connect_host"]) >= asInt(before["blocked_connect_host"])+1 &&
				before["blocked_packets"] != nil && now["blocked_packets"] != nil &&
				asInt(now["blocked_packets"]) >= asInt(before["blocked_packets"])+1
		})
	}
	c.CountersAfter = counters(t.State)
	return c
}

func (t *Tester) hostDNS(ctx context.Context) Check {
	before := counters(t.State)
	c := Check{Name: "host_dns", Expected: "name resolution fails and nothing leaves the host",
		CountersBefore: before, Simulated: t.Mode == "dev"}
	dctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	addrs, err := t.Resolver.LookupHost(dctx, t.Host)
	cancel()
	after := counters(t.State)
	c.CountersAfter = after
	if err == nil {
		c.Observed = fmt.Sprintf("resolved to %v", addrs)
		return c
	}
	c.Observed = err.Error()
	c.Pass = asInt(after["blocked_connect_host"]) == asInt(before["blocked_connect_host"])
	return c
}

// ProbeScript is written into the probe job directory and run by sandboxd.
const ProbeScript = `import socket
try:
    socket.create_connection(("1.1.1.1", 443), 5)
    print("connected")
except OSError as exc:
    print("blocked:", exc)
`

func (t *Tester) sandbox(ctx context.Context) Check {
	before := counters(t.State)
	c := Check{Name: "sandbox", Expected: "one recorded attempt; sandbox connect() rises by one; packets unchanged",
		CountersBefore: before}
	if t.Sandbox == nil {
		c.Observed = "no sandbox configured"
		c.CountersAfter = before
		return c
	}
	if err := os.MkdirAll(t.ProbeDir, 0o750); err != nil {
		c.Observed = err.Error()
		c.CountersAfter = before
		return c
	}
	if err := os.WriteFile(filepath.Join(t.ProbeDir, "egress_probe.py"), []byte(ProbeScript), 0o640); err != nil {
		c.Observed = err.Error()
		c.CountersAfter = before
		return c
	}
	runID := fmt.Sprintf("egress-probe-%d", time.Now().UnixNano()%1e12)
	attempts, stdout, err := t.Sandbox.RunProbe(ctx, t.ProbeDir, "egress_probe.py", runID)
	if err != nil {
		c.Observed = "sandbox run failed: " + err.Error()
		c.CountersAfter = counters(t.State)
		return c
	}
	c.Observed = fmt.Sprintf("%d attempt(s) recorded; script said %q", attempts, bytes.TrimSpace([]byte(stdout)))
	ok := t.waitFor(ctx, func() bool {
		return asInt(counters(t.State)["blocked_connect_sandbox"]) >= asInt(before["blocked_connect_sandbox"])+1
	})
	after := counters(t.State)
	c.CountersAfter = after
	packetsSame := t.Mode == "dev" || fmt.Sprint(after["blocked_packets"]) == fmt.Sprint(before["blocked_packets"])
	c.Pass = attempts == 1 && ok && packetsSame
	return c
}

// SandboxdClient runs the probe through sandboxd.
type SandboxdClient struct {
	Client *http.Client
}

// NewSandboxdClient returns a client for the sandboxd endpoint.
func NewSandboxdClient(ep udsclient.Endpoint) *SandboxdClient {
	return &SandboxdClient{Client: udsclient.New(ep, 60*time.Second)}
}

// RunProbe implements SandboxRunner.
func (s *SandboxdClient) RunProbe(ctx context.Context, jobDir, script, runID string) (int, string, error) {
	body, _ := json.Marshal(map[string]any{"run_id": runID, "job_dir": jobDir, "script": script, "timeout_s": 20,
		"label": "egress self-test"})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, udsclient.URL("/v1/run"), bytes.NewReader(body))
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.Client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	var out struct {
		StdoutTail  string            `json:"stdout_tail"`
		NetAttempts []json.RawMessage `json:"net_attempts"`
		Error       *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, "", err
	}
	if resp.StatusCode != http.StatusOK {
		msg := resp.Status
		if out.Error != nil {
			msg = out.Error.Message
		}
		return 0, "", errors.New(msg)
	}
	return len(out.NetAttempts), out.StdoutTail, nil
}
