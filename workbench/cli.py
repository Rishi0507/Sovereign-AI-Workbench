"""Command-line interface: ``workbench --help``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
import yaml

from workbench import __version__
from workbench.settings import Settings, get_settings

app = typer.Typer(help="Sovereign AI Workbench: agentic layer.", no_args_is_help=True, add_completion=False)
registry_app = typer.Typer(help="Model registry commands.", no_args_is_help=True)
kb_app = typer.Typer(help="Knowledge-base commands.", no_args_is_help=True)
audit_app = typer.Typer(help="Audit log commands.", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
app.add_typer(kb_app, name="kb")
app.add_typer(audit_app, name="audit")


def _guard(settings: Settings) -> None:
    if not settings.egress_guard:
        return
    from workbench.security import egress_guard

    egress_guard.install(egress_guard.load_allowlist(settings.config_dir / "egress_allowlist.yaml"))


def _echo(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def setup(reset: bool = typer.Option(False, help="Delete the data directory first.")) -> None:
    """Create runtime folders, seed demo workspaces and ingest the knowledge base."""
    from workbench.runtime import setup_environment

    s = get_settings()
    _guard(s)
    _echo(setup_environment(s, reset=reset))


@app.command()
def ingest(source: Path = typer.Argument(None, help="Folder of Markdown documents (default: settings.kb_source)."),
           asset_register: Path = typer.Option(None, help="Asset register CSV.")) -> None:
    """Ingest documents into the knowledge base and the plant graph."""
    from workbench.kb.ingest import ingest as do_ingest
    from workbench.runtime import Runtime

    s = get_settings()
    _guard(s)
    rt = Runtime(s)
    try:
        _echo(do_ingest(rt.kb, source or s.path(s.kb_source), asset_register or s.path(s.asset_register)))
    finally:
        rt.close()


@app.command()
def serve(host: str = typer.Option("127.0.0.1", help="Bind address."), port: int = 8080) -> None:
    """Start the API and the UI."""
    import uvicorn

    from workbench.api.app import create_app

    get_settings()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        typer.echo(f"Serving on {host}: make sure only the plant LAN can reach this address.", err=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command()
def demo(runs: int = typer.Option(1, help="Repeat each trace this many times.")) -> None:
    """Run Traces A to E headless with automatic approvals and print a summary table."""
    import shutil
    import tempfile

    from workbench.agent.approvals import AutoApprove
    from workbench.eval.runner import TRACES, isolated_settings, run_trace, trace_row
    from workbench.runtime import Runtime, setup_environment

    base = get_settings()
    _guard(base)
    tmp = Path(tempfile.mkdtemp(prefix="wb-demo-"))
    try:
        s = isolated_settings(base, tmp)
        setup_environment(s)
        rt = Runtime(s, approvals=AutoApprove())
        if rt.sandbox.health().get("status") != "ok":
            typer.echo("sandboxd is not running; using the in-process fake sandbox and egress monitor.", err=True)
            rt.close()
            s = s.model_copy(update={"sandbox": "fake", "egress": "fake"})
            rt = Runtime(s, approvals=AutoApprove())
        rows = []
        try:
            for _ in range(runs):
                for key in TRACES:
                    state, seconds = run_trace(rt, key)
                    rows.append(trace_row(key, state, seconds, rt))
            egress = rt.egress.run_test()
            snap = rt.egress.snapshot()
            out_dir = base.path(base.reports_dir) / "demo"
            out_dir.mkdir(parents=True, exist_ok=True)
            for f in (s.path(s.workspaces_root)).rglob("final/*"):
                shutil.copy2(f, out_dir / f"{f.parent.parent.name}-{f.name}")
        finally:
            rt.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    header = f"{'trace':<6} {'status':<11} {'route':<9} {'model':<17} {'plan':<24} {'fb':>2} {'esc':>3} " \
             f"{'sbx':>3} {'mism':>4} {'unsrc':>5} {'label':<28} {'sec':>6}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        typer.echo(f"{r['trace']:<6} {r['status']:<11} {r['route']!s:<9} {r['model']!s:<17} "
                   f"{(r['template'] or 'compiled (' + str(r['repairs']) + ' repair)'):<24} {r['fallbacks']:>2} "
                   f"{r['escalations']:>3} {r['sandbox_runs']:>3} {r['checks'].get('mismatch', 0):>4} "
                   f"{r['provenance']['unsourced']:>5} {r['label']:<28} {r['seconds']:>6}")
    typer.echo("")
    typer.echo("files: " + ", ".join(f for r in rows for f in r["files"]))
    typer.echo(f"egress test: {'PASS' if egress.get('pass') else 'FAIL'} "
               + " · ".join(f"{c['name']}={'pass' if c['pass'] else 'fail'}" for c in egress["checks"]))
    typer.echo(f"egress counters: external={snap.get('external_connections')} packets={snap.get('blocked_packets')} "
               f"connect(host)={snap.get('blocked_connect_host')} connect(sandbox)={snap.get('blocked_connect_sandbox')}")
    typer.echo(f"approved deliverables copied to {base.path(base.reports_dir) / 'demo'}")
    if any(r["status"] != "completed" for r in rows):
        raise typer.Exit(1)


@app.command(name="eval")
def evaluate(write_registry: bool = typer.Option(False, "--write-registry", help="Write config/models.proposed.yaml."),
             refresh: bool = typer.Option(False, "--refresh", help="Fold reviewed task outcomes into the diff."),
             runs: int = typer.Option(3, help="Reliability repetitions for Traces A and B.")) -> None:
    """Run the evaluation set and write reports/eval.md."""
    from workbench.eval.runner import run_all

    s = get_settings()
    _guard(s)
    report = run_all(s, reliability_runs=runs, write_registry=write_registry or refresh, refresh=refresh)
    typer.echo((s.path(s.reports_dir) / "eval.md").read_text(encoding="utf-8"))
    typer.echo(f"routing accuracy {report['routing']['accuracy']:.2f}")


@app.command(name="render-go-config")
def render_go_config(
    out: Path = typer.Option(None, help="Output folder (default: config/go)."),
    run_dir: Path = typer.Option(None, help="Socket folder (default: settings.run_dir)."),
    workspace_root: Path = typer.Option(None, help="Root that job directories must live under."),
    backend: str = typer.Option("auto", help="sandboxd backend: auto, docker or dev."),
    mode: str = typer.Option("dev", help="egressd mode: dev or enforced."),
) -> None:
    """Render config/go/*.json from the YAML configuration (one source of truth)."""
    s = get_settings()
    out = out or s.config_dir / "go"
    run = (run_dir or s.path(s.run_dir)).resolve()
    ws_root = (workspace_root or s.path(s.workspaces_root)).resolve()
    allow = yaml.safe_load((s.config_dir / "egress_allowlist.yaml").read_text(encoding="utf-8")) or {}
    transport = s.transport
    probe = (Path(__file__).resolve().parent / "security" / "probe").resolve()
    sandboxd = {
        "socket": str(run / "sandboxd.sock"), "transport": transport, "workspace_root": str(ws_root),
        "backend": backend, "docker_bin": "docker", "image_allowlist": [s.sandbox_image],
        "runtime_allowlist": ["runc", "runsc"], "max_concurrent": 2,
        "defaults": {"timeout_s": s.sandbox_timeout_s, "cpus": 2, "memory_mb": 2048, "pids": 256, "tmpfs_mb": 256},
        "limits": {"timeout_s": 300, "cpus": 4, "memory_mb": 4096, "pids": 512},
        "stdout_tail_bytes": 4000, "traceback_head_lines": 15, "probe_dir": str(probe),
        "dev_python": sys.executable, "egressd_socket": str(run / "egressd.sock"), "run_dir": str(run),
    }
    egressd = {
        "socket": str(run / "egressd.sock"), "transport": transport, "mode": mode,
        "lan_cidrs": allow.get("lan_cidrs") or [],
        "allowlist": [{"cidr": f"{a['host']}/32", "port": int(a["port"]), "proto": a.get("proto", "tcp")}
                      for a in allow.get("allow") or []],
        "nft": {"bin": "nft", "table": "inet sovereign", "counter": "egress_blocked", "poll_s": 2},
        "conntrack": {"path": "/proc/net/nf_conntrack", "poll_s": 2},
        "auditlog": {"path": "/var/log/audit/audit.log", "enabled": False},
        "sandbox_cgroup_markers": ["docker", "wb-"],
        "sandboxd_socket": str(run / "sandboxd.sock"),
        "probe_job_dir": str(ws_root / "_egress_probe"),
        "event_buffer": 1000, "state_file": str(run / "egressd_state.json"), "run_dir": str(run),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "sandboxd.json").write_text(json.dumps(sandboxd, indent=2) + "\n", encoding="utf-8")
    (out / "egressd.json").write_text(json.dumps(egressd, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"wrote {out / 'sandboxd.json'} and {out / 'egressd.json'} (transport {transport})")


@registry_app.command("render-serve")
def render_serve(profile: str = typer.Option("S", help="Hardware profile."),
                 out: Path = typer.Option(None, help="Output script (default: deploy/vllm_commands.sh).")) -> None:
    """Write the vllm serve commands for a hardware profile."""
    from workbench.registry.models import load_registry
    from workbench.registry.serve_cmd import write_script

    s = get_settings()
    reg = load_registry(s.config_dir / "models.yaml", profile)
    ps = s.profiles.get(profile)
    path = write_script(reg, profile, out or s.root / "deploy" / "vllm_commands.sh", ps)
    typer.echo(f"wrote {path}")


@registry_app.command("shadow")
def shadow(name: str) -> None:
    """Evaluate a shadow entry offline and write the proposed quality table."""
    from workbench.registry.shadow import run_shadow
    from workbench.runtime import Runtime

    s = get_settings()
    _guard(s)
    rt = Runtime(s)
    try:
        _echo(run_shadow(rt, name, "cli")["quality"])
    finally:
        rt.close()


@registry_app.command("promote")
def promote(name: str, confirm: bool = typer.Option(False, "--confirm", help="Required.")) -> None:
    """Promote a shadow entry to active (one field change, audited)."""
    from workbench.registry.shadow import promote as do_promote
    from workbench.runtime import Runtime

    rt = Runtime(get_settings())
    try:
        _echo(do_promote(rt, name, confirm, "cli"))
    finally:
        rt.close()


@kb_app.command("impact")
def impact(doc_number: str, revision: str) -> None:
    """Print the supersession impact report for a new revision."""
    from workbench.kb.impact import impact_report
    from workbench.runtime import Runtime

    rt = Runtime(get_settings())
    try:
        typer.echo(impact_report(rt.kb, doc_number, revision)["markdown"])
    finally:
        rt.close()


@kb_app.command("stats")
def kb_stats() -> None:
    """Print knowledge-base statistics."""
    from workbench.runtime import Runtime

    rt = Runtime(get_settings())
    try:
        _echo(rt.kb.stats())
    finally:
        rt.close()


@audit_app.command("verify")
def audit_verify() -> None:
    """Verify the hash chain of the audit log."""
    from workbench.core.audit import AuditLog

    s = get_settings()
    detail = AuditLog(s.path(s.audit_path)).verify_detail()
    _echo(detail)
    if not detail["ok"]:
        raise typer.Exit(1)


@audit_app.command("summary")
def audit_summary(day: str) -> None:
    """Print the (unsigned) daily summary for YYYY-MM-DD."""
    from datetime import date

    from workbench.core.audit import AuditLog

    s = get_settings()
    _echo(AuditLog(s.path(s.audit_path)).daily_summary(date.fromisoformat(day)))


@app.command(name="manifest-verify")
def manifest_verify(manifest: Path, root: Path) -> None:
    """Verify a SHA-256 manifest against a folder."""
    from workbench.security.manifest import verify

    problems = verify(manifest, root)
    _echo([p.model_dump() for p in problems])
    if problems:
        raise typer.Exit(1)


@app.command(name="run")
def run_task(text: str, workspace: str = typer.Option("plant-a"), user: str = typer.Option("engineer1"),
             attach: list[str] = typer.Option([], help="Workspace-relative attachment (repeatable)."),
             auto_approve: bool = typer.Option(False, "--auto-approve", help="Approve every gate automatically.")) -> None:
    """Run one task headless."""
    from workbench.agent.approvals import AutoApprove
    from workbench.runtime import Runtime

    if not auto_approve:
        typer.echo("Headless runs need --auto-approve; use the UI for interactive approvals.", err=True)
        raise typer.Exit(2)
    s = get_settings()
    _guard(s)
    rt = Runtime(s, approvals=AutoApprove())
    try:
        state = rt.orchestrator.create_task(workspace, user, text, list(attach))
        done = rt.jobs.run_inline(state.id)
        typer.echo((done.route or {}).get("log_line", ""))
        for t in done.trace:
            typer.echo(f"{t.n:>3} {t.kind:<10} {t.step_id or ''!s:<10} {t.tool or t.purpose or ''!s:<18} "
                       f"{'ok ' if t.ok else 'ERR'} {t.summary[:110]}")
        typer.echo(f"status: {done.status} {done.status_note}")
        for d in done.deliverables:
            typer.echo(f"deliverable: {d.relpath} ({d.label.display()}) provenance {d.provenance.get('counts')}")
    finally:
        rt.close()


if __name__ == "__main__":
    app()
