package sandbox

import (
	"errors"
	"fmt"
	"path/filepath"
)

// Resources are CPU, memory, process and time limits.
type Resources struct {
	TimeoutS int `json:"timeout_s"`
	CPUs     int `json:"cpus"`
	MemoryMB int `json:"memory_mb"`
	Pids     int `json:"pids"`
	TmpfsMB  int `json:"tmpfs_mb,omitempty"`
}

// Config is config/go/sandboxd.json.
type Config struct {
	Socket             string    `json:"socket"`
	Transport          string    `json:"transport"`
	WorkspaceRoot      string    `json:"workspace_root"`
	Backend            string    `json:"backend"`
	DockerBin          string    `json:"docker_bin"`
	ImageAllowlist     []string  `json:"image_allowlist"`
	RuntimeAllowlist   []string  `json:"runtime_allowlist"`
	MaxConcurrent      int       `json:"max_concurrent"`
	Defaults           Resources `json:"defaults"`
	Limits             Resources `json:"limits"`
	StdoutTailBytes    int       `json:"stdout_tail_bytes"`
	TracebackHeadLines int       `json:"traceback_head_lines"`
	ProbeDir           string    `json:"probe_dir"`
	DevPython          string    `json:"dev_python"`
	EgressdSocket      string    `json:"egressd_socket"`
	RunDir             string    `json:"run_dir"`
}

// Validate checks required fields and sane limits.
func (c *Config) Validate() error {
	var errs []error
	if c.Socket == "" {
		errs = append(errs, errors.New("socket is required"))
	}
	if c.Transport != "" && c.Transport != "unix" && c.Transport != "tcp" {
		errs = append(errs, fmt.Errorf("transport %q must be unix or tcp", c.Transport))
	}
	if !filepath.IsAbs(c.WorkspaceRoot) {
		errs = append(errs, errors.New("workspace_root must be an absolute path"))
	}
	switch c.Backend {
	case "auto", "docker", "dev":
	default:
		errs = append(errs, fmt.Errorf("backend %q must be auto, docker or dev", c.Backend))
	}
	if len(c.ImageAllowlist) == 0 {
		errs = append(errs, errors.New("image_allowlist must not be empty"))
	}
	if c.MaxConcurrent < 1 {
		errs = append(errs, errors.New("max_concurrent must be at least 1"))
	}
	if c.Defaults.TimeoutS < 1 || c.Limits.TimeoutS < c.Defaults.TimeoutS {
		errs = append(errs, errors.New("timeouts must be positive and limits must not be below defaults"))
	}
	if c.Defaults.CPUs < 1 || c.Defaults.MemoryMB < 64 || c.Defaults.Pids < 8 {
		errs = append(errs, errors.New("defaults need cpus >= 1, memory_mb >= 64 and pids >= 8"))
	}
	if c.Limits.CPUs < c.Defaults.CPUs || c.Limits.MemoryMB < c.Defaults.MemoryMB || c.Limits.Pids < c.Defaults.Pids {
		errs = append(errs, errors.New("limits must not be below defaults"))
	}
	if c.ProbeDir == "" {
		errs = append(errs, errors.New("probe_dir is required"))
	}
	if c.StdoutTailBytes < 256 {
		c.StdoutTailBytes = 4000
	}
	if c.TracebackHeadLines < 1 {
		c.TracebackHeadLines = 15
	}
	if c.Defaults.TmpfsMB < 1 {
		c.Defaults.TmpfsMB = 256
	}
	if c.DockerBin == "" {
		c.DockerBin = "docker"
	}
	if c.DevPython == "" {
		c.DevPython = "python3"
	}
	if c.RunDir == "" {
		c.RunDir = filepath.Dir(c.Socket)
	}
	return errors.Join(errs...)
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
