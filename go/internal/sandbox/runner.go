package sandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"

	"workbench.local/sovereign/internal/ringbuf"
	"workbench.local/sovereign/internal/udsclient"
)

// Backend starts and kills processes for one isolation technology.
type Backend interface {
	Name() string
	Isolation() string
	Command(ctx context.Context, job *Job) (*exec.Cmd, []string)
	Kill(job *Job, cmd *exec.Cmd)
	Grace() time.Duration
}

// Reporter forwards blocked network attempts to egressd.
type Reporter interface {
	Report(ctx context.Context, event map[string]any) error
}

// ErrBusy is returned when no run slot frees up within the wait limit.
var ErrBusy = errors.New("all sandbox slots are busy")

// ErrDuplicate is returned when a run id is already running.
var ErrDuplicate = errors.New("run id is already running")

// Runner executes jobs with bounded concurrency and keeps the last results.
type Runner struct {
	cfg      *Config
	backend  Backend
	reporter Reporter
	log      *slog.Logger
	slots    chan struct{}
	wait     time.Duration

	mu      sync.Mutex
	running map[string]context.CancelFunc
	queued  int
	results map[string]*RunResult
	order   []string
}

// NewRunner picks the backend ("auto" probes Docker once) and returns a runner.
func NewRunner(ctx context.Context, cfg *Config, reporter Reporter, log *slog.Logger) *Runner {
	var backend Backend = devBackend{cfg: cfg}
	switch cfg.Backend {
	case "docker":
		backend = dockerBackend{cfg: cfg}
	case "auto":
		if DockerAvailable(ctx, cfg.DockerBin) {
			backend = dockerBackend{cfg: cfg}
		}
	}
	if backend.Name() == "dev" {
		log.Warn("sandboxd uses the development backend: network isolation is probe-only")
	}
	return &Runner{cfg: cfg, backend: backend, reporter: reporter, log: log,
		slots: make(chan struct{}, cfg.MaxConcurrent), wait: 30 * time.Second,
		running: map[string]context.CancelFunc{}, results: map[string]*RunResult{}}
}

// Backend returns the backend in use.
func (r *Runner) Backend() Backend { return r.backend }

// Counts returns the number of running and queued jobs.
func (r *Runner) Counts() (running, queued int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.running), r.queued
}

// Result returns a stored result.
func (r *Runner) Result(id string) (*RunResult, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	res, ok := r.results[id]
	return res, ok
}

// Kill cancels a running job.
func (r *Runner) Kill(id string) bool {
	r.mu.Lock()
	cancel, ok := r.running[id]
	r.mu.Unlock()
	if ok {
		cancel()
	}
	return ok
}

func (r *Runner) acquire(ctx context.Context) error {
	r.mu.Lock()
	r.queued++
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.queued--
		r.mu.Unlock()
	}()
	timer := time.NewTimer(r.wait)
	defer timer.Stop()
	select {
	case r.slots <- struct{}{}:
		return nil
	case <-timer.C:
		return ErrBusy
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Run executes a validated job synchronously. Cancelling ctx kills the job.
func (r *Runner) Run(ctx context.Context, job *Job) (*RunResult, error) {
	if err := r.acquire(ctx); err != nil {
		return nil, err
	}
	defer func() { <-r.slots }()
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	id := job.Req.RunID
	r.mu.Lock()
	if _, dup := r.running[id]; dup {
		r.mu.Unlock()
		return nil, ErrDuplicate
	}
	r.running[id] = cancel
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		delete(r.running, id)
		r.mu.Unlock()
	}()

	_ = os.Remove(filepath.Join(job.JobDir, ".net_attempts.jsonl"))
	before, err := Snapshot(job.JobDir)
	if err != nil {
		return nil, fmt.Errorf("snapshot: %w", err)
	}
	limit := time.Duration(job.Resources.TimeoutS)*time.Second + r.backend.Grace()
	if r.backend.Name() == "dev" {
		limit = time.Duration(job.Resources.TimeoutS) * time.Second
	}
	deadline, stop := context.WithTimeout(runCtx, limit)
	defer stop()
	cmd, argv := r.backend.Command(deadline, job)
	stdout := ringbuf.New(r.cfg.StdoutTailBytes)
	stderr := ringbuf.New(64 << 10)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	cmd.WaitDelay = 3 * time.Second
	started := time.Now()
	runErr := cmd.Run()
	duration := time.Since(started)
	exit := 0
	var exitErr *exec.ExitError
	switch {
	case runErr == nil:
	case errors.As(runErr, &exitErr):
		exit = exitErr.ExitCode()
	default:
		exit = -1
		fmt.Fprintf(stderr, "\n[sandboxd] %v\n", runErr)
	}
	timedOut := errors.Is(deadline.Err(), context.DeadlineExceeded) ||
		(r.backend.Name() == "docker" && exit == 124)
	if deadline.Err() != nil {
		r.backend.Kill(job, cmd)
		if timedOut {
			fmt.Fprintf(stderr, "\n[sandboxd] timed out after %ds\n", job.Resources.TimeoutS)
		} else {
			fmt.Fprint(stderr, "\n[sandboxd] killed\n")
		}
		if exit == 0 {
			exit = -9
		}
	}
	after, err := Snapshot(job.JobDir)
	if err != nil {
		return nil, fmt.Errorf("snapshot: %w", err)
	}
	added, changed := Diff(before, after)
	errText := stderr.String()
	tail := errText
	if len(tail) > r.cfg.StdoutTailBytes {
		tail = tail[len(tail)-r.cfg.StdoutTailBytes:]
	}
	res := &RunResult{
		RunID: id, Backend: r.backend.Name(), ExitCode: exit, TimedOut: timedOut,
		OOMKilled:  r.backend.Name() == "docker" && dockerOOM(exit, errText),
		DurationMs: duration.Milliseconds(), StdoutTail: stdout.String(), StderrTail: tail,
		TracebackHead: TracebackHead(errText, r.cfg.TracebackHeadLines), NewFiles: added, ChangedFiles: changed,
		NetAttempts: ReadNetAttempts(job.JobDir), Argv: argv,
	}
	for _, a := range res.NetAttempts {
		if r.reporter == nil {
			break
		}
		event := map[string]any{"origin": "sandbox", "addr": a.Addr, "ts": a.TS, "run_id": id, "source": "sandboxd"}
		rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
		if err := r.reporter.Report(rctx, event); err != nil {
			r.log.Warn("egress report failed", "run_id", id, "err", err)
		}
		rcancel()
	}
	r.log.Info("run finished", "run_id", id, "backend", res.Backend, "exit", exit, "timed_out", timedOut,
		"ms", res.DurationMs, "new_files", len(added), "net_attempts", len(res.NetAttempts), "label", job.Req.Label)
	r.store(res)
	return res, nil
}

func (r *Runner) store(res *RunResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.results[res.RunID]; !ok {
		r.order = append(r.order, res.RunID)
	}
	r.results[res.RunID] = res
	for len(r.order) > 200 {
		delete(r.results, r.order[0])
		r.order = r.order[1:]
	}
}

// EgressReporter posts events to egressd and spools them locally when it is unreachable.
type EgressReporter struct {
	Client *http.Client
	Spool  string
	mu     sync.Mutex
}

// NewEgressReporter returns a reporter for the egressd endpoint.
func NewEgressReporter(ep udsclient.Endpoint, spool string) *EgressReporter {
	return &EgressReporter{Client: udsclient.New(ep, 5*time.Second), Spool: spool}
}

// Report implements Reporter.
func (e *EgressReporter) Report(ctx context.Context, event map[string]any) error {
	body, _ := json.Marshal(event)
	e.flush(ctx)
	err := e.post(ctx, body)
	if err != nil {
		e.mu.Lock()
		defer e.mu.Unlock()
		f, ferr := os.OpenFile(e.Spool, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if ferr != nil {
			return errors.Join(err, ferr)
		}
		defer f.Close()
		_, _ = f.Write(append(body, '\n'))
	}
	return err
}

func (e *EgressReporter) post(ctx context.Context, body []byte) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, udsclient.URL("/v1/report"), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := e.Client.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("egressd returned %d", resp.StatusCode)
	}
	return nil
}

func (e *EgressReporter) flush(ctx context.Context) {
	e.mu.Lock()
	defer e.mu.Unlock()
	data, err := os.ReadFile(e.Spool)
	if err != nil || len(data) == 0 {
		return
	}
	var rest [][]byte
	for _, line := range bytes.Split(bytes.TrimSpace(data), []byte("\n")) {
		if len(line) == 0 {
			continue
		}
		if e.post(ctx, line) != nil {
			rest = append(rest, line)
		}
	}
	if len(rest) == 0 {
		_ = os.Remove(e.Spool)
		return
	}
	_ = os.WriteFile(e.Spool, append(bytes.Join(rest, []byte("\n")), '\n'), 0o600)
}
