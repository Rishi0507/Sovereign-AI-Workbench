// Package pathjail resolves job directories strictly inside a root, rejecting symlinks,
// parent references and prefix tricks such as /srv/workspaces-evil.
package pathjail

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrEscape is returned when a path resolves outside the root.
var ErrEscape = errors.New("path escapes the workspace root")

// Within reports whether target equals root or lies below it on a separator boundary.
func Within(root, target string) bool {
	root = filepath.Clean(root)
	target = filepath.Clean(target)
	if caseInsensitiveFS {
		root, target = strings.ToLower(root), strings.ToLower(target)
	}
	if root == target {
		return true
	}
	prefix := root
	if !strings.HasSuffix(prefix, string(filepath.Separator)) {
		prefix += string(filepath.Separator)
	}
	return strings.HasPrefix(target, prefix)
}

// Resolve returns the real path of dir, which must be an existing directory below root with no
// symlinked component between root and dir.
func Resolve(root, dir string) (string, error) {
	if root == "" || dir == "" {
		return "", errors.New("root and directory are required")
	}
	if !filepath.IsAbs(dir) {
		return "", fmt.Errorf("%w: job_dir must be absolute", ErrEscape)
	}
	for _, part := range strings.Split(filepath.ToSlash(dir), "/") {
		if part == ".." {
			return "", fmt.Errorf("%w: parent references are not allowed", ErrEscape)
		}
	}
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", fmt.Errorf("resolve root: %w", err)
	}
	realRoot, err = filepath.Abs(realRoot)
	if err != nil {
		return "", err
	}
	clean := filepath.Clean(dir)
	if !Within(filepath.Clean(root), clean) && !Within(realRoot, clean) {
		return "", ErrEscape
	}
	rel, err := filepath.Rel(filepath.Clean(root), clean)
	if err != nil || strings.HasPrefix(rel, "..") {
		rel, err = filepath.Rel(realRoot, clean)
		if err != nil || strings.HasPrefix(rel, "..") {
			return "", ErrEscape
		}
	}
	cur := realRoot
	if rel != "." {
		for _, part := range strings.Split(rel, string(filepath.Separator)) {
			cur = filepath.Join(cur, part)
			fi, err := os.Lstat(cur)
			if err != nil {
				return "", fmt.Errorf("stat %s: %w", cur, err)
			}
			if fi.Mode()&os.ModeSymlink != 0 {
				return "", fmt.Errorf("%w: %s is a symlink", ErrEscape, cur)
			}
		}
	}
	real, err := filepath.EvalSymlinks(cur)
	if err != nil {
		return "", err
	}
	if !Within(realRoot, real) {
		return "", ErrEscape
	}
	fi, err := os.Stat(real)
	if err != nil {
		return "", err
	}
	if !fi.IsDir() {
		return "", fmt.Errorf("%s is not a directory", real)
	}
	if err := checkOwner(fi); err != nil {
		return "", err
	}
	return real, nil
}

// ScriptName validates a script file name inside a job directory.
func ScriptName(dir, name string) (string, error) {
	if name == "" || strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") || !strings.HasSuffix(name, ".py") {
		return "", fmt.Errorf("script must be a .py file name without path separators")
	}
	path := filepath.Join(dir, name)
	fi, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("script: %w", err)
	}
	if !fi.Mode().IsRegular() {
		return "", fmt.Errorf("script %s is not a regular file", name)
	}
	return path, nil
}
