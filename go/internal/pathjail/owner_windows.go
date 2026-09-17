//go:build windows

package pathjail

import "os"

const caseInsensitiveFS = true

// checkOwner is a no-op on Windows development machines; ACLs govern access there.
func checkOwner(os.FileInfo) error { return nil }
