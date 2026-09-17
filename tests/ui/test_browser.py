"""Browser tests of the interface, driven through the installed Chrome.

Skipped unless Playwright is installed (it is not a project dependency) and a Chrome browser is
available. They start the real application with the approval queue on a loopback port.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from workbench.agent.approvals import QueueApprove
from workbench.api.app import create_app
from workbench.runtime import Runtime

playwright_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.ui


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture()
def server(make_runtime: Callable[..., Runtime]) -> Iterator[str]:
    import uvicorn

    rt = make_runtime(approvals=QueueApprove(poll_s=0.05))
    rt.jobs.start()
    port = _free_port()
    config = uvicorn.Config(create_app(rt, install_guard=False), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture()
def page(server: str) -> Iterator[Any]:
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as exc:  # no Chrome on this machine
            pytest.skip(f"Chrome is not available: {exc}")
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        pg = context.new_page()
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.base = server  # type: ignore[attr-defined]
        pg.errors = errors  # type: ignore[attr-defined]
        yield pg
        browser.close()
    assert not errors, errors


def test_script_task_from_the_home_page_to_approval(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.get_by_role("button", name="Analyse sensor readings").click()
    expect(page.locator("#attached")).to_contain_text("pressure_readings.csv")
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    # Mark the conversation on screen; updates must patch it in place instead of rebuilding it.
    start = page.get_by_role("button", name="Start")
    playwright_api.expect(start).to_be_visible(timeout=15_000)
    page.evaluate("document.querySelector('.turn.assistant').__probe = 'kept'")
    start.click()
    ready = page.locator(".ready-card")
    expect(ready).to_contain_text("Ready for your approval", timeout=30_000)
    expect(page.locator(".status-pill")).to_have_text("Ready for your review")
    expect(page.locator(".preview img")).to_be_visible()
    ready.get_by_role("button", name="Approve").click()
    expect(page.locator(".status-pill")).to_have_text("Done", timeout=15_000)
    expect(page.locator(".file").first).to_contain_text("Final")
    assert page.evaluate("document.querySelector('.turn.assistant').__probe") == "kept"
    # The details panel slides in, switches tabs and closes with Escape.
    page.get_by_role("button", name="Details").click()
    expect(page.locator("#panel")).to_have_class("panel shown")
    page.locator("#panel").get_by_role("tab", name="Evidence").click()
    expect(page.locator("#panel .evidence").first).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).to_be_hidden()


def test_library_tabs_slide_and_filter(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.get_by_role("button", name="Summarise a contract").click()
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    expect(page.locator(".status-pill")).to_have_text("Needs your approval", timeout=15_000)

    page.goto(page.base + "/library")
    tabs = page.locator("#lib-tabs .seg")
    expect(tabs.get_by_role("tab", name="Waiting on you")).to_contain_text("1")
    thumb = page.locator(".seg-thumb")
    before = thumb.evaluate("el => el.getBoundingClientRect().left")
    tabs.get_by_role("tab", name="Waiting on you").click()
    expect(page.locator(".wait-card")).to_contain_text("Approve the plan")
    page.wait_for_timeout(600)
    after = thumb.evaluate("el => el.getBoundingClientRect().left")
    assert after > before + 40, (before, after)
    assert thumb.evaluate("el => getComputedStyle(el).transitionDuration") != "0s"
    tabs.get_by_role("tab", name="Approvals").click()
    expect(page.locator(".empty-state")).to_contain_text("No decisions yet")
    tabs.get_by_role("tab", name="Documents").click()
    expect(page.locator(".empty-state")).to_contain_text("No documents yet")
    page.locator("#lib-search").fill("nothing matches this")
    expect(page.locator(".empty-state")).to_contain_text("No matching documents")


def test_damaged_tab_storage_does_not_break_pages(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    expect(page.locator("#account-name")).to_have_text("Plant engineer")
    page.evaluate("""() => {
      sessionStorage.setItem('wb2:identity', JSON.stringify({ user: 'engineer1' }));
      sessionStorage.setItem('wb2:recent', JSON.stringify({ user: 'engineer1', tasks: 'broken' }));
      sessionStorage.setItem('wb:shell', JSON.stringify({ user: 'engineer1', tasks: [] }));
    }""")
    page.get_by_role("button", name="Summarise a contract").click()
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    expect(page.locator(".status-pill")).to_have_text("Needs your approval", timeout=15_000)
    expect(page.locator("#account-name")).to_have_text("Plant engineer")
    expect(page.locator("#toasts")).to_have_text("")
    page.goto(page.base + "/library")
    expect(page.locator(".wait-card")).to_have_count(0)
    expect(page.locator("#lib-tabs .seg")).to_be_visible()
