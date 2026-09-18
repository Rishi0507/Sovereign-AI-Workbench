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


def test_conversation_continues_on_the_same_page(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.locator("#attach-btn").click()
    page.locator(".pick", has_text="pipe_data.md").locator("input").check()
    page.locator("#task-text").fill("What is the design pressure in the data sheet?")
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    url = page.url
    first = page.locator(".answer").first
    expect(first).to_contain_text("4.5 MPa", timeout=15_000)
    left_before = page.evaluate("document.querySelector('.thread').getBoundingClientRect().left")

    box = page.locator("#followup-text")
    expect(box).to_be_enabled()
    box.fill("How many words are in it?")
    box.press("Enter")
    expect(page.locator(".turn.user")).to_have_count(2)
    expect(page.locator(".answer").nth(1)).to_contain_text("pipe_data.md has 1 page and", timeout=15_000)
    expect(page.locator(".source-card").last).to_contain_text("Counted from the document")
    assert page.url == url
    expect(box).to_be_enabled()
    expect(box).to_have_value("")
    # The sidebar lists the conversation once.
    expect(page.locator("#recent a")).to_have_count(1)

    # Switching to another chat keeps the column in exactly the same place.
    page.goto(page.base + "/")
    page.get_by_role("button", name="Analyse sensor readings").click()
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    expect(page.get_by_role("button", name="Start")).to_be_visible(timeout=15_000)
    left_other = page.evaluate("document.querySelector('.thread').getBoundingClientRect().left")
    page.locator("#recent a", has_text="design pressure").click()
    page.wait_for_url(url)
    expect(page.locator(".turn.user")).to_have_count(2)
    left_after = page.evaluate("document.querySelector('.thread').getBoundingClientRect().left")
    assert left_before == left_other == left_after, (left_before, left_other, left_after)


def test_small_talk_then_choosing_a_document(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.locator("#task-text").fill("hello")
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    expect(page.locator(".answer").first).to_contain_text("Hello!", timeout=15_000)
    expect(page.locator(".suggest-row").first).to_contain_text("Summarise the contract")
    expect(page.locator(".steps-wrap")).to_have_count(0)

    box = page.locator("#followup-text")
    box.fill("summarise any document")
    box.press("Enter")
    expect(page.locator(".answer").nth(1)).to_contain_text("Which document should I summarise?", timeout=15_000)
    picked = page.locator(".suggest-row").nth(1).get_by_role("button").first
    name = picked.inner_text().replace("Summarise ", "").strip()
    picked.click()
    expect(page.locator(".turn.user")).to_have_count(3)
    expect(page.locator(".turn.user").nth(2)).to_contain_text(name)
    expect(page.get_by_role("button", name="Start")).to_be_visible(timeout=15_000)

    # Consecutive turns sit close together: no empty band between an answer and the next message.
    gap = page.evaluate("""() => {
      const answer = document.querySelectorAll('.turn.assistant')[0].getBoundingClientRect();
      const next = document.querySelectorAll('.turn.user')[1].getBoundingClientRect();
      return next.top - answer.bottom;
    }""")
    assert 0 <= gap <= 40, gap


def test_a_document_can_be_read_in_the_library_without_downloading(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.get_by_role("button", name="Summarise a contract").click()
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".ready-card")).to_be_visible(timeout=60_000)

    page.goto(page.base + "/library")
    tile = page.locator(".tile-open").first
    expect(tile).to_contain_text("summary.docx")
    tile.click()
    expect(page.locator("#modal-title")).to_have_text("summary.docx")
    expect(page.locator(".doc-preview")).to_contain_text("contract", ignore_case=True)
    expect(page.locator("#modal").get_by_role("link", name="Download")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#modal")).to_be_hidden()


def test_the_network_chip_stays_out_of_the_way_while_nothing_leaks(page: Any) -> None:
    playwright_api.expect(page.locator("#net")).to_be_hidden()
    page.goto(page.base + "/")
    playwright_api.expect(page.locator("#account-name")).to_have_text("Plant engineer")
    page.wait_for_timeout(500)
    playwright_api.expect(page.locator("#net")).to_be_hidden()


def test_the_plant_map_and_a_drawing_open_what_is_recorded(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/plant")
    expect(page.locator(".graph-canvas")).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="P-108B", exact=True).click()
    panel = page.locator(".plant-panel")
    expect(panel.locator(".panel-name")).to_have_text("P-108B", timeout=15_000)
    expect(panel).to_contain_text("Narmada Pumps Ltd")
    expect(panel).to_contain_text("PID-CW-003")

    # The same equipment on its drawing sheet.
    page.get_by_role("tab", name="Drawings").click()
    expect(page.locator(".sheet-holder svg")).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="PID-CW-003").click()
    sheet_tag = page.locator("[data-tag='P-108B']")
    expect(sheet_tag).to_be_visible(timeout=15_000)
    sheet_tag.click()
    expect(page.locator(".plant-panel .panel-name")).to_have_text("P-108B", timeout=15_000)
    assert "pid-selected" in (sheet_tag.get_attribute("class") or "")


def test_a_reviewer_can_rewrite_a_paragraph_and_the_edit_must_be_accepted(page: Any) -> None:
    expect = playwright_api.expect
    page.goto(page.base + "/")
    page.get_by_role("button", name="Summarise a contract").click()
    page.locator("#task-text").press("Enter")
    page.wait_for_url("**/t/T*")
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".ready-card")).to_be_visible(timeout=60_000)
    page.locator('a[href$="/review"]').first.click()
    para = page.locator(".paper p.editable").first
    expect(para).to_be_visible(timeout=30_000)

    para.click()
    editor = page.locator(".paper-edit")
    expect(editor).to_be_visible(timeout=10_000)
    editor.fill("The reviewer rewrote this line and typed 99.9 mm, which no record supports.")
    page.get_by_role("button", name="Save").click()

    expect(page.locator(".edit-mark").first).to_be_visible(timeout=30_000)
    side = page.locator("#review-side")
    expect(side).to_contain_text("Edited by hand")
    expect(side).to_contain_text("Figures without a source")
    expect(page.get_by_role("button", name="Approve")).to_be_disabled()
    page.get_by_role("button", name="Accept this edit").click()
    expect(page.locator(".edit-mark.ok").first).to_be_visible(timeout=20_000)
