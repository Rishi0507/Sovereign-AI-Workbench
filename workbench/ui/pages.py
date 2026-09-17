"""Server-rendered page shells. Content is rendered in the browser from the JSON API."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from workbench import __version__

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter(include_in_schema=False)
TASK_RE = re.compile(r"^T[0-9A-F]{8}$")


STATIC = Path(__file__).resolve().parent / "static"


def asset_version() -> str:
    """Content hash of the static bundle, so browsers never keep a stale script."""
    h = hashlib.sha256(__version__.encode())
    for name in ("app.js", "app.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:12]


def _page(request: Request, name: str, page: str, **ctx: object) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, {"page": page, "version": __version__,
                                                      "asset_version": asset_version(), **ctx})


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return _page(request, "home.html", "home", title="Workspace")


@router.get("/t/{task_id}", response_class=HTMLResponse)
def task(request: Request, task_id: str) -> HTMLResponse:
    if not TASK_RE.match(task_id):
        raise HTTPException(404)
    return _page(request, "task.html", "task", title="Task", task_id=task_id)


@router.get("/t/{task_id}/review", response_class=HTMLResponse)
def review(request: Request, task_id: str) -> HTMLResponse:
    if not TASK_RE.match(task_id):
        raise HTTPException(404)
    return _page(request, "review.html", "review", title="Draft review", task_id=task_id)


@router.get("/library", response_class=HTMLResponse)
def library(request: Request) -> HTMLResponse:
    return _page(request, "library.html", "library", title="Library")


@router.get("/models", response_class=HTMLResponse)
def models(request: Request) -> HTMLResponse:
    return _page(request, "models.html", "models", title="Models")


@router.get("/security", response_class=HTMLResponse)
def security(request: Request) -> HTMLResponse:
    return _page(request, "security.html", "security", title="Security")


@router.get("/favicon.ico")
def favicon() -> Response:
    svg = Path(__file__).resolve().parent / "static" / "mark.svg"
    return Response(svg.read_bytes(), media_type="image/svg+xml")
