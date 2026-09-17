//go:build !windows

package pathjail

import (
	"fmt"
	"os"
	"syscall"
)

const caseInsensitiveFS = false

// checkOwner requires the job directory to belong to the user running the daemon.
func checkOwner(fi os.FileInfo) error {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	if int(st.Uid) != os.Getuid() {
		return fmt.Errorf("job directory is owned by uid %d, not by the workbench user", st.Uid)
	}
	return nil
}
