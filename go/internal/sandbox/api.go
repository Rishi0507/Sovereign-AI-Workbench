// Package sandbox validates run requests, executes them in Docker (or a development subprocess)
// and returns a result the Python side mirrors field by field.
package sandbox

// RunRequest is the body of POST /v1/run.
type RunRequest struct {
	RunID    string `json:"run_id"`
	JobDir   string `json:"job_dir"`
	Script   string `json:"script"`
	Image    string `json:"image"`
	Runtime  string `json:"runtime"`
	TimeoutS int    `json:"timeout_s"`
	CPUs     int    `json:"cpus"`
	MemoryMB int    `json:"memory_mb"`
	Pids     int    `json:"pids"`
	Label    string `json:"label"`
}

// FileInfo describes a file created or changed by a run.
type FileInfo struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// NetAttempt is one blocked network attempt recorded by the in-sandbox probe.
type NetAttempt struct {
	TS   string `json:"ts"`
	Addr string `json:"addr"`
}

// RunResult is the response of POST /v1/run.
type RunResult struct {
	RunID         string       `json:"run_id"`
	Backend       string       `json:"backend"`
	ExitCode      int          `json:"exit_code"`
	TimedOut      bool         `json:"timed_out"`
	OOMKilled     bool         `json:"oom_killed"`
	DurationMs    int64        `json:"duration_ms"`
	StdoutTail    string       `json:"stdout_tail"`
	StderrTail    string       `json:"stderr_tail"`
	TracebackHead []string     `json:"traceback_head"`
	NewFiles      []FileInfo   `json:"new_files"`
	ChangedFiles  []FileInfo   `json:"changed_files"`
	NetAttempts   []NetAttempt `json:"net_attempts"`
	Argv          []string     `json:"argv"`
}
