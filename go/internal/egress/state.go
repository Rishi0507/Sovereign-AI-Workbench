package egress

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

// Event is one recorded outbound attempt.
type Event struct {
	Seq    int64  `json:"seq"`
	TS     string `json:"ts"`
	Origin string `json:"origin"`
	Addr   string `json:"addr"`
	Port   int    `json:"port,omitempty"`
	PID    int    `json:"pid,omitempty"`
	RunID  string `json:"run_id,omitempty"`
	Source string `json:"source"`
}

// Snapshot is the badge state returned by GET /v1/snapshot.
type Snapshot struct {
	Mode                  string            `json:"mode"`
	ExternalConnections   int               `json:"external_connections"`
	BlockedPackets        *int64            `json:"blocked_packets"`
	BlockedConnectHost    int64             `json:"blocked_connect_host"`
	BlockedConnectSandbox int64             `json:"blocked_connect_sandbox"`
	Collectors            map[string]string `json:"collectors"`
	Since                 string            `json:"since"`
	Breach                bool              `json:"breach"`
}

type persisted struct {
	Since                 string `json:"since"`
	BlockedConnectHost    int64  `json:"blocked_connect_host"`
	BlockedConnectSandbox int64  `json:"blocked_connect_sandbox"`
	Breach                bool   `json:"breach"`
	Seq                   int64  `json:"seq"`
}

type dedupKey struct {
	pid  int
	addr string
}

// State holds counters and the event ring buffer.
type State struct {
	mu       sync.Mutex
	mode     string
	since    time.Time
	host     int64
	sandbox  int64
	packets  *int64
	external int
	breach   bool
	seq      int64
	events   []Event
	limit    int
	recent   map[dedupKey]time.Time
	statuses map[string]string
	now      func() time.Time
	path     string
	dirty    bool
}

// NewState loads persisted counters from path (if present).
func NewState(mode, path string, limit int, now func() time.Time) *State {
	if now == nil {
		now = time.Now
	}
	s := &State{mode: mode, since: now().UTC(), limit: limit, recent: map[dedupKey]time.Time{},
		statuses: map[string]string{}, now: now, path: path}
	if data, err := os.ReadFile(path); err == nil {
		var p persisted
		if json.Unmarshal(data, &p) == nil {
			if t, err := time.Parse(time.RFC3339, p.Since); err == nil {
				s.since = t
			}
			s.host, s.sandbox, s.breach, s.seq = p.BlockedConnectHost, p.BlockedConnectSandbox, p.Breach, p.Seq
		}
	}
	return s
}

// SetStatus records a collector status.
func (s *State) SetStatus(name, status string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.statuses[name] = status
}

// SetPackets records the absolute nftables counter.
func (s *State) SetPackets(n int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.packets = &n
}

// SetExternal records the current number of external conntrack entries.
func (s *State) SetExternal(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.external = n
}

// SetBreach marks a failed containment check.
func (s *State) SetBreach() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.breach = true
	s.dirty = true
}

// Record counts an event unless it duplicates one seen within a second for the same pid and address.
func (s *State) Record(e Event) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	if e.TS == "" {
		e.TS = now.UTC().Format(time.RFC3339)
	}
	ts, err := time.Parse(time.RFC3339, e.TS)
	if err != nil {
		ts = now
	}
	key := dedupKey{pid: e.PID, addr: e.Addr}
	if e.PID != 0 {
		if prev, ok := s.recent[key]; ok && absDuration(ts.Sub(prev)) <= time.Second {
			return false
		}
		s.recent[key] = ts
		for k, v := range s.recent {
			if absDuration(now.Sub(v)) > time.Minute {
				delete(s.recent, k)
			}
		}
	}
	switch e.Origin {
	case "sandbox":
		s.sandbox++
	default:
		e.Origin = "host"
		s.host++
	}
	s.seq++
	e.Seq = s.seq
	s.events = append(s.events, e)
	if over := len(s.events) - s.limit; over > 0 {
		s.events = append([]Event(nil), s.events[over:]...)
	}
	s.dirty = true
	return true
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

// Counters returns host and sandbox counts and the packet counter.
func (s *State) Counters() (host, sandbox int64, packets *int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.packets != nil {
		v := *s.packets
		packets = &v
	}
	return s.host, s.sandbox, packets
}

// Snapshot returns the badge state.
func (s *State) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	statuses := map[string]string{}
	for k, v := range s.statuses {
		statuses[k] = v
	}
	var packets *int64
	if s.packets != nil {
		v := *s.packets
		packets = &v
	}
	return Snapshot{Mode: s.mode, ExternalConnections: s.external, BlockedPackets: packets,
		BlockedConnectHost: s.host, BlockedConnectSandbox: s.sandbox, Collectors: statuses,
		Since: s.since.UTC().Format(time.RFC3339), Breach: s.breach || s.external > 0}
}

// Events returns events with seq > since, at most limit.
func (s *State) Events(since int64, limit int) []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []Event{}
	for _, e := range s.events {
		if e.Seq > since {
			out = append(out, e)
			if len(out) >= limit {
				break
			}
		}
	}
	return out
}

// Save writes the counters to the state file if they changed.
func (s *State) Save() error {
	s.mu.Lock()
	p := persisted{Since: s.since.UTC().Format(time.RFC3339), BlockedConnectHost: s.host,
		BlockedConnectSandbox: s.sandbox, Breach: s.breach, Seq: s.seq}
	dirty := s.dirty
	s.dirty = false
	s.mu.Unlock()
	if !dirty {
		return nil
	}
	data, _ := json.MarshalIndent(p, "", "  ")
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}
