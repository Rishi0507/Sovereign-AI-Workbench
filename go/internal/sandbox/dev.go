package sandbox

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

// devBackend runs the script as a local subprocess through the same probe. Network isolation is
// probe-only; health reports it so the UI can say so.
type devBackend struct{ cfg *Config }

func (d devBackend) Name() string      { return "dev" }
func (d devBackend) Isolation() string { return "probe-only" }

func (d devBackend) Command(ctx context.Context, job *Job) (*exec.Cmd, []string) {
	r := job.Resources
	argv := []string{d.cfg.DevPython, "-I", filepath.Join(d.cfg.ProbeDir, "run.py")}
	if runtime.GOOS != "windows" {
		rl := fmt.Sprintf("cpu=%d,as=%d,fsize=%d,nproc=%d", r.TimeoutS+1, int64(r.MemoryMB)<<20, int64(512)<<20, r.Pids)
		argv = append(argv, "--rlimits", rl)
	}
	argv = append(argv, job.Req.Script)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = job.JobDir
	home := filepath.Join(job.JobDir, ".home")
	_ = os.MkdirAll(home, 0o700)
	cmd.Env = []string{"PATH=" + devPath(), "HOME=" + home, "PYTHONIOENCODING=utf-8"}
	for _, k := range []string{"SYSTEMROOT", "TEMP", "TMP"} {
		if v, ok := os.LookupEnv(k); ok && runtime.GOOS == "windows" {
			cmd.Env = append(cmd.Env, k+"="+v)
		}
	}
	setProcessGroup(cmd)
	return cmd, argv
}

func devPath() string {
	if runtime.GOOS == "windows" {
		return os.Getenv("PATH")
	}
	return "/usr/bin:/bin"
}

func (d devBackend) Kill(_ *Job, cmd *exec.Cmd) { killProcessGroup(cmd) }

func (d devBackend) Grace() time.Duration { return 2 * time.Second }
