"""Replaced modals whose behaviour changes in U3 (WF3, NFR4.9, NFR6.12).

KB binding and insight categories now close only when their request succeeds,
stay open on failure, and no longer close on a backdrop click.
"""

import json

import pytest

from tests.ui.conftest import UiHarness, render_page

FAILURE_TOAST = {
    "HX-Trigger": json.dumps(
        {"showToast": {"type": "error", "message": "失敗しました"}}
    )
}

KB_BINDING = render_page(
    "persona/partials/kb_binding.html",
    persona_id="p1",
    knowledge_bases=[
        {
            "id": "kb1",
            "name": "社内ナレッジ",
            "description": "",
            "knowledge_base_id": "ABC",
        }
    ],
    binding=None,
)
INSIGHTS = render_page(
    "discussion/partials/insights.html",
    insights=[],
    discussion_id="d1",
    default_categories=[{"name": "顧客ニーズ", "description": "ニーズ"}],
)

CASES = {
    "kb": {
        "container": "kb-binding-content",
        "fragment": KB_BINDING,
        "dialog": "kb-binding-kb1",
        "url": "/persona/p1/kb-binding",
        "submit": "#kb-binding-kb1 [hx-post]",
    },
    "insights": {
        "container": "insights-container",
        "fragment": INSIGHTS,
        "dialog": "regenerate-insights-dialog",
        "url": "/discussion/d1/regenerate-insights",
        "submit": "#regenerate-insights-dialog [type='submit']",
    },
}


def _load(ui: UiHarness, case: dict[str, str]) -> None:
    ui.load_component(
        "fragment.html", container_id=case["container"], fragment=case["fragment"]
    )
    ui.page.click(f"[data-dialog-open='{case['dialog']}']")
    ui.settle()


def _is_open(ui: UiHarness, dialog_id: str) -> bool:
    ui.settle()
    return ui.page.evaluate(
        "(id) => { const d = document.getElementById(id); return !!d && !d.hidden; }",
        dialog_id,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_backdrop_click_does_not_close(ui: UiHarness, name: str) -> None:
    case = CASES[name]
    _load(ui, case)

    ui.page.mouse.click(3, 3)

    assert _is_open(ui, case["dialog"])


@pytest.mark.parametrize("name", list(CASES))
def test_failed_request_keeps_the_dialog_open(ui: UiHarness, name: str) -> None:
    case = CASES[name]
    ui.respond(case["url"], status=503, headers=FAILURE_TOAST)
    _load(ui, case)

    ui.page.click(case["submit"])
    ui.page.wait_for_selector("#toast-alert [data-toast]")

    assert _is_open(ui, case["dialog"])


@pytest.mark.parametrize("name", list(CASES))
def test_successful_request_closes_and_releases_the_page(
    ui: UiHarness, name: str
) -> None:
    case = CASES[name]
    ui.respond(case["url"], body="<p>更新しました</p>")
    _load(ui, case)

    ui.page.click(case["submit"])
    ui.page.wait_for_selector(f"#{case['container']}:has-text('更新しました')")

    assert not _is_open(ui, case["dialog"])
    assert ui.page.evaluate("document.querySelectorAll('[inert]').length") == 0
