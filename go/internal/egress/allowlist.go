package egress

import (
	"net/netip"
	"strconv"
	"strings"
)

// Policy decides which destinations count as external.
type Policy struct {
	lan   []netip.Prefix
	allow []allowRule
}

type allowRule struct {
	prefix netip.Prefix
	port   int
	proto  string
}

// NewPolicy parses LAN prefixes and allowlist entries (already validated).
func NewPolicy(lan []string, allow []AllowEntry) *Policy {
	p := &Policy{}
	for _, c := range lan {
		if pr, err := netip.ParsePrefix(c); err == nil {
			p.lan = append(p.lan, pr.Masked())
		}
	}
	for _, a := range allow {
		if pr, err := netip.ParsePrefix(a.CIDR); err == nil {
			proto := a.Proto
			if proto == "" {
				proto = "tcp"
			}
			p.allow = append(p.allow, allowRule{prefix: pr.Masked(), port: a.Port, proto: proto})
		}
	}
	return p
}

// Allowed reports whether a connection to addr:port is permitted without counting.
func (p *Policy) Allowed(addr netip.Addr, port int, proto string) bool {
	addr = addr.Unmap()
	if addr.IsLoopback() || addr.IsUnspecified() {
		return true
	}
	for _, r := range p.allow {
		if r.prefix.Contains(addr) && r.port == port && (proto == "" || r.proto == proto) {
			return true
		}
	}
	return false
}

// External reports whether a destination is outside loopback, the LAN and the allowlist.
func (p *Policy) External(addr netip.Addr, port int, proto string) bool {
	addr = addr.Unmap()
	if p.Allowed(addr, port, proto) {
		return false
	}
	for _, pr := range p.lan {
		if pr.Contains(addr) {
			return false
		}
	}
	return !addr.IsLinkLocalUnicast() && !addr.IsLinkLocalMulticast() && !addr.IsMulticast()
}

// SplitHostPort parses "1.2.3.4:443" or "[::1]:443".
func SplitHostPort(s string) (netip.Addr, int, bool) {
	if ap, err := netip.ParseAddrPort(s); err == nil {
		return ap.Addr(), int(ap.Port()), true
	}
	i := strings.LastIndex(s, ":")
	if i < 0 {
		return netip.Addr{}, 0, false
	}
	addr, err := netip.ParseAddr(strings.Trim(s[:i], "[]"))
	if err != nil {
		return netip.Addr{}, 0, false
	}
	port, err := strconv.Atoi(s[i+1:])
	if err != nil {
		return netip.Addr{}, 0, false
	}
	return addr, port, true
}
