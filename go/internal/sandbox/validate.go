package sandbox

import (
	"fmt"
	"regexp"

	"workbench.local/sovereign/internal/pathjail"
)

var runIDRe = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`)

// Job is a validated, clamped request ready to run.
type Job struct {
	Req       RunRequest
	JobDir    string
	Script    string
	Resources Resources
}

// ValidationError is returned for requests rejected before anything runs.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

func invalid(format string, args ...any) error {
	return &ValidationError{Msg: fmt.Sprintf(format, args...)}
}

func clamp(v, def, limit int) int {
	if v <= 0 {
		return def
	}
	if v > limit {
		return limit
	}
	return v
}

// Validate checks a request against the configuration and clamps its resources.
func Validate(cfg *Config, req RunRequest) (*Job, error) {
	if !runIDRe.MatchString(req.RunID) {
		return nil, invalid("run_id must match %s", runIDRe)
	}
	dir, err := pathjail.Resolve(cfg.WorkspaceRoot, req.JobDir)
	if err != nil {
		return nil, invalid("job_dir: %v", err)
	}
	script, err := pathjail.ScriptName(dir, req.Script)
	if err != nil {
		return nil, invalid("%v", err)
	}
	if req.Image == "" {
		req.Image = cfg.ImageAllowlist[0]
	}
	if !contains(cfg.ImageAllowlist, req.Image) {
		return nil, invalid("image %q is not in the allowlist", req.Image)
	}
	if req.Runtime != "" && !contains(cfg.RuntimeAllowlist, req.Runtime) {
		return nil, invalid("runtime %q is not in the allowlist", req.Runtime)
	}
	res := Resources{
		TimeoutS: clamp(req.TimeoutS, cfg.Defaults.TimeoutS, cfg.Limits.TimeoutS),
		CPUs:     clamp(req.CPUs, cfg.Defaults.CPUs, cfg.Limits.CPUs),
		MemoryMB: clamp(req.MemoryMB, cfg.Defaults.MemoryMB, cfg.Limits.MemoryMB),
		Pids:     clamp(req.Pids, cfg.Defaults.Pids, cfg.Limits.Pids),
		TmpfsMB:  cfg.Defaults.TmpfsMB,
	}
	req.TimeoutS, req.CPUs, req.MemoryMB, req.Pids = res.TimeoutS, res.CPUs, res.MemoryMB, res.Pids
	return &Job{Req: req, JobDir: dir, Script: script, Resources: res}, nil
}
