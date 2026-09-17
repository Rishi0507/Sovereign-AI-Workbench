//go:build windows

package sandbox

import "os/exec"

func setProcessGroup(cmd *exec.Cmd) {
	cmd.Cancel = func() error {
		killProcessGroup(cmd)
		return nil
	}
}

func killProcessGroup(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = cmd.Process.Kill()
}
