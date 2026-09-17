package pathjail

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func setup(t *testing.T) (root, job string) {
	t.Helper()
	base := t.TempDir()
	root = filepath.Join(base, "workspaces")
	job = filepath.Join(root, "_jobs", "T1", "run1")
	if err := os.MkdirAll(job, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(base, "workspaces-evil", "x"), 0o750); err != nil {
		t.Fatal(err)
	}
	return root, job
}

func TestResolveAcceptsJobDir(t *testing.T) {
	root, job := setup(t)
	got, err := Resolve(root, job)
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	want, _ := filepath.EvalSymlinks(job)
	if got != want {
		t.Fatalf("got %s want %s", got, want)
	}
	if _, err := Resolve(root, root); err != nil {
		t.Fatalf("root itself should resolve: %v", err)
	}
}

func TestResolveRejectsEscapes(t *testing.T) {
	root, job := setup(t)
	base := filepath.Dir(root)
	cases := map[string]string{
		"parent reference": filepath.Join(root, "_jobs", "..", "..", "etc"),
		"absolute escape":  filepath.Join(base),
		"prefix trick":     filepath.Join(base, "workspaces-evil", "x"),
		"relative path":    "_jobs/T1/run1",
		"missing":          filepath.Join(root, "_jobs", "nope"),
	}
	for name, dir := range cases {
		if _, err := Resolve(root, dir); err == nil {
			t.Errorf("%s: expected an error for %s", name, dir)
		}
	}
	if _, err := Resolve("", job); err == nil {
		t.Error("empty root must fail")
	}
	file := filepath.Join(job, "f.txt")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Resolve(root, file); err == nil {
		t.Error("a file is not a job directory")
	}
}

func symlinkOrSkip(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
}

func TestResolveRejectsSymlinks(t *testing.T) {
	root, job := setup(t)
	outside := t.TempDir()
	link := filepath.Join(root, "_jobs", "linked")
	symlinkOrSkip(t, outside, link)
	if _, err := Resolve(root, link); !errors.Is(err, ErrEscape) {
		t.Fatalf("symlinked job dir: got %v", err)
	}
	parentLink := filepath.Join(root, "_jobs", "T2")
	symlinkOrSkip(t, filepath.Join(root, "_jobs", "T1"), parentLink)
	if _, err := Resolve(root, filepath.Join(parentLink, "run1")); !errors.Is(err, ErrEscape) {
		t.Fatalf("symlinked parent: got %v", err)
	}
	_ = job
}

func TestResolveMissingRoot(t *testing.T) {
	base := t.TempDir()
	missing := filepath.Join(base, "nope")
	if _, err := Resolve(missing, filepath.Join(missing, "job")); err == nil {
		t.Fatal("a missing root must fail")
	}
	if _, err := Resolve(base, ""); err == nil {
		t.Fatal("an empty job dir must fail")
	}
}

func TestWithin(t *testing.T) {
	sep := string(filepath.Separator)
	root := sep + filepath.Join("srv", "workspaces")
	if !Within(root, root+sep+"a") || Within(root, root+"-evil"+sep+"a") || !Within(root, root) {
		t.Fatal("Within is wrong")
	}
}

func TestScriptName(t *testing.T) {
	_, job := setup(t)
	if err := os.WriteFile(filepath.Join(job, "script.py"), []byte("print(1)"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ScriptName(job, "script.py"); err != nil {
		t.Fatalf("valid script: %v", err)
	}
	for _, bad := range []string{"", "../x.py", "a/b.py", `a\b.py`, "x.sh", "missing.py"} {
		if _, err := ScriptName(job, bad); err == nil {
			t.Errorf("%q should be rejected", bad)
		}
	}
	if err := os.Mkdir(filepath.Join(job, "dir.py"), 0o750); err != nil {
		t.Fatal(err)
	}
	if _, err := ScriptName(job, "dir.py"); err == nil {
		t.Error("a directory is not a script")
	}
}
