// Command sandboxd runs agent code in isolated containers (or a development subprocess) and
// reports blocked network attempts to egressd. It listens only on a Unix socket.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"workbench.local/sovereign/internal/config"
	"workbench.local/sovereign/internal/sandbox"
	"workbench.local/sovereign/internal/udsclient"
	"workbench.local/sovereign/internal/udsserver"
)

var version = "dev"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	fs := flag.NewFlagSet("sandboxd", flag.ContinueOnError)
	cfgPath := fs.String("config", "config/go/sandboxd.json", "configuration file")
	showVersion := fs.Bool("version", false, "print the version and exit")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *showVersion {
		fmt.Println(version)
		return 0
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("service", "sandboxd")
	var cfg sandbox.Config
	if err := config.Load(*cfgPath, &cfg); err != nil {
		log.Error("configuration", "err", err)
		return 1
	}
	ctx := context.Background()
	ep := udsclient.FromSocket(cfg.EgressdSocket, cfg.Transport, cfg.RunDir)
	reporter := sandbox.NewEgressReporter(ep, filepath.Join(cfg.RunDir, "egress_spool.jsonl"))
	runner := sandbox.NewRunner(ctx, &cfg, reporter, log)
	ln, err := udsserver.Listen(udsserver.Options{Name: "sandboxd", Socket: cfg.Socket, Transport: cfg.Transport,
		RunDir: cfg.RunDir})
	if err != nil {
		log.Error("listen", "err", err)
		return 1
	}
	log.Info("starting", "version", version, "backend", runner.Backend().Name(), "workspace_root", cfg.WorkspaceRoot)
	if err := udsserver.Serve(ctx, log, ln, sandbox.NewHandler(&cfg, runner, version, time.Now())); err != nil {
		log.Error("serve", "err", err)
		return 1
	}
	return 0
}
