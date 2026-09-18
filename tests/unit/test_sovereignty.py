"""Sovereignty in code (PRD section 6.3): no network, no telemetry, no external URLs."""

from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest

from tests.helpers import ROOT
from workbench.security import egress_guard
from workbench.security.manifest import parse_manifest, verify, verify_signature, write_manifest

PACKAGE = ROOT / "workbench"
FORBIDDEN_MODULES = {"requests", "urllib3", "huggingface_hub", "transformers", "torch", "vllm", "paddleocr",
                     "sentry_sdk", "posthog", "segment", "mixpanel", "opentelemetry", "aiohttp", "boto3",
                     "openai", "anthropic"}
URL_RE = re.compile(r"https?://(?!127\.0\.0\.1|localhost|\[::1\]|local[\"'/])[A-Za-z0-9.-]+")


def python_files() -> list[Path]:
    return [p for p in PACKAGE.rglob("*.py") if "optional" not in p.parts]


def test_no_forbidden_imports() -> None:
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN_MODULES, f"{path}: imports {name}"
        text = path.read_text(encoding="utf-8")
        assert "urlopen(" not in text, f"{path} uses urllib.request.urlopen"


def test_no_hardcoded_remote_urls_outside_docs() -> None:
    # config/groq.yaml names a hosted service on purpose (D40) and is the only file allowed to.
    for path in [*python_files(), *PACKAGE.rglob("*.j2"), *PACKAGE.rglob("*.html"), *PACKAGE.rglob("*.js"),
                 *PACKAGE.rglob("*.css"), *(p for p in (ROOT / "config").glob("*.yaml") if p.name != "groq.yaml")]:
        text = path.read_text(encoding="utf-8")
        hits = [m.group(0) for m in URL_RE.finditer(text) if "www.w3.org" not in m.group(0)]
        assert not hits, f"{path}: {hits}"


def test_offline_environment_defaults() -> None:
    import os

    import workbench.settings  # noqa: F401

    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "VLLM_NO_USAGE_STATS", "DO_NOT_TRACK"):
        assert os.environ.get(key) == "1"


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_sockets_are_disabled_in_tests() -> None:
    from pytest_socket import SocketBlockedError, SocketConnectBlockedError

    with pytest.raises((SocketBlockedError, SocketConnectBlockedError)):
        socket.create_connection(("1.1.1.1", 443), 1)


def test_egress_guard_blocks_and_reports() -> None:
    reports: list[dict[str, object]] = []
    egress_guard.install({("10.20.0.10", 636)}, reports.append)
    try:
        before = egress_guard.blocked_count()
        with pytest.raises(PermissionError):
            socket.create_connection(("203.0.113.5", 443), 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(PermissionError):
                s.connect(("198.51.100.7", 80))
            assert s.connect_ex(("198.51.100.8", 80)) == 13
        finally:
            s.close()
        assert egress_guard._is_allowed(("127.0.0.1", 8001), socket.AF_INET, set())
        assert egress_guard._is_allowed(("::ffff:127.0.0.1", 8001), socket.AF_INET6, set())
        assert egress_guard._is_allowed(("10.20.0.10", 636), socket.AF_INET, {("10.20.0.10", 636)})
        assert not egress_guard._is_allowed(("10.20.0.10", 389), socket.AF_INET, {("10.20.0.10", 636)})
        assert not egress_guard._is_allowed(("example.com", 443), socket.AF_INET, set())
        assert egress_guard.blocked_count() == before + 3
        assert [r["addr"] for r in reports] == ["203.0.113.5:443", "198.51.100.7:80", "198.51.100.8:80"]
        assert all(r["origin"] == "host" and r["pid"] for r in reports)
        assert egress_guard.installed()
        allow = egress_guard.load_allowlist(ROOT / "config" / "egress_allowlist.yaml")
        assert allow == {("10.20.0.10", 636), ("api.groq.com", 443)}
        # A name is allowed only for the addresses it resolves to, and only on its own port.
        assert not egress_guard._is_allowed(("203.0.113.5", 443), socket.AF_INET, {("api.groq.com", 443)})
    finally:
        egress_guard.uninstall()
    assert not egress_guard.installed()


def test_manifest_verification(tmp_path: Path) -> None:
    (tmp_path / "weights").mkdir()
    a = tmp_path / "weights" / "model.safetensors"
    a.write_bytes(b"weights")
    b = tmp_path / "wheel.whl"
    b.write_bytes(b"wheel")
    manifest = tmp_path / "MANIFEST.sha256"
    manifest.write_text(write_manifest(tmp_path, [a, b]) + f"{'0' * 64}  missing.bin\n", encoding="utf-8")
    problems = verify(manifest, tmp_path)
    assert [p.problem for p in problems] == ["missing"]
    a.write_bytes(b"tampered")
    assert {p.problem for p in verify(manifest, tmp_path)} == {"missing", "hash mismatch"}
    evil = tmp_path / "evil.sha256"
    evil.write_text(f"{'1' * 64}  ../outside\n", encoding="utf-8")
    assert verify(evil, tmp_path)[0].problem == "path escapes root"
    with pytest.raises(ValueError):
        parse_manifest("not a manifest line")
    assert verify_signature(manifest, tmp_path / "sig") is NotImplemented


def test_deploy_artifacts_are_not_executed_by_the_app() -> None:
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        assert "nftables.conf" not in text and "sinkhole.sh" not in text, path
