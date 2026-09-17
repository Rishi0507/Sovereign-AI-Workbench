package egress

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"workbench.local/sovereign/internal/testutil"
	"workbench.local/sovereign/internal/udsclient"
	"workbench.local/sovereign/internal/udsserver"
)

func TestMain(m *testing.M) {
	testutil.MaybeRunFake()
	os.Exit(m.Run())
}

func testdata(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func policy() *Policy {
	return NewPolicy([]string{"10.20.0.0/24"}, []AllowEntry{{CIDR: "10.20.0.10/32", Port: 636, Proto: "tcp"},
		{CIDR: "10.30.0.5/32", Port: 6514}})
}

func TestConfigValidate(t *testing.T) {
	c := Config{Mode: "chaos", Transport: "x", LanCIDRs: []string{"nope"},
		Allowlist: []AllowEntry{{CIDR: "bad", Port: 0}}}
	err := c.Validate()
	for _, want := range []string{"socket", "mode", "transport", "lan_cidrs", "allowlist", "port", "probe_job_dir"} {
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("missing %q in %v", want, err)
		}
	}
	ok := Config{Socket: "/run/wb/egressd.sock", Mode: "dev", ProbeJobDir: "/srv/p", StateFile: "/run/wb/s.json"}
	if err := ok.Validate(); err != nil || ok.EventBuffer != 1000 || ok.RunDir != filepath.Dir(ok.Socket) || ok.ResolvConf == "" {
		t.Fatalf("defaults: %v %+v", err, ok)
	}
}

func TestParseNft(t *testing.T) {
	n, err := ParseNftCounter(testdata(t, "nft_counter.json"), "egress_blocked")
	if err != nil || n != 17 {
		t.Fatalf("counter %d %v", n, err)
	}
	if _, err := ParseNftCounter(testdata(t, "nft_counter_missing.json"), "egress_blocked"); err == nil {
		t.Fatal("missing counter must fail")
	}
	if _, err := ParseNftCounter([]byte("{not json"), "x"); err == nil {
		t.Fatal("malformed output must fail")
	}
	if _, err := ParseNftCounter([]byte(`{"nftables":[{"counter":"oops"}]}`), "x"); err == nil {
		t.Fatal("malformed counter must fail")
	}
	chk, err := ParseNftTable(testdata(t, "nft_table_drop.json"), "inet", "sovereign")
	if err != nil || !chk.TableFound || !chk.OutputPolicyDrop {
		t.Fatalf("drop table %+v %v", chk, err)
	}
	chk, _ = ParseNftTable(testdata(t, "nft_table_accept.json"), "inet", "sovereign")
	if !chk.TableFound || chk.OutputPolicyDrop {
		t.Fatalf("accept table %+v", chk)
	}
	if _, err := ParseNftTable([]byte("]"), "inet", "sovereign"); err == nil {
		t.Fatal("malformed table must fail")
	}
}

func TestConntrackAndPolicy(t *testing.T) {
	entries := ParseConntrack(bytes.NewReader(testdata(t, "conntrack.txt")))
	if len(entries) != 6 {
		t.Fatalf("parsed %d entries", len(entries))
	}
	p := policy()
	external := 0
	for _, e := range entries {
		if p.External(e.Dst, e.DPort, e.Proto) {
			external++
		}
	}
	if external != 2 {
		t.Fatalf("external = %d, want 2 (1.1.1.1 and the IPv6 Cloudflare address)", external)
	}
	mapped := netip.MustParseAddr("::ffff:10.20.0.10")
	if !p.Allowed(mapped, 636, "tcp") || p.Allowed(mapped, 389, "tcp") {
		t.Fatal("IPv4-mapped allowlist matching is wrong")
	}
	if !p.Allowed(netip.MustParseAddr("10.30.0.5"), 6514, "tcp") {
		t.Fatal("default proto should be tcp")
	}
	if p.External(netip.MustParseAddr("::ffff:127.0.0.1"), 80, "tcp") {
		t.Fatal("mapped loopback is not external")
	}
	if p.External(netip.MustParseAddr("fe80::1"), 80, "tcp") || p.External(netip.MustParseAddr("224.0.0.1"), 5353, "udp") {
		t.Fatal("link-local and multicast are not counted")
	}
	for in, ok := range map[string]bool{"1.1.1.1:443": true, "[2606:4700::1111]:443": true, "nope": false,
		"host:abc": false, "x:1": false} {
		if _, _, got := SplitHostPort(in); got != ok {
			t.Errorf("SplitHostPort(%q) = %v", in, got)
		}
	}
}

func TestAuditJoinAcrossReads(t *testing.T) {
	j := NewAuditJoiner()
	var recs []AuditRecord
	for _, line := range strings.Split(string(testdata(t, "audit.log")), "\n") {
		if r, ok := j.Feed(line); ok {
			recs = append(recs, r)
		}
	}
	if len(recs) != 3 {
		t.Fatalf("joined %d records", len(recs))
	}
	if recs[0].Addr.String() != "1.1.1.1" || recs[0].Port != 443 || recs[0].PID != 2211 || recs[0].Success {
		t.Fatalf("ipv4 record %+v", recs[0])
	}
	if recs[1].Addr.String() != "2606:4700:4700::1111" || recs[1].Family != 10 {
		t.Fatalf("ipv6 record %+v", recs[1])
	}
	if !recs[2].Addr.IsLoopback() || !recs[2].Success {
		t.Fatalf("loopback record %+v", recs[2])
	}
	for _, bad := range []string{"zz", "0200", "0300000000000000", "0A00000000000000"} {
		if _, _, _, ok := DecodeSockaddr(bad); ok {
			t.Errorf("DecodeSockaddr(%q) should fail", bad)
		}
	}
	if IsSandboxCgroup("0::/system.slice/sshd.service", []string{"docker", "wb-"}) ||
		!IsSandboxCgroup("0::/system.slice/docker-abc.scope", []string{"", "docker"}) {
		t.Fatal("cgroup classification wrong")
	}
	if !ResolvHasNameserver("# c\nnameserver 10.0.0.1\n") || ResolvHasNameserver("# nameserver 1.1.1.1\n") {
		t.Fatal("resolv.conf parsing wrong")
	}
}

func TestAuditCollectorTailsAndRotates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.log")
	os.WriteFile(path, []byte("type=DAEMON_START msg=audit(1.0:1): old content\n"), 0o600)
	st := NewState("dev", filepath.Join(dir, "state.json"), 100, nil)
	c := NewAuditCollector(AuditConfig{Path: path, Enabled: true}, policy(), []string{"wb-"})
	c.poll = 20 * time.Millisecond
	c.cgroup = func(pid int) string {
		if pid == 3300 {
			return "0::/system.slice/docker-wb-run.scope"
		}
		return "0::/user.slice"
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := c.Start(ctx, st); err != nil || c.Status() != "ok" {
		t.Fatalf("start %v %s", err, c.Status())
	}
	lines := strings.Split(string(testdata(t, "audit.log")), "\n")
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	f.WriteString(lines[0] + "\n" + lines[1][:30])
	f.Close()
	time.Sleep(100 * time.Millisecond)
	f, _ = os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	f.WriteString(lines[1][30:] + "\n")
	f.Close()
	waitFor(t, func() bool { h, _, _ := st.Counters(); return h == 1 })
	rotatedContent := []byte(strings.Join(lines[3:5], "\n") + "\n" + strings.Join(lines[5:7], "\n") + "\n")
	if err := os.Rename(path, path+".1"); err != nil {
		// Windows cannot rename a file another handle holds open; auditd rotation happens on Linux.
		t.Logf("rotation step skipped: %v", err)
		f, _ = os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
		f.Write(rotatedContent)
		f.Close()
	} else {
		os.WriteFile(path, rotatedContent, 0o600)
	}
	waitFor(t, func() bool { _, s, _ := st.Counters(); return s == 1 })
	if st.Snapshot().Breach {
		t.Fatal("a loopback success is not a breach")
	}
	disabled := NewAuditCollector(AuditConfig{Path: path}, policy(), nil)
	disabled.Start(ctx, st)
	if !strings.HasPrefix(disabled.Status(), "unavailable") {
		t.Fatal("disabled collector must report unavailable")
	}
	missing := NewAuditCollector(AuditConfig{Path: filepath.Join(dir, "none"), Enabled: true}, policy(), nil)
	missing.Start(ctx, st)
	if !strings.HasPrefix(missing.Status(), "unavailable") {
		t.Fatal("missing log must report unavailable, not zero")
	}
	c.Handle(st, AuditRecord{Addr: netip.MustParseAddr("8.8.8.8"), Port: 53, PID: 9, Success: true, Epoch: 5})
	if !st.Snapshot().Breach {
		t.Fatal("a successful external connect is a breach")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not reached")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestNftAndConntrackCollectors(t *testing.T) {
	dir := t.TempDir()
	st := NewState("dev", filepath.Join(dir, "s.json"), 100, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Setenv("WB_FAKE_ROLE", "nft")
	out := filepath.Join(dir, "nft.json")
	os.WriteFile(out, testdata(t, "nft_counter.json"), 0o600)
	t.Setenv("WB_FAKE_NFT_OUT", out)
	nft := NewNftCollector(NftConfig{Bin: os.Args[0], Table: "inet sovereign", Counter: "egress_blocked", PollS: 1})
	nft.Start(ctx, st)
	if _, _, p := st.Counters(); p == nil || *p != 17 || nft.Status() != "ok" {
		t.Fatalf("packets %v status %s", p, nft.Status())
	}
	os.WriteFile(out, []byte("garbage"), 0o600)
	nft.poll(ctx, st)
	if !strings.HasPrefix(nft.Status(), "error") {
		t.Fatalf("status %s", nft.Status())
	}
	missingBin := NewNftCollector(NftConfig{Bin: filepath.Join(dir, "no-nft"), Table: "inet sovereign", PollS: 1})
	fresh := NewState("dev", filepath.Join(dir, "s2.json"), 10, nil)
	missingBin.Start(ctx, fresh)
	if !strings.HasPrefix(missingBin.Status(), "unavailable") || fresh.Snapshot().BlockedPackets != nil {
		t.Fatal("without nft the counter must be unavailable, not zero")
	}
	ct := filepath.Join(dir, "nf_conntrack")
	os.WriteFile(ct, testdata(t, "conntrack.txt"), 0o600)
	c := NewConntrackCollector(ConntrackConfig{Path: ct, PollS: 1}, policy())
	c.Start(ctx, st)
	if snap := st.Snapshot(); snap.ExternalConnections != 2 || !snap.Breach || c.Status() != "ok" {
		t.Fatalf("snapshot %+v", snap)
	}
	gone := NewConntrackCollector(ConntrackConfig{Path: filepath.Join(dir, "none"), PollS: 1}, policy())
	gone.Start(ctx, st)
	if !strings.HasPrefix(gone.Status(), "unavailable") {
		t.Fatal("missing conntrack must be unavailable")
	}
	if shorten(strings.Repeat("a", 200)) != strings.Repeat("a", 80) {
		t.Fatal("shorten")
	}
}

func TestStateDedupEventsAndPersistence(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	now := time.Date(2026, 9, 15, 10, 0, 0, 0, time.UTC)
	st := NewState("dev", path, 3, func() time.Time { return now })
	ts := now.Format(time.RFC3339)
	if !st.Record(Event{TS: ts, Origin: "host", Addr: "1.1.1.1:443", PID: 7, Source: "report"}) {
		t.Fatal("first event must count")
	}
	if st.Record(Event{TS: now.Add(time.Second).Format(time.RFC3339), Origin: "host", Addr: "1.1.1.1:443", PID: 7, Source: "auditlog"}) {
		t.Fatal("the audit duplicate within a second must not count")
	}
	if !st.Record(Event{TS: now.Add(3 * time.Second).Format(time.RFC3339), Origin: "host", Addr: "1.1.1.1:443", PID: 7}) {
		t.Fatal("a later attempt must count")
	}
	for i := 0; i < 4; i++ {
		st.Record(Event{Origin: "sandbox", Addr: "8.8.8.8:53", TS: "not a time"})
	}
	st.Record(Event{Origin: "weird", Addr: "9.9.9.9:1"})
	h, s, _ := st.Counters()
	if h != 3 || s != 4 {
		t.Fatalf("host %d sandbox %d", h, s)
	}
	evs := st.Events(0, 100)
	if len(evs) != 3 || evs[len(evs)-1].Origin != "host" || evs[0].Seq != 5 {
		t.Fatalf("ring buffer %+v", evs)
	}
	if got := st.Events(6, 1); len(got) != 1 || got[0].Seq != 7 {
		t.Fatalf("since/limit %+v", got)
	}
	if err := st.Save(); err != nil {
		t.Fatal(err)
	}
	if err := st.Save(); err != nil {
		t.Fatal("second save is a no-op")
	}
	st.SetBreach()
	st.Save()
	again := NewState("dev", path, 3, nil)
	h2, s2, _ := again.Counters()
	snap := again.Snapshot()
	if h2 != 3 || s2 != 4 || !snap.Breach || snap.Since != "2026-09-15T10:00:00Z" {
		t.Fatalf("round trip %d %d %+v", h2, s2, snap)
	}
	bad := NewState("dev", filepath.Join(dir, "missing", "x.json"), 3, nil)
	bad.Record(Event{Origin: "host", Addr: "a"})
	if err := bad.Save(); err == nil {
		t.Fatal("expected a write error")
	}
}

type fakeSandbox struct {
	st       *State
	attempts int
	fail     bool
	report   bool
}

func (f *fakeSandbox) RunProbe(_ context.Context, dir, script, runID string) (int, string, error) {
	if f.fail {
		return 0, "", errors.New("sandboxd down")
	}
	if _, err := os.Stat(filepath.Join(dir, script)); err != nil {
		return 0, "", err
	}
	if f.report {
		f.st.Record(Event{Origin: "sandbox", Addr: "1.1.1.1:443", RunID: runID, Source: "sandboxd"})
	}
	return f.attempts, "blocked: [Errno 101] Network is unreachable", nil
}

type noDial struct{ dialed int }

func (n *noDial) DialContext(context.Context, string, string) (net.Conn, error) {
	n.dialed++
	return nil, errors.New("must not be used in dev mode")
}

func TestDevSelfTestPassesWithoutRealSockets(t *testing.T) {
	st := NewState("dev", filepath.Join(t.TempDir(), "s.json"), 100, nil)
	guard := GuardedDialer{State: st}
	tester := &Tester{Mode: "dev", State: st, Dialer: guard, Resolver: SimulatedResolver{}, Wait: time.Second,
		ProbeDir: filepath.Join(t.TempDir(), "probe"), Sandbox: &fakeSandbox{st: st, attempts: 1, report: true}}
	res := tester.Run(context.Background())
	if !res.Pass || len(res.Checks) != 3 {
		t.Fatalf("self-test %+v", res)
	}
	for _, c := range res.Checks {
		if !c.Pass {
			t.Errorf("%s failed: %+v", c.Name, c)
		}
	}
	if !res.Checks[1].Simulated {
		t.Fatal("dns check must be marked simulated in dev mode")
	}
	h, s, _ := st.Counters()
	if h != 1 || s != 1 {
		t.Fatalf("counters host %d sandbox %d", h, s)
	}
	if _, err := guard.DialContext(context.Background(), "tcp", "1.1.1.1:443"); !errors.Is(err, ErrRefused) {
		t.Fatal("guarded dialer must refuse")
	}
	bad := &Tester{Mode: "dev", State: st, Dialer: guard, Resolver: SimulatedResolver{}, Wait: 100 * time.Millisecond,
		ProbeDir: filepath.Join(t.TempDir(), "probe"), Sandbox: &fakeSandbox{fail: true}}
	if r := bad.Run(context.Background()); r.Pass || r.Checks[2].Pass {
		t.Fatal("a failing sandbox must fail the test")
	}
	none := &Tester{Mode: "dev", State: st, Dialer: guard, Resolver: SimulatedResolver{}, Wait: 100 * time.Millisecond}
	if r := none.Run(context.Background()); r.Checks[2].Pass {
		t.Fatal("no sandbox configured must fail")
	}
}

type okConn struct{ net.Conn }

func (okConn) Close() error { return nil }

type succeedDial struct{}

func (succeedDial) DialContext(context.Context, string, string) (net.Conn, error) {
	return okConn{}, nil
}

type okResolver struct{}

func (okResolver) LookupHost(context.Context, string) ([]string, error) {
	return []string{"93.184.216.34"}, nil
}

func TestEnforcedBreachPath(t *testing.T) {
	st := NewState("enforced", filepath.Join(t.TempDir(), "s.json"), 100, nil)
	tester := &Tester{Mode: "enforced", State: st, Dialer: succeedDial{}, Resolver: okResolver{}, Wait: 100 * time.Millisecond,
		ProbeDir: t.TempDir(), Sandbox: &fakeSandbox{st: st, attempts: 1, report: true}}
	res := tester.Run(context.Background())
	if res.Pass || !res.Breach || !st.Snapshot().Breach || !strings.Contains(res.Checks[0].Observed, "BREACH") {
		t.Fatalf("breach not reported: %+v", res)
	}
	if res.Checks[1].Pass {
		t.Fatal("a successful DNS lookup must fail the check")
	}
	st2 := NewState("enforced", filepath.Join(t.TempDir(), "s.json"), 100, nil)
	st2.SetPackets(5)
	nd := &noDial{}
	enforced := &Tester{Mode: "enforced", State: st2, Dialer: failThenCount{st: st2}, Resolver: SimulatedResolver{},
		Wait: 500 * time.Millisecond, ProbeDir: t.TempDir(), Sandbox: &fakeSandbox{st: st2, attempts: 1, report: true}}
	r2 := enforced.Run(context.Background())
	if !r2.Pass {
		t.Fatalf("enforced run with counters moving should pass: %+v", r2)
	}
	if nd.dialed != 0 {
		t.Fatal("unused dialer was called")
	}
}

type failThenCount struct{ st *State }

func (f failThenCount) DialContext(context.Context, string, string) (net.Conn, error) {
	f.st.Record(Event{Origin: "host", Addr: "1.1.1.1:443", PID: 1, Source: "auditlog"})
	_, _, p := f.st.Counters()
	f.st.SetPackets(*p + 1)
	return nil, errors.New("connect: operation not permitted")
}

func TestCheckEnforced(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	os.WriteFile(resolv, []byte("# no upstream DNS\n"), 0o600)
	cfg := &Config{Nft: NftConfig{Bin: os.Args[0], Table: "inet sovereign"}, ResolvConf: resolv}
	t.Setenv("WB_FAKE_ROLE", "nft")
	out := filepath.Join(dir, "table.json")
	t.Setenv("WB_FAKE_NFT_OUT", out)
	os.WriteFile(out, testdata(t, "nft_table_drop.json"), 0o600)
	ctx := context.Background()
	if err := CheckEnforced(ctx, cfg, RunCommand); err != nil {
		t.Fatalf("drop table should pass: %v", err)
	}
	os.WriteFile(out, testdata(t, "nft_table_accept.json"), 0o600)
	if err := CheckEnforced(ctx, cfg, RunCommand); err == nil || !strings.Contains(err.Error(), "policy drop") {
		t.Fatalf("accept policy must refuse: %v", err)
	}
	os.WriteFile(out, []byte(`{"nftables":[]}`), 0o600)
	if err := CheckEnforced(ctx, cfg, RunCommand); err == nil || !strings.Contains(err.Error(), "not found") {
		t.Fatalf("missing table must refuse: %v", err)
	}
	t.Setenv("WB_FAKE_NFT_FAIL", "1")
	if err := CheckEnforced(ctx, cfg, RunCommand); err == nil {
		t.Fatal("nft failure must refuse")
	}
	t.Setenv("WB_FAKE_NFT_FAIL", "")
	os.WriteFile(out, testdata(t, "nft_table_drop.json"), 0o600)
	os.WriteFile(resolv, []byte("nameserver 8.8.8.8\n"), 0o600)
	if err := CheckEnforced(ctx, cfg, RunCommand); err == nil || !strings.Contains(err.Error(), "nameserver") {
		t.Fatalf("configured nameserver must refuse: %v", err)
	}
	if err := CheckEnforced(ctx, &Config{Nft: NftConfig{Table: "nospace"}}, RunCommand); err == nil {
		t.Fatal("bad table name must refuse")
	}
}

func TestHandlerAndSandboxdClient(t *testing.T) {
	dir := t.TempDir()
	st := NewState("dev", filepath.Join(dir, "s.json"), 100, nil)
	sbSock := filepath.Join(dir, "sandboxd.sock")
	sbMux := http.NewServeMux()
	sbMux.HandleFunc("POST /v1/run", func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		json.NewDecoder(r.Body).Decode(&req)
		if req["script"] != "egress_probe.py" {
			udsserver.WriteError(w, 400, "invalid_request", "bad script")
			return
		}
		st.Record(Event{Origin: "sandbox", Addr: "1.1.1.1:443", RunID: req["run_id"].(string), Source: "sandboxd"})
		udsserver.WriteJSON(w, 200, map[string]any{"stdout_tail": "blocked: unreachable\n",
			"net_attempts": []map[string]string{{"ts": "x", "addr": "1.1.1.1:443"}}})
	})
	ln, err := udsserver.Listen(udsserver.Options{Name: "sandboxd", Socket: sbSock, Transport: "unix", RunDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go udsserver.Serve(ctx, quiet(), ln, sbMux)
	client := NewSandboxdClient(udsclient.FromSocket(sbSock, "unix", dir))
	tester := &Tester{Mode: "dev", State: st, Dialer: GuardedDialer{State: st}, Resolver: SimulatedResolver{},
		Sandbox: client, ProbeDir: filepath.Join(dir, "probe"), Wait: time.Second}
	srv := httptest.NewServer(NewHandler(st, tester, "test", time.Now()))
	defer srv.Close()
	post := func(path, body string) (int, map[string]any) {
		resp, err := http.Post(srv.URL+path, "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		json.NewDecoder(resp.Body).Decode(&out)
		return resp.StatusCode, out
	}
	if code, out := post("/v1/report", `{"origin":"host","addr":"1.1.1.1:443","pid":42,"ts":"bad","stack_summary":"x"}`); code != 202 || out["counted"] != true {
		t.Fatalf("report %d %v", code, out)
	}
	for _, bad := range []string{`{"origin":"moon","addr":"x"}`, `{"origin":"host"}`, `{"origin":"host","addr":"x","junk":1}`, `nope`} {
		if code, _ := post("/v1/report", bad); code != 400 {
			t.Errorf("%s accepted with %d", bad, code)
		}
	}
	code, res := post("/v1/test", ``)
	if code != 200 || res["pass"] != true {
		t.Fatalf("self-test over the API: %d %v", code, res)
	}
	get := func(path string) map[string]any {
		resp, err := http.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		json.NewDecoder(resp.Body).Decode(&out)
		return out
	}
	snap := get("/v1/snapshot")
	if snap["blocked_connect_host"].(float64) != 2 || snap["blocked_connect_sandbox"].(float64) != 1 ||
		snap["blocked_packets"] != nil || snap["mode"] != "dev" {
		t.Fatalf("snapshot %v", snap)
	}
	events := get("/v1/events?since=1&limit=0")["events"].([]any)
	if len(events) != 2 {
		t.Fatalf("events %v", events)
	}
	if get("/v1/health")["status"] != "ok" {
		t.Fatal("health")
	}
	if _, _, err := client.RunProbe(context.Background(), dir, "other.py", "r"); err == nil {
		t.Fatal("sandboxd error must surface")
	}
	persistCtx, stop := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { Persist(persistCtx, st, 10*time.Millisecond); close(done) }()
	time.Sleep(30 * time.Millisecond)
	stop()
	<-done
	if _, err := os.Stat(filepath.Join(dir, "s.json")); err != nil {
		t.Fatalf("state not persisted: %v", err)
	}
}
