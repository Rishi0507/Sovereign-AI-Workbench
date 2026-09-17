package sandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
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

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func testConfig(t *testing.T, backend string) (*Config, string) {
	t.Helper()
	root := filepath.Join(t.TempDir(), "workspaces")
	job := filepath.Join(root, "_jobs", "T1", "r1")
	if err := os.MkdirAll(job, 0o750); err != nil {
		t.Fatal(err)
	}
	probe, err := filepath.Abs(filepath.Join("..", "..", "..", "workbench", "security", "probe"))
	if err != nil {
		t.Fatal(err)
	}
	cfg := &Config{
		Socket: filepath.Join(t.TempDir(), "sandboxd.sock"), Transport: "unix", WorkspaceRoot: root,
		Backend: backend, DockerBin: os.Args[0], ImageAllowlist: []string{"workbench-sandbox:py311"},
		RuntimeAllowlist: []string{"runc", "runsc"}, MaxConcurrent: 2,
		Defaults:        Resources{TimeoutS: 60, CPUs: 2, MemoryMB: 2048, Pids: 256, TmpfsMB: 256},
		Limits:          Resources{TimeoutS: 300, CPUs: 4, MemoryMB: 4096, Pids: 512},
		StdoutTailBytes: 4000, TracebackHeadLines: 15, ProbeDir: probe, DevPython: python(t),
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	return cfg, job
}

func python(t *testing.T) string {
	for _, name := range []string{"python3", "python"} {
		if p, err := exec.LookPath(name); err == nil {
			if exec.Command(p, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)").Run() == nil {
				return p
			}
		}
	}
	return ""
}

func writeScript(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "script.py"), []byte(body), 0o640); err != nil {
		t.Fatal(err)
	}
}

func TestConfigValidation(t *testing.T) {
	bad := &Config{Backend: "magic", Transport: "smoke", WorkspaceRoot: "relative"}
	err := bad.Validate()
	for _, want := range []string{"socket", "transport", "workspace_root", "backend", "image_allowlist",
		"max_concurrent", "timeouts", "probe_dir"} {
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("missing %q in %v", want, err)
		}
	}
}

func TestValidateAndClamp(t *testing.T) {
	cfg, job := testConfig(t, "dev")
	writeScript(t, job, "print(1)")
	ok := RunRequest{RunID: "r-1", JobDir: job, Script: "script.py", Image: "workbench-sandbox:py311",
		TimeoutS: 9999, CPUs: 0, MemoryMB: 100, Pids: 1000}
	j, err := Validate(cfg, ok)
	if err != nil {
		t.Fatal(err)
	}
	if j.Resources.TimeoutS != 300 || j.Resources.CPUs != 2 || j.Resources.MemoryMB != 100 || j.Resources.Pids != 512 {
		t.Fatalf("clamping wrong: %+v", j.Resources)
	}
	cases := []RunRequest{
		{RunID: "bad id!", JobDir: job, Script: "script.py"},
		{RunID: "r", JobDir: filepath.Dir(cfg.WorkspaceRoot), Script: "script.py"},
		{RunID: "r", JobDir: job, Script: "../script.py"},
		{RunID: "r", JobDir: job, Script: "script.py", Image: "evil:latest"},
		{RunID: "r", JobDir: job, Script: "script.py", Runtime: "kata"},
	}
	for i, c := range cases {
		var ve *ValidationError
		if _, err := Validate(cfg, c); !errors.As(err, &ve) {
			t.Errorf("case %d: expected a validation error, got %v", i, err)
		}
	}
	j2, err := Validate(cfg, RunRequest{RunID: "r2", JobDir: job, Script: "script.py"})
	if err != nil || j2.Req.Image != "workbench-sandbox:py311" {
		t.Fatalf("default image: %v %+v", err, j2)
	}
}

func TestDockerArgvGolden(t *testing.T) {
	cfg, job := testConfig(t, "docker")
	cfg.DockerBin = "docker"
	cfg.ProbeDir = "/opt/workbench/probe"
	j := &Job{Req: RunRequest{RunID: "abc", Script: "script.py", Image: "workbench-sandbox:py311"}, JobDir: job,
		Resources: Resources{TimeoutS: 60, CPUs: 2, MemoryMB: 2048, Pids: 256, TmpfsMB: 256}}
	want := []string{"docker", "run", "--rm", "--name", "wb-abc", "--network", "none", "--memory", "2048m",
		"--memory-swap", "2048m", "--cpus", "2", "--pids-limit", "256", "--read-only", "--tmpfs",
		"/tmp:rw,size=256m,noexec,nosuid", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--user", "1000:1000", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
		"-v", job + ":/workspace:rw", "-v", "/opt/workbench/probe:/opt/wbprobe:ro", "-w", "/workspace",
		"workbench-sandbox:py311", "timeout", "60", "python", "-I", "/opt/wbprobe/run.py", "/workspace/script.py"}
	if got := BuildDockerArgv(j, cfg); !reflect.DeepEqual(got, want) {
		t.Fatalf("argv mismatch\n got %q\nwant %q", got, want)
	}
	j.Req.Runtime = "runsc"
	got := BuildDockerArgv(j, cfg)
	if !strings.Contains(strings.Join(got, " "), "--user 1000:1000 --runtime runsc -e") {
		t.Fatalf("runtime flag missing: %q", got)
	}
}

func TestOOMHeuristic(t *testing.T) {
	if !dockerOOM(137, "container killed: out of memory") || dockerOOM(137, "killed") || dockerOOM(1, "memory") {
		t.Fatal("OOM heuristic wrong")
	}
}

func runner(t *testing.T, cfg *Config, rep Reporter) *Runner {
	t.Helper()
	return NewRunner(context.Background(), cfg, rep, quiet())
}

type memReporter struct {
	mu     sync.Mutex
	events []map[string]any
	fail   bool
}

func (m *memReporter) Report(_ context.Context, e map[string]any) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.fail {
		return errors.New("egressd down")
	}
	m.events = append(m.events, e)
	return nil
}

func TestFakeDockerRunner(t *testing.T) {
	cfg, job := testConfig(t, "auto")
	writeScript(t, job, "print(1)")
	logFile := filepath.Join(t.TempDir(), "docker.log")
	t.Setenv("WB_FAKE_ROLE", "docker")
	t.Setenv("WB_FAKE_LOG", logFile)
	t.Setenv("WB_FAKE_STDOUT", strings.Repeat("x", 5000)+"done\n")
	t.Setenv("WB_FAKE_STDERR", "noise\nTraceback (most recent call last):\n  File \"script.py\", line 1\nKeyError: 'pressure'\n")
	t.Setenv("WB_FAKE_WRITE", "out.csv")
	t.Setenv("WB_FAKE_NET", "1.1.1.1:443")
	t.Setenv("WB_FAKE_EXIT", "1")
	rep := &memReporter{}
	r := runner(t, cfg, rep)
	if r.Backend().Name() != "docker" || r.Backend().Isolation() != "container" {
		t.Fatalf("auto should pick docker, got %s", r.Backend().Name())
	}
	j, err := Validate(cfg, RunRequest{RunID: "run-1", JobDir: job, Script: "script.py"})
	if err != nil {
		t.Fatal(err)
	}
	res, err := r.Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 1 || res.TimedOut || res.OOMKilled || res.Backend != "docker" {
		t.Fatalf("unexpected result %+v", res)
	}
	if len(res.StdoutTail) != 4000 || !strings.HasSuffix(res.StdoutTail, "done\n") {
		t.Fatalf("stdout tail length %d", len(res.StdoutTail))
	}
	if len(res.TracebackHead) != 3 || res.TracebackHead[2] != "KeyError: 'pressure'" {
		t.Fatalf("traceback head %q", res.TracebackHead)
	}
	if len(res.NewFiles) != 1 || res.NewFiles[0].Path != "out.csv" || res.NewFiles[0].SHA256 == "" {
		t.Fatalf("new files %+v", res.NewFiles)
	}
	if len(res.NetAttempts) != 1 || len(rep.events) != 1 || rep.events[0]["origin"] != "sandbox" || rep.events[0]["run_id"] != "run-1" {
		t.Fatalf("net attempts %+v events %+v", res.NetAttempts, rep.events)
	}
	if stored, ok := r.Result("run-1"); !ok || stored != res {
		t.Fatal("result not stored")
	}
	t.Setenv("WB_FAKE_WRITE", "")
	t.Setenv("WB_FAKE_NET", "")
	os.WriteFile(filepath.Join(job, "out.csv"), []byte("changed!"), 0o600)
	j2, _ := Validate(cfg, RunRequest{RunID: "run-2", JobDir: job, Script: "script.py"})
	t.Setenv("WB_FAKE_WRITE", "out.csv")
	res2, err := r.Run(context.Background(), j2)
	if err != nil || len(res2.ChangedFiles) != 1 || len(res2.NewFiles) != 0 {
		t.Fatalf("changed files: %v %+v", err, res2)
	}
	logged, _ := os.ReadFile(logFile)
	if !bytes.Contains(logged, []byte(`"--network","none"`)) {
		t.Fatalf("docker was not called with --network none: %s", logged)
	}
}

func TestFakeDockerTimeoutKillsContainer(t *testing.T) {
	cfg, job := testConfig(t, "docker")
	writeScript(t, job, "print(1)")
	logFile := filepath.Join(t.TempDir(), "docker.log")
	t.Setenv("WB_FAKE_ROLE", "docker")
	t.Setenv("WB_FAKE_LOG", logFile)
	t.Setenv("WB_FAKE_SLEEP_MS", "5000")
	cfg.Defaults.TimeoutS = 1
	r := runner(t, cfg, nil)
	r.backend = shortGrace{dockerBackend{cfg: cfg}}
	j, _ := Validate(cfg, RunRequest{RunID: "slow", JobDir: job, Script: "script.py", TimeoutS: 1})
	res, err := r.Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || res.ExitCode == 0 {
		t.Fatalf("expected a timeout, got %+v", res)
	}
	logged, _ := os.ReadFile(logFile)
	if !bytes.Contains(logged, []byte(`["kill","wb-slow"]`)) {
		t.Fatalf("docker kill not called: %s", logged)
	}
}

type shortGrace struct{ dockerBackend }

func (shortGrace) Grace() time.Duration { return 100 * time.Millisecond }

func TestAutoFallsBackToDev(t *testing.T) {
	cfg, _ := testConfig(t, "auto")
	t.Setenv("WB_FAKE_ROLE", "docker")
	t.Setenv("WB_FAKE_DOCKER_DOWN", "1")
	r := runner(t, cfg, nil)
	if r.Backend().Name() != "dev" || r.Backend().Isolation() != "probe-only" {
		t.Fatalf("expected the dev backend, got %s", r.Backend().Name())
	}
}

func devRunner(t *testing.T) (*Config, string, *Runner, *memReporter) {
	t.Helper()
	cfg, job := testConfig(t, "dev")
	if cfg.DevPython == "" {
		t.Skip("python is not installed")
	}
	rep := &memReporter{}
	return cfg, job, runner(t, cfg, rep), rep
}

func TestDevBackendKeyErrorAndSocket(t *testing.T) {
	cfg, job, r, rep := devRunner(t)
	writeScript(t, job, "rows = [{'pressure_bar': 1}]\nprint(rows[0]['pressure'])\n")
	j, _ := Validate(cfg, RunRequest{RunID: "dev-1", JobDir: job, Script: "script.py", TimeoutS: 30})
	res, err := r.Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 1 || res.Backend != "dev" || len(res.TracebackHead) == 0 ||
		!strings.Contains(res.StderrTail, "KeyError: 'pressure'") {
		t.Fatalf("unexpected result %+v", res)
	}
	writeScript(t, job, "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 5)\n"+
		"except OSError as exc:\n    print('blocked', exc.errno)\n")
	j2, _ := Validate(cfg, RunRequest{RunID: "dev-2", JobDir: job, Script: "script.py", TimeoutS: 30})
	res2, err := r.Run(context.Background(), j2)
	if err != nil {
		t.Fatal(err)
	}
	if res2.ExitCode != 0 || len(res2.NetAttempts) != 1 || res2.NetAttempts[0].Addr != "1.1.1.1:443" {
		t.Fatalf("probe did not record the attempt: %+v", res2)
	}
	if len(rep.events) != 1 {
		t.Fatalf("attempt not forwarded: %+v", rep.events)
	}
	for _, f := range res2.NewFiles {
		if f.Path == ".net_attempts.jsonl" || strings.HasPrefix(f.Path, ".home") {
			t.Fatalf("probe files must not be listed: %+v", res2.NewFiles)
		}
	}
}

func TestDevBackendTimeoutAndKill(t *testing.T) {
	cfg, job, r, _ := devRunner(t)
	writeScript(t, job, "import time\ntime.sleep(30)\n")
	j, _ := Validate(cfg, RunRequest{RunID: "dev-slow", JobDir: job, Script: "script.py", TimeoutS: 1})
	start := time.Now()
	res, err := r.Run(context.Background(), j)
	if err != nil || !res.TimedOut || time.Since(start) > 15*time.Second {
		t.Fatalf("timeout not enforced: %v %+v", err, res)
	}
	j2, _ := Validate(cfg, RunRequest{RunID: "dev-kill", JobDir: job, Script: "script.py", TimeoutS: 60})
	done := make(chan *RunResult, 1)
	go func() {
		res, _ := r.Run(context.Background(), j2)
		done <- res
	}()
	deadline := time.Now().Add(10 * time.Second)
	for !r.Kill("dev-kill") {
		if time.Now().After(deadline) {
			t.Fatal("job never started")
		}
		time.Sleep(50 * time.Millisecond)
	}
	select {
	case res := <-done:
		if res == nil || res.ExitCode == 0 || res.TimedOut {
			t.Fatalf("kill result %+v", res)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("kill did not stop the job")
	}
	if r.Kill("nothing") {
		t.Fatal("kill of an unknown id must report false")
	}
}

func TestBusyAndDuplicate(t *testing.T) {
	cfg, job, r, _ := devRunner(t)
	cfg.MaxConcurrent = 1
	r.slots = make(chan struct{}, 1)
	r.wait = 200 * time.Millisecond
	writeScript(t, job, "import time\ntime.sleep(3)\n")
	j, _ := Validate(cfg, RunRequest{RunID: "hold", JobDir: job, Script: "script.py", TimeoutS: 10})
	go r.Run(context.Background(), j)
	deadline := time.Now().Add(5 * time.Second)
	for {
		running, _ := r.Counts()
		if running == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("first job did not start")
		}
		time.Sleep(20 * time.Millisecond)
	}
	j2, _ := Validate(cfg, RunRequest{RunID: "second", JobDir: job, Script: "script.py", TimeoutS: 10})
	if _, err := r.Run(context.Background(), j2); !errors.Is(err, ErrBusy) {
		t.Fatalf("expected ErrBusy, got %v", err)
	}
	r.slots = make(chan struct{}, 2)
	if _, err := r.Run(context.Background(), j); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("expected ErrDuplicate, got %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r.slots = make(chan struct{})
	if _, err := r.Run(ctx, j2); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
	r.Kill("hold")
}

func TestHandlers(t *testing.T) {
	cfg, job, r, _ := devRunner(t)
	h := NewHandler(cfg, r, "test", time.Now())
	srv := httptest.NewServer(udsserver.Middleware(quiet(), "", h))
	defer srv.Close()
	post := func(body any) (*http.Response, map[string]any) {
		b, _ := json.Marshal(body)
		resp, err := http.Post(srv.URL+"/v1/run", "application/json", bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		json.NewDecoder(resp.Body).Decode(&out)
		return resp, out
	}
	resp, out := post(map[string]any{"run_id": "x", "job_dir": "/etc", "script": "script.py"})
	if resp.StatusCode != 400 || out["error"] == nil {
		t.Fatalf("validation: %d %v", resp.StatusCode, out)
	}
	resp, _ = post(map[string]any{"run_id": "x", "surprise": true})
	if resp.StatusCode != 400 {
		t.Fatalf("unknown fields must be rejected, got %d", resp.StatusCode)
	}
	writeScript(t, job, "print('{\"ok\": 1}')\n")
	resp, out = post(map[string]any{"run_id": "h1", "job_dir": job, "script": "script.py"})
	if resp.StatusCode != 200 || out["exit_code"].(float64) != 0 {
		t.Fatalf("run: %d %v", resp.StatusCode, out)
	}
	get, _ := http.Get(srv.URL + "/v1/runs/h1")
	if get.StatusCode != 200 {
		t.Fatalf("get run: %d", get.StatusCode)
	}
	get.Body.Close()
	miss, _ := http.Get(srv.URL + "/v1/runs/none")
	if miss.StatusCode != 404 {
		t.Fatalf("missing run: %d", miss.StatusCode)
	}
	miss.Body.Close()
	kill, _ := http.Post(srv.URL+"/v1/runs/none/kill", "application/json", nil)
	if kill.StatusCode != 404 {
		t.Fatalf("kill missing: %d", kill.StatusCode)
	}
	kill.Body.Close()
	health, _ := http.Get(srv.URL + "/v1/health")
	var hb map[string]any
	json.NewDecoder(health.Body).Decode(&hb)
	health.Body.Close()
	if hb["status"] != "ok" || hb["backend"] != "dev" || hb["isolation"] != "probe-only" {
		t.Fatalf("health %v", hb)
	}
	r.slots = make(chan struct{})
	r.wait = 50 * time.Millisecond
	resp, out = post(map[string]any{"run_id": "h2", "job_dir": job, "script": "script.py"})
	if resp.StatusCode != 429 || out["retry_after_s"].(float64) != 5 {
		t.Fatalf("busy: %d %v", resp.StatusCode, out)
	}
}

func TestHandlerRunKill(t *testing.T) {
	cfg, job, r, _ := devRunner(t)
	h := NewHandler(cfg, r, "test", time.Now())
	srv := httptest.NewServer(h)
	defer srv.Close()
	writeScript(t, job, "import time\ntime.sleep(30)\n")
	done := make(chan int, 1)
	go func() {
		b, _ := json.Marshal(map[string]any{"run_id": "k1", "job_dir": job, "script": "script.py", "timeout_s": 60})
		resp, err := http.Post(srv.URL+"/v1/run", "application/json", bytes.NewReader(b))
		if err != nil {
			done <- -1
			return
		}
		resp.Body.Close()
		done <- resp.StatusCode
	}()
	deadline := time.Now().Add(10 * time.Second)
	for {
		resp, err := http.Post(srv.URL+"/v1/runs/k1/kill", "application/json", nil)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("could not kill")
		}
		time.Sleep(50 * time.Millisecond)
	}
	if code := <-done; code != 200 {
		t.Fatalf("run after kill returned %d", code)
	}
}

func TestEgressReporterSpoolsAndFlushes(t *testing.T) {
	dir := t.TempDir()
	sock := filepath.Join(dir, "egressd.sock")
	spool := filepath.Join(dir, "spool.jsonl")
	rep := NewEgressReporter(udsclient.FromSocket(sock, "unix", dir), spool)
	ctx := context.Background()
	if err := rep.Report(ctx, map[string]any{"origin": "sandbox", "addr": "1.1.1.1:443"}); err == nil {
		t.Fatal("expected an error while egressd is down")
	}
	if data, _ := os.ReadFile(spool); !bytes.Contains(data, []byte("1.1.1.1:443")) {
		t.Fatalf("event not spooled: %s", data)
	}
	var mu sync.Mutex
	var got []string
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/report", func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got = append(got, string(b))
		mu.Unlock()
		w.WriteHeader(202)
	})
	ln, err := udsserver.Listen(udsserver.Options{Name: "egressd", Socket: sock, Transport: "unix", RunDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	sctx, cancel := context.WithCancel(ctx)
	go udsserver.Serve(sctx, quiet(), ln, mux)
	defer cancel()
	if err := rep.Report(ctx, map[string]any{"origin": "sandbox", "addr": "8.8.8.8:53"}); err != nil {
		t.Fatalf("report: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 || !strings.Contains(got[0], "1.1.1.1") || !strings.Contains(got[1], "8.8.8.8") {
		t.Fatalf("got %v", got)
	}
	if _, err := os.Stat(spool); !os.IsNotExist(err) {
		t.Fatal("spool should be removed after a flush")
	}
}

func TestSnapshotHelpers(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "a.txt"), []byte("a"), 0o600)
	os.MkdirAll(filepath.Join(dir, ".home", "x"), 0o700)
	os.WriteFile(filepath.Join(dir, ".home", "x", "h"), []byte("h"), 0o600)
	os.WriteFile(filepath.Join(dir, ".net_attempts.jsonl"), []byte("{\"addr\":\"1.2.3.4:5\"}\nnot json\n"), 0o600)
	snap, err := Snapshot(dir)
	if err != nil || len(snap) != 1 {
		t.Fatalf("snapshot %v %v", snap, err)
	}
	if got := ReadNetAttempts(dir); len(got) != 1 {
		t.Fatalf("net attempts %v", got)
	}
	if got := ReadNetAttempts(t.TempDir()); len(got) != 0 {
		t.Fatal("expected none")
	}
	if len(TracebackHead("nothing here", 3)) != 0 {
		t.Fatal("no traceback expected")
	}
	if _, err := Snapshot(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("expected an error for a missing dir")
	}
}
