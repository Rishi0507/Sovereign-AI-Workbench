// Package testutil lets a Go test binary impersonate the docker and nft command-line tools.
// A test sets WB_FAKE_ROLE and points the configured binary at os.Args[0]; TestMain calls
// MaybeRunFake before running the tests.
package testutil

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// MaybeRunFake exits the process after acting as a fake binary when WB_FAKE_ROLE is set.
func MaybeRunFake() {
	switch os.Getenv("WB_FAKE_ROLE") {
	case "docker":
		os.Exit(fakeDocker(os.Args[1:]))
	case "nft":
		os.Exit(fakeNft(os.Args[1:]))
	}
}

func logArgs(args []string) {
	path := os.Getenv("WB_FAKE_LOG")
	if path == "" {
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	b, _ := json.Marshal(args)
	f.Write(append(b, '\n'))
}

func fakeDocker(args []string) int {
	logArgs(args)
	if len(args) == 0 {
		return 2
	}
	switch args[0] {
	case "version":
		if os.Getenv("WB_FAKE_DOCKER_DOWN") != "" {
			fmt.Fprintln(os.Stderr, "Cannot connect to the Docker daemon")
			return 1
		}
		fmt.Println("27.0.1")
		return 0
	case "kill":
		return 0
	case "run":
	default:
		return 2
	}
	jobDir := ""
	for i := 0; i < len(args)-1; i++ {
		if args[i] == "-v" && strings.HasSuffix(args[i+1], ":/workspace:rw") {
			jobDir = strings.TrimSuffix(args[i+1], ":/workspace:rw")
		}
	}
	if ms, err := strconv.Atoi(os.Getenv("WB_FAKE_SLEEP_MS")); err == nil {
		time.Sleep(time.Duration(ms) * time.Millisecond)
	}
	fmt.Print(os.Getenv("WB_FAKE_STDOUT"))
	fmt.Fprint(os.Stderr, os.Getenv("WB_FAKE_STDERR"))
	if name := os.Getenv("WB_FAKE_WRITE"); name != "" && jobDir != "" {
		os.WriteFile(filepath.Join(jobDir, name), []byte("result"), 0o600)
	}
	if addr := os.Getenv("WB_FAKE_NET"); addr != "" && jobDir != "" {
		line := fmt.Sprintf(`{"ts":"2026-09-15T10:00:00Z","kind":"connect","addr":%q}`+"\n", addr)
		os.WriteFile(filepath.Join(jobDir, ".net_attempts.jsonl"), []byte(line), 0o600)
	}
	code, _ := strconv.Atoi(os.Getenv("WB_FAKE_EXIT"))
	return code
}

func fakeNft(args []string) int {
	logArgs(args)
	if os.Getenv("WB_FAKE_NFT_FAIL") != "" {
		fmt.Fprintln(os.Stderr, "Error: No such file or directory")
		return 1
	}
	data, err := os.ReadFile(os.Getenv("WB_FAKE_NFT_OUT"))
	if err != nil {
		return 1
	}
	os.Stdout.Write(data)
	return 0
}
