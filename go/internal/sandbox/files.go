package sandbox

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const maxHashBytes = 50 << 20

type fileState struct {
	size  int64
	mtime time.Time
	hash  string
}

// Snapshot records size, mtime and hash of every file below dir, skipping probe output and the
// dev HOME folder. Files over 50 MB are listed without a hash.
func Snapshot(dir string) (map[string]fileState, error) {
	out := map[string]fileState{}
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(dir, path)
		rel = filepath.ToSlash(rel)
		if d.IsDir() {
			if rel == ".home" {
				return filepath.SkipDir
			}
			return nil
		}
		if rel == ".net_attempts.jsonl" || !d.Type().IsRegular() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		st := fileState{size: info.Size(), mtime: info.ModTime()}
		if info.Size() <= maxHashBytes {
			if st.hash, err = hashFile(path); err != nil {
				return err
			}
		}
		out[rel] = st
		return nil
	})
	return out, err
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// Diff lists files that are new or changed between two snapshots, sorted by path.
func Diff(before, after map[string]fileState) (added, changed []FileInfo) {
	added, changed = []FileInfo{}, []FileInfo{}
	for rel, st := range after {
		info := FileInfo{Path: rel, Size: st.size, SHA256: st.hash}
		old, ok := before[rel]
		switch {
		case !ok:
			added = append(added, info)
		case old.size != st.size || old.hash != st.hash || (st.hash == "" && !old.mtime.Equal(st.mtime)):
			changed = append(changed, info)
		}
	}
	sort.Slice(added, func(i, j int) bool { return added[i].Path < added[j].Path })
	sort.Slice(changed, func(i, j int) bool { return changed[i].Path < changed[j].Path })
	return added, changed
}

// TracebackHead returns up to n lines starting at the last Python traceback in stderr.
func TracebackHead(stderr string, n int) []string {
	idx := strings.LastIndex(stderr, "Traceback (most recent call last):")
	if idx < 0 {
		return []string{}
	}
	lines := strings.Split(strings.TrimRight(stderr[idx:], "\n"), "\n")
	if len(lines) > n {
		lines = lines[:n]
	}
	for i := range lines {
		lines[i] = strings.TrimRight(lines[i], "\r")
	}
	return lines
}

// ReadNetAttempts parses .net_attempts.jsonl written by the probe.
func ReadNetAttempts(dir string) []NetAttempt {
	out := []NetAttempt{}
	f, err := os.Open(filepath.Join(dir, ".net_attempts.jsonl"))
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var a struct {
			TS   string `json:"ts"`
			Addr string `json:"addr"`
		}
		if json.Unmarshal(sc.Bytes(), &a) == nil && a.Addr != "" {
			out = append(out, NetAttempt{TS: a.TS, Addr: a.Addr})
		}
	}
	return out
}
