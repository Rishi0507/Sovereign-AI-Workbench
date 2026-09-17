// Package egress counts and proves blocked outbound connections: nftables counters,
// conntrack, audit-log connect() records and reports from the app and the sandbox.
package egress

import (
	"errors"
	"fmt"
	"net/netip"
	"path/filepath"
)

// AllowEntry is one allowlisted destination.
type AllowEntry struct {
	CIDR  string `json:"cidr"`
	Port  int    `json:"port"`
	Proto string `json:"proto"`
}

// NftConfig configures the nftables counter collector.
type NftConfig struct {
	Bin     string `json:"bin"`
	Table   string `json:"table"`
	Counter string `json:"counter"`
	PollS   int    `json:"poll_s"`
}

// ConntrackConfig configures the conntrack collector.
type ConntrackConfig struct {
	Path  string `json:"path"`
	PollS int    `json:"poll_s"`
}

// AuditConfig configures the audit-log collector.
type AuditConfig struct {
	Path    string `json:"path"`
	Enabled bool   `json:"enabled"`
}

// Config is config/go/egressd.json.
type Config struct {
	Socket               string          `json:"socket"`
	Transport            string          `json:"transport"`
	Mode                 string          `json:"mode"`
	LanCIDRs             []string        `json:"lan_cidrs"`
	Allowlist            []AllowEntry    `json:"allowlist"`
	Nft                  NftConfig       `json:"nft"`
	Conntrack            ConntrackConfig `json:"conntrack"`
	Auditlog             AuditConfig     `json:"auditlog"`
	SandboxCgroupMarkers []string        `json:"sandbox_cgroup_markers"`
	SandboxdSocket       string          `json:"sandboxd_socket"`
	ProbeJobDir          string          `json:"probe_job_dir"`
	EventBuffer          int             `json:"event_buffer"`
	StateFile            string          `json:"state_file"`
	RunDir               string          `json:"run_dir"`
	ResolvConf           string          `json:"resolv_conf,omitempty"`
}

// Validate checks the configuration and fills defaults.
func (c *Config) Validate() error {
	var errs []error
	if c.Socket == "" {
		errs = append(errs, errors.New("socket is required"))
	}
	if c.Mode != "dev" && c.Mode != "enforced" {
		errs = append(errs, fmt.Errorf("mode %q must be dev or enforced", c.Mode))
	}
	if c.Transport != "" && c.Transport != "unix" && c.Transport != "tcp" {
		errs = append(errs, fmt.Errorf("transport %q must be unix or tcp", c.Transport))
	}
	for _, cidr := range c.LanCIDRs {
		if _, err := netip.ParsePrefix(cidr); err != nil {
			errs = append(errs, fmt.Errorf("lan_cidrs: %w", err))
		}
	}
	for _, a := range c.Allowlist {
		if _, err := netip.ParsePrefix(a.CIDR); err != nil {
			errs = append(errs, fmt.Errorf("allowlist: %w", err))
		}
		if a.Port < 1 || a.Port > 65535 {
			errs = append(errs, fmt.Errorf("allowlist port %d out of range", a.Port))
		}
	}
	if c.ProbeJobDir == "" || c.StateFile == "" {
		errs = append(errs, errors.New("probe_job_dir and state_file are required"))
	}
	if c.EventBuffer < 10 {
		c.EventBuffer = 1000
	}
	if c.Nft.PollS < 1 {
		c.Nft.PollS = 2
	}
	if c.Conntrack.PollS < 1 {
		c.Conntrack.PollS = 2
	}
	if c.RunDir == "" {
		c.RunDir = filepath.Dir(c.Socket)
	}
	if c.ResolvConf == "" {
		c.ResolvConf = "/etc/resolv.conf"
	}
	return errors.Join(errs...)
}
