package sandbox

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// BuildDockerArgv returns the exact docker command for a job (PRD section 4.19.2).
func BuildDockerArgv(job *Job, cfg *Config) []string {
	r := job.Resources
	mem := strconv.Itoa(r.MemoryMB) + "m"
	argv := []string{
		cfg.DockerBin, "run", "--rm", "--name", "wb-" + job.Req.RunID,
		"--network", "none", "--memory", mem, "--memory-swap", mem,
		"--cpus", strconv.Itoa(r.CPUs), "--pids-limit", strconv.Itoa(r.Pids),
		"--read-only", "--tmpfs", fmt.Sprintf("/tmp:rw,size=%dm,noexec,nosuid", r.TmpfsMB),
		"--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000",
	}
	if job.Req.Runtime != "" {
		argv = append(argv, "--runtime", job.Req.Runtime)
	}
	argv = append(argv,
		"-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
		"-v", job.JobDir+":/workspace:rw",
		"-v", cfg.ProbeDir+":/opt/wbprobe:ro",
		"-w", "/workspace",
		job.Req.Image, "timeout", strconv.Itoa(r.TimeoutS), "python", "-I", "/opt/wbprobe/run.py",
		"/workspace/"+job.Req.Script,
	)
	return argv
}

// DockerAvailable reports whether the Docker daemon answers.
func DockerAvailable(ctx context.Context, bin string) bool {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, bin, "version", "--format", "{{.Server.Version}}").Output()
	return err == nil && strings.TrimSpace(string(out)) != ""
}

// dockerOOM is the documented heuristic: exit code 137 with memory wording in stderr.
func dockerOOM(exitCode int, stderr string) bool {
	if exitCode != 137 {
		return false
	}
	low := strings.ToLower(stderr)
	return strings.Contains(low, "memory") || strings.Contains(low, "oom")
}

type dockerBackend struct{ cfg *Config }

func (d dockerBackend) Name() string      { return "docker" }
func (d dockerBackend) Isolation() string { return "container" }

func (d dockerBackend) Command(ctx context.Context, job *Job) (*exec.Cmd, []string) {
	argv := BuildDockerArgv(job, d.cfg)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	return cmd, argv
}

func (d dockerBackend) Kill(job *Job, _ *exec.Cmd) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = exec.CommandContext(ctx, d.cfg.DockerBin, "kill", "wb-"+job.Req.RunID).Run()
}

func (d dockerBackend) Grace() time.Duration { return 5 * time.Second }
