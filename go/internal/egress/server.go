package egress

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"workbench.local/sovereign/internal/udsserver"
)

// CheckEnforced refuses to start unless the nftables table has an output chain with policy drop
// and no upstream nameserver is configured.
func CheckEnforced(ctx context.Context, cfg *Config, run func(context.Context, string, ...string) ([]byte, error)) error {
	fam, table, ok := strings.Cut(cfg.Nft.Table, " ")
	if !ok {
		return fmt.Errorf("nft.table %q must be '<family> <name>'", cfg.Nft.Table)
	}
	out, err := run(ctx, cfg.Nft.Bin, "-j", "list", "table", fam, table)
	if err != nil {
		return fmt.Errorf("enforced mode needs the nftables table %s: %w", cfg.Nft.Table, err)
	}
	check, err := ParseNftTable(out, fam, table)
	if err != nil {
		return err
	}
	if !check.TableFound {
		return fmt.Errorf("nftables table %s not found", cfg.Nft.Table)
	}
	if !check.OutputPolicyDrop {
		return fmt.Errorf("nftables table %s has no output chain with policy drop", cfg.Nft.Table)
	}
	data, err := os.ReadFile(cfg.ResolvConf)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if ResolvHasNameserver(string(data)) {
		return fmt.Errorf("%s configures a nameserver; upstream DNS must be disabled", cfg.ResolvConf)
	}
	return nil
}

// ReportBody is the body of POST /v1/report.
type ReportBody struct {
	Origin       string `json:"origin"`
	Addr         string `json:"addr"`
	Port         int    `json:"port,omitempty"`
	PID          int    `json:"pid,omitempty"`
	RunID        string `json:"run_id,omitempty"`
	TS           string `json:"ts,omitempty"`
	Source       string `json:"source,omitempty"`
	StackSummary string `json:"stack_summary,omitempty"`
}

// NewHandler returns the egressd API.
func NewHandler(st *State, tester *Tester, version string, started time.Time) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		udsserver.WriteJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": version,
			"mode": tester.Mode, "backend": tester.Mode, "uptime_s": int(time.Since(started).Seconds())})
	})
	mux.HandleFunc("GET /v1/snapshot", func(w http.ResponseWriter, r *http.Request) {
		udsserver.WriteJSON(w, http.StatusOK, st.Snapshot())
	})
	mux.HandleFunc("GET /v1/events", func(w http.ResponseWriter, r *http.Request) {
		since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
		limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
		if err != nil || limit < 1 || limit > 1000 {
			limit = 100
		}
		udsserver.WriteJSON(w, http.StatusOK, map[string]any{"events": st.Events(since, limit)})
	})
	mux.HandleFunc("POST /v1/report", func(w http.ResponseWriter, r *http.Request) {
		var body ReportBody
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&body); err != nil {
			udsserver.WriteError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		if body.Origin != "host" && body.Origin != "sandbox" {
			udsserver.WriteError(w, http.StatusBadRequest, "bad_origin", "origin must be host or sandbox")
			return
		}
		if body.Addr == "" || len(body.Addr) > 300 {
			udsserver.WriteError(w, http.StatusBadRequest, "bad_addr", "addr is required")
			return
		}
		if body.TS != "" {
			if _, err := time.Parse(time.RFC3339, body.TS); err != nil {
				body.TS = ""
			}
		}
		port := body.Port
		if _, p, ok := SplitHostPort(body.Addr); ok && port == 0 {
			port = p
		}
		source := body.Source
		if source == "" {
			source = "report"
		}
		counted := st.Record(Event{TS: body.TS, Origin: body.Origin, Addr: body.Addr, Port: port, PID: body.PID,
			RunID: body.RunID, Source: source})
		udsserver.WriteJSON(w, http.StatusAccepted, map[string]any{"counted": counted})
	})
	mux.HandleFunc("POST /v1/test", func(w http.ResponseWriter, r *http.Request) {
		res := tester.Run(r.Context())
		_ = st.Save()
		udsserver.WriteJSON(w, http.StatusOK, res)
	})
	return mux
}

// Persist saves the state every interval until ctx is done, and once more at the end.
func Persist(ctx context.Context, st *State, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			_ = st.Save()
			return
		case <-t.C:
			_ = st.Save()
		}
	}
}
