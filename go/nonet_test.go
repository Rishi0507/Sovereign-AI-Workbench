package sovereign_test

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// TestNoOutboundNetworkCode fails if production code gains an HTTP client or an IP dialer outside
// the two files allowed to hold them.
func TestNoOutboundNetworkCode(t *testing.T) {
	httpRe := regexp.MustCompile(`http\.(Get|Post|PostForm|Head)\(|http\.DefaultClient|http\.Client\{`)
	dialRe := regexp.MustCompile(`net\.Dial\(|net\.DialTimeout\(|net\.Dialer\{`)
	allowedHTTP := filepath.Join("internal", "udsclient", "client.go")
	allowedDial := map[string]bool{
		filepath.Join("internal", "egress", "selftest.go"):  true,
		filepath.Join("internal", "udsclient", "client.go"): true,
	}
	checked := 0
	err := filepath.Walk(".", func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") ||
			strings.Contains(path, "testutil") {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		checked++
		src := string(data)
		if httpRe.MatchString(src) && path != allowedHTTP {
			t.Errorf("%s contains an HTTP client call", path)
		}
		if dialRe.MatchString(src) && !allowedDial[path] {
			t.Errorf("%s contains an IP dialer", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if checked < 10 {
		t.Fatalf("scanned only %d files", checked)
	}
	mod, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(mod), "require") {
		t.Fatal("go.mod must not have require lines")
	}
}
