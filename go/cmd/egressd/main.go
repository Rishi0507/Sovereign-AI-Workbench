// Command egressd counts and proves blocked outbound connections and runs the egress test.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"workbench.local/sovereign/internal/config"
	"workbench.local/sovereign/internal/egress"
	"workbench.local/sovereign/internal/udsclient"
	"workbench.local/sovereign/internal/udsserver"
)

var version = "dev"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	fs := flag.NewFlagSet("egressd", flag.ContinueOnError)
	cfgPath := fs.String("config", "config/go/egressd.json", "configuration file")
	showVersion := fs.Bool("version", false, "print the version and exit")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *showVersion {
		fmt.Println(version)
		return 0
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("service", "egressd")
	var cfg egress.Config
	if err := config.Load(*cfgPath, &cfg); err != nil {
		log.Error("configuration", "err", err)
		return 1
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if cfg.Mode == "enforced" {
		if err := egress.CheckEnforced(ctx, &cfg, egress.RunCommand); err != nil {
			log.Error("refusing to start in enforced mode", "err", err)
			return 1
		}
	}
	st := egress.NewState(cfg.Mode, cfg.StateFile, cfg.EventBuffer, nil)
	policy := egress.NewPolicy(cfg.LanCIDRs, cfg.Allowlist)
	collectors := []egress.Collector{
		egress.NewNftCollector(cfg.Nft),
		egress.NewConntrackCollector(cfg.Conntrack, policy),
		egress.NewAuditCollector(cfg.Auditlog, policy, cfg.SandboxCgroupMarkers),
	}
	for _, c := range collectors {
		if err := c.Start(ctx, st); err != nil {
			log.Warn("collector failed to start", "collector", c.Name(), "err", err)
		}
		log.Info("collector", "name", c.Name(), "status", c.Status())
	}
	st.SetStatus("reports", "ok")
	tester := &egress.Tester{Mode: cfg.Mode, State: st, ProbeDir: cfg.ProbeJobDir, Wait: 5 * time.Second,
		Sandbox: egress.NewSandboxdClient(udsclient.FromSocket(cfg.SandboxdSocket, cfg.Transport, cfg.RunDir))}
	if cfg.Mode == "enforced" {
		tester.Dialer, tester.Resolver = egress.RealDialer(), egress.RealResolver()
	} else {
		tester.Dialer, tester.Resolver = egress.GuardedDialer{State: st}, egress.SimulatedResolver{}
	}
	go egress.Persist(ctx, st, 10*time.Second)
	ln, err := udsserver.Listen(udsserver.Options{Name: "egressd", Socket: cfg.Socket, Transport: cfg.Transport,
		RunDir: cfg.RunDir})
	if err != nil {
		log.Error("listen", "err", err)
		return 1
	}
	log.Info("starting", "version", version, "mode", cfg.Mode)
	err = udsserver.Serve(ctx, log, ln, egress.NewHandler(st, tester, version, time.Now()))
	cancel()
	_ = st.Save()
	if err != nil {
		log.Error("serve", "err", err)
		return 1
	}
	return 0
}
