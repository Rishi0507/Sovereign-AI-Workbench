package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"workbench.local/sovereign/internal/udsserver"
)

// NewHandler returns the sandboxd HTTP API.
func NewHandler(cfg *Config, runner *Runner, version string, started time.Time) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		running, queued := runner.Counts()
		udsserver.WriteJSON(w, http.StatusOK, map[string]any{
			"status": "ok", "version": version, "backend": runner.Backend().Name(),
			"isolation": runner.Backend().Isolation(), "uptime_s": int(time.Since(started).Seconds()),
			"running": running, "queued": queued, "max_concurrent": cfg.MaxConcurrent,
		})
	})
	mux.HandleFunc("POST /v1/run", func(w http.ResponseWriter, r *http.Request) {
		var req RunRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			udsserver.WriteError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		job, err := Validate(cfg, req)
		if err != nil {
			udsserver.WriteError(w, http.StatusBadRequest, "invalid_request", err.Error())
			return
		}
		res, err := runner.Run(r.Context(), job)
		switch {
		case err == nil:
			udsserver.WriteJSON(w, http.StatusOK, res)
		case errors.Is(err, ErrBusy):
			var body udsserver.ErrorBody
			body.Error.Code, body.Error.Message, body.RetryAfterS = "busy", err.Error(), 5
			udsserver.WriteJSON(w, http.StatusTooManyRequests, body)
		case errors.Is(err, ErrDuplicate):
			udsserver.WriteError(w, http.StatusConflict, "duplicate", err.Error())
		case errors.Is(err, context.Canceled):
			udsserver.WriteError(w, 499, "cancelled", "client went away")
		default:
			udsserver.WriteError(w, http.StatusInternalServerError, "run_failed", err.Error())
		}
	})
	mux.HandleFunc("GET /v1/runs/{id}", func(w http.ResponseWriter, r *http.Request) {
		res, ok := runner.Result(r.PathValue("id"))
		if !ok {
			udsserver.WriteError(w, http.StatusNotFound, "not_found", "no result for this run id")
			return
		}
		udsserver.WriteJSON(w, http.StatusOK, res)
	})
	mux.HandleFunc("POST /v1/runs/{id}/kill", func(w http.ResponseWriter, r *http.Request) {
		if !runner.Kill(r.PathValue("id")) {
			udsserver.WriteError(w, http.StatusNotFound, "not_running", "no running job with this id")
			return
		}
		udsserver.WriteJSON(w, http.StatusOK, map[string]any{"killed": r.PathValue("id")})
	})
	return mux
}
