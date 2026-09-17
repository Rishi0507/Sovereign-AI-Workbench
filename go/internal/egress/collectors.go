package egress

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// Collector feeds the state from one independent source.
type Collector interface {
	Name() string
	Start(ctx context.Context, st *State) error
	Status() string
}

type statusHolder struct{ status string }

func (s *statusHolder) Status() string { return s.status }

// NftCollector polls the nftables drop counter.
type NftCollector struct {
	statusHolder
	cfg NftConfig
	run func(ctx context.Context, bin string, args ...string) ([]byte, error)
}

// NewNftCollector returns the collector; run executes the nft binary.
func NewNftCollector(cfg NftConfig) *NftCollector {
	return &NftCollector{cfg: cfg, run: RunCommand}
}

// RunCommand executes a binary and returns stdout.
func RunCommand(ctx context.Context, bin string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("%s: %w: %s", bin, err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}

func (c *NftCollector) Name() string { return "nft" }

func (c *NftCollector) poll(ctx context.Context, st *State) {
	fam, table, _ := strings.Cut(c.cfg.Table, " ")
	out, err := c.run(ctx, c.cfg.Bin, "-j", "list", "counter", fam, table, c.cfg.Counter)
	if err != nil {
		c.status = "unavailable(" + shorten(err.Error()) + ")"
		st.SetStatus(c.Name(), c.status)
		return
	}
	n, err := ParseNftCounter(out, c.cfg.Counter)
	if err != nil {
		c.status = "error(" + shorten(err.Error()) + ")"
		st.SetStatus(c.Name(), c.status)
		return
	}
	c.status = "ok"
	st.SetStatus(c.Name(), c.status)
	st.SetPackets(n)
}

// Start polls until ctx is done.
func (c *NftCollector) Start(ctx context.Context, st *State) error {
	c.poll(ctx, st)
	go every(ctx, time.Duration(c.cfg.PollS)*time.Second, func() { c.poll(ctx, st) })
	return nil
}

// ConntrackCollector counts current connections to external destinations.
type ConntrackCollector struct {
	statusHolder
	cfg    ConntrackConfig
	policy *Policy
}

// NewConntrackCollector returns the collector.
func NewConntrackCollector(cfg ConntrackConfig, policy *Policy) *ConntrackCollector {
	return &ConntrackCollector{cfg: cfg, policy: policy}
}

func (c *ConntrackCollector) Name() string { return "conntrack" }

func (c *ConntrackCollector) poll(st *State) {
	f, err := os.Open(c.cfg.Path)
	if err != nil {
		c.status = "unavailable(" + shorten(err.Error()) + ")"
		st.SetStatus(c.Name(), c.status)
		return
	}
	defer f.Close()
	external := 0
	for _, e := range ParseConntrack(f) {
		if c.policy.External(e.Dst, e.DPort, e.Proto) {
			external++
		}
	}
	c.status = "ok"
	st.SetStatus(c.Name(), c.status)
	st.SetExternal(external)
}

// Start polls until ctx is done.
func (c *ConntrackCollector) Start(ctx context.Context, st *State) error {
	c.poll(st)
	go every(ctx, time.Duration(c.cfg.PollS)*time.Second, func() { c.poll(st) })
	return nil
}

// AuditCollector tails audit.log for connect() syscalls, following rotation by inode.
type AuditCollector struct {
	statusHolder
	cfg     AuditConfig
	policy  *Policy
	markers []string
	cgroup  func(pid int) string
	poll    time.Duration
}

// NewAuditCollector returns the collector.
func NewAuditCollector(cfg AuditConfig, policy *Policy, markers []string) *AuditCollector {
	return &AuditCollector{cfg: cfg, policy: policy, markers: markers, cgroup: readCgroup, poll: 500 * time.Millisecond}
}

func readCgroup(pid int) string {
	b, _ := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cgroup")
	return string(b)
}

func (c *AuditCollector) Name() string { return "auditlog" }

// Handle classifies one joined record and records it if it is a blocked external attempt.
func (c *AuditCollector) Handle(st *State, rec AuditRecord) {
	if !c.policy.External(rec.Addr, rec.Port, "tcp") {
		return
	}
	origin := "host"
	if IsSandboxCgroup(c.cgroup(rec.PID), c.markers) {
		origin = "sandbox"
	}
	if rec.Success {
		st.SetBreach()
	}
	st.Record(Event{TS: time.Unix(rec.Epoch, 0).UTC().Format(time.RFC3339), Origin: origin,
		Addr: rec.Addr.String() + ":" + strconv.Itoa(rec.Port), Port: rec.Port, PID: rec.PID, Source: "auditlog"})
}

// Start tails the log until ctx is done.
func (c *AuditCollector) Start(ctx context.Context, st *State) error {
	if !c.cfg.Enabled {
		c.status = "unavailable(disabled in config)"
		st.SetStatus(c.Name(), c.status)
		return nil
	}
	f, err := os.Open(c.cfg.Path)
	if err != nil {
		c.status = "unavailable(" + shorten(err.Error()) + ")"
		st.SetStatus(c.Name(), c.status)
		return nil
	}
	if _, err := f.Seek(0, io.SeekEnd); err != nil {
		f.Close()
		return err
	}
	c.status = "ok"
	st.SetStatus(c.Name(), c.status)
	go c.follow(ctx, st, f)
	return nil
}

func (c *AuditCollector) follow(ctx context.Context, st *State, f *os.File) {
	joiner := NewAuditJoiner()
	reader := bufio.NewReader(f)
	var partial string
	ticker := time.NewTicker(c.poll)
	defer ticker.Stop()
	defer func() { f.Close() }()
	for {
		line, err := reader.ReadString('\n')
		if err == nil {
			if rec, ok := joiner.Feed(partial + line); ok {
				c.Handle(st, rec)
			}
			partial = ""
			continue
		}
		partial += line
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if rotated(f, c.cfg.Path) {
			nf, err := os.Open(c.cfg.Path)
			if err != nil {
				continue
			}
			f.Close()
			f = nf
			reader = bufio.NewReader(f)
			partial = ""
		}
	}
}

func rotated(f *os.File, path string) bool {
	cur, err := f.Stat()
	if err != nil {
		return true
	}
	next, err := os.Stat(path)
	if err != nil {
		return false
	}
	return !os.SameFile(cur, next) || next.Size() < mustOffset(f)
}

func mustOffset(f *os.File) int64 {
	off, err := f.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0
	}
	return off
}

func every(ctx context.Context, d time.Duration, fn func()) {
	t := time.NewTicker(d)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			fn()
		}
	}
}

func shorten(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) > 80 {
		return s[:80]
	}
	return s
}
