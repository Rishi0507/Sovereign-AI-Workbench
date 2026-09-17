package egress

import (
	"bufio"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
)

// ParseNftCounter extracts the packet count from `nft -j list counter ...` output.
func ParseNftCounter(data []byte, name string) (int64, error) {
	var doc struct {
		Nftables []map[string]json.RawMessage `json:"nftables"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return 0, fmt.Errorf("nft output: %w", err)
	}
	for _, item := range doc.Nftables {
		raw, ok := item["counter"]
		if !ok {
			continue
		}
		var c struct {
			Name    string `json:"name"`
			Packets int64  `json:"packets"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			return 0, err
		}
		if name == "" || c.Name == name {
			return c.Packets, nil
		}
	}
	return 0, errors.New("counter not found in nft output")
}

// TableCheck is the result of inspecting `nft -j list table inet sovereign`.
type TableCheck struct {
	TableFound       bool
	OutputPolicyDrop bool
}

// ParseNftTable reports whether the table exists and has an output hook chain with policy drop.
func ParseNftTable(data []byte, family, table string) (TableCheck, error) {
	var doc struct {
		Nftables []map[string]json.RawMessage `json:"nftables"`
	}
	var out TableCheck
	if err := json.Unmarshal(data, &doc); err != nil {
		return out, fmt.Errorf("nft output: %w", err)
	}
	for _, item := range doc.Nftables {
		if raw, ok := item["table"]; ok {
			var t struct{ Family, Name string }
			if json.Unmarshal(raw, &t) == nil && t.Family == family && t.Name == table {
				out.TableFound = true
			}
		}
		if raw, ok := item["chain"]; ok {
			var c struct {
				Family, Table, Hook, Policy string
			}
			if json.Unmarshal(raw, &c) == nil && c.Family == family && c.Table == table &&
				c.Hook == "output" && c.Policy == "drop" {
				out.OutputPolicyDrop = true
			}
		}
	}
	return out, nil
}

// ConntrackEntry is one parsed connection.
type ConntrackEntry struct {
	Proto string
	Dst   netip.Addr
	DPort int
}

// ParseConntrack reads /proc/net/nf_conntrack lines (the first dst= and dport= are the original
// direction).
func ParseConntrack(r io.Reader) []ConntrackEntry {
	var out []ConntrackEntry
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 3 {
			continue
		}
		e := ConntrackEntry{Proto: fields[2]}
		haveDst, havePort := false, false
		for _, f := range fields {
			switch {
			case !haveDst && strings.HasPrefix(f, "dst="):
				if a, err := netip.ParseAddr(f[4:]); err == nil {
					e.Dst, haveDst = a, true
				}
			case !havePort && strings.HasPrefix(f, "dport="):
				if p, err := strconv.Atoi(f[6:]); err == nil {
					e.DPort, havePort = p, true
				}
			}
		}
		if haveDst {
			out = append(out, e)
		}
	}
	return out
}

var (
	auditRe = regexp.MustCompile(`^type=(\w+) msg=audit\((\d+)\.(\d+):(\d+)\):(.*)$`)
	kvRe    = regexp.MustCompile(`(\w+)=("[^"]*"|\S+)`)
)

// AuditRecord is a joined connect() syscall with its socket address.
type AuditRecord struct {
	Serial  string
	Epoch   int64
	PID     int
	Success bool
	Addr    netip.Addr
	Port    int
	Family  int
}

// AuditJoiner joins SYSCALL and SOCKADDR records by event serial, across reads.
type AuditJoiner struct {
	syscalls map[string]map[string]string
	sockaddr map[string]string
	epochs   map[string]int64
}

// NewAuditJoiner returns an empty joiner.
func NewAuditJoiner() *AuditJoiner {
	return &AuditJoiner{syscalls: map[string]map[string]string{}, sockaddr: map[string]string{},
		epochs: map[string]int64{}}
}

// Feed consumes one audit.log line and returns a completed record when both halves are seen.
func (j *AuditJoiner) Feed(line string) (AuditRecord, bool) {
	m := auditRe.FindStringSubmatch(strings.TrimSpace(line))
	if m == nil {
		return AuditRecord{}, false
	}
	typ, serial := m[1], m[2]+"."+m[3]+":"+m[4]
	epoch, _ := strconv.ParseInt(m[2], 10, 64)
	kv := map[string]string{}
	for _, p := range kvRe.FindAllStringSubmatch(m[5], -1) {
		kv[p[1]] = strings.Trim(p[2], `"`)
	}
	switch typ {
	case "SYSCALL":
		if kv["syscall"] != "42" && kv["syscall"] != "connect" {
			return AuditRecord{}, false
		}
		j.syscalls[serial] = kv
		j.epochs[serial] = epoch
	case "SOCKADDR":
		j.sockaddr[serial] = kv["saddr"]
	default:
		return AuditRecord{}, false
	}
	sc, okA := j.syscalls[serial]
	sa, okB := j.sockaddr[serial]
	if !okA || !okB {
		if len(j.sockaddr) > 4096 {
			j.sockaddr = map[string]string{}
		}
		return AuditRecord{}, false
	}
	delete(j.syscalls, serial)
	delete(j.sockaddr, serial)
	ep := j.epochs[serial]
	delete(j.epochs, serial)
	fam, addr, port, ok := DecodeSockaddr(sa)
	if !ok {
		return AuditRecord{}, false
	}
	pid, _ := strconv.Atoi(sc["pid"])
	return AuditRecord{Serial: serial, Epoch: ep, PID: pid, Success: sc["success"] == "yes", Addr: addr,
		Port: port, Family: fam}, true
}

// DecodeSockaddr decodes a hex sockaddr_in or sockaddr_in6 (family in host byte order, port in
// network byte order).
func DecodeSockaddr(h string) (family int, addr netip.Addr, port int, ok bool) {
	b, err := hex.DecodeString(h)
	if err != nil || len(b) < 8 {
		return 0, netip.Addr{}, 0, false
	}
	family = int(binary.LittleEndian.Uint16(b[0:2]))
	port = int(binary.BigEndian.Uint16(b[2:4]))
	switch family {
	case 2:
		var a [4]byte
		copy(a[:], b[4:8])
		return family, netip.AddrFrom4(a), port, true
	case 10:
		if len(b) < 24 {
			return 0, netip.Addr{}, 0, false
		}
		var a [16]byte
		copy(a[:], b[8:24])
		return family, netip.AddrFrom16(a), port, true
	}
	return family, netip.Addr{}, 0, false
}

// IsSandboxCgroup reports whether a /proc/<pid>/cgroup content belongs to a sandbox container.
func IsSandboxCgroup(content string, markers []string) bool {
	for _, m := range markers {
		if m != "" && strings.Contains(content, m) {
			return true
		}
	}
	return false
}

// ResolvHasNameserver reports whether resolv.conf content configures any nameserver.
func ResolvHasNameserver(content string) bool {
	for _, line := range strings.Split(content, "\n") {
		f := strings.Fields(line)
		if len(f) > 0 && f[0] == "nameserver" {
			return true
		}
	}
	return false
}
