"""List tabs and list reload (C4, WF5, NFR4.10, NFR6.2-NFR6.5, NFR6.11, NFR6.12, B8-B10)."""

import json
from typing import Any

import pytest

from tests.ui.conftest import UiHarness

TABS = "[data-list-tabs] [role='tab']"
COUNTS = {"mine": 12, "shared": 3, "all": 40}


def _load(ui: UiHarness, **overrides: Any) -> None:
    context: dict[str, Any] = {"active": "mine", "counts": COUNTS, "show_all": True}
    context.update(overrides)
    ui.load_component("list_tabs.html", **context)


def _tab(key: str) -> str:
    return f"[data-list-tabs] [role='tab'][data-tab='{key}']"


def _selected(ui: UiHarness) -> list[str]:
    ui.settle()
    return ui.page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel))
            .filter((t) => t.getAttribute('aria-selected') === 'true')
            .map((t) => t.dataset.tab)""",
        TABS,
    )


def _rows(key: str) -> str:
    return f'<ul class="divide-y"><li class="py-2 text-gray-700">{key} の行</li></ul>'


def test_roles_selection_and_non_color_cues(ui: UiHarness) -> None:
    _load(ui)

    assert ui.page.locator("[data-list-tabs] [role='tablist']").count() == 1
    assert ui.page.locator(TABS).count() == 3
    panel = ui.page.locator("#list-panel")
    assert panel.get_attribute("role") == "tabpanel"
    assert panel.get_attribute("aria-labelledby") == ui.page.get_attribute(
        _tab("mine"), "id"
    )
    assert _selected(ui) == ["mine"]
    classes = (ui.page.get_attribute(_tab("mine"), "class") or "").split()
    assert {"border-b-2", "font-semibold"} <= set(classes)
    other = (ui.page.get_attribute(_tab("shared"), "class") or "").split()
    assert "font-semibold" not in other
    assert ui.page.get_attribute(_tab("shared"), "href") == "/items?tab=shared"


def test_tab_names_include_the_count(ui: UiHarness) -> None:
    _load(ui, counts={"mine": 12, "shared": None, "all": 40})

    names = [" ".join(t.split()) for t in ui.page.locator(TABS).all_inner_texts()]

    assert names[0] == "自分のもの 12 件"
    assert names[1] == "共有されたもの -"


def test_show_all_false_hides_the_all_tab(ui: UiHarness) -> None:
    _load(ui, show_all=False)

    assert ui.page.locator(_tab("all")).count() == 0
    assert ui.page.locator(TABS).count() == 2


def test_arrow_keys_move_focus_and_wrap_without_requests(ui: UiHarness) -> None:
    _load(ui)
    ui.page.focus(_tab("mine"))

    ui.page.keyboard.press("ArrowRight")
    assert ui.active("document.activeElement.dataset.tab") == "shared"
    ui.page.keyboard.press("ArrowRight")
    ui.page.keyboard.press("ArrowRight")
    assert ui.active("document.activeElement.dataset.tab") == "mine"
    ui.page.keyboard.press("ArrowLeft")
    assert ui.active("document.activeElement.dataset.tab") == "all"

    assert ui.requests == []
    assert _selected(ui) == ["mine"]
    assert ui.page.get_attribute(_tab("all"), "tabindex") == "0"
    assert ui.page.get_attribute(_tab("mine"), "tabindex") == "-1"


@pytest.mark.parametrize("key", ["Enter", " "])
def test_enter_or_space_switches_and_keeps_focus(ui: UiHarness, key: str) -> None:
    ui.respond("/items?tab=shared", body=_rows("shared"))
    _load(ui)
    ui.page.focus(_tab("mine"))
    ui.page.keyboard.press("ArrowRight")

    ui.page.keyboard.press(key)
    ui.page.wait_for_selector("#list-panel:has-text('shared の行')")

    assert _selected(ui) == ["shared"]
    assert ui.page.url.endswith("/items?tab=shared")
    assert ui.active("document.activeElement.dataset.tab") == "shared"
    panel = ui.page.locator("#list-panel")
    assert panel.get_attribute("aria-labelledby") == ui.page.get_attribute(
        _tab("shared"), "id"
    )
    assert panel.get_attribute("data-reload-url") == "/items?tab=shared"


def test_panel_is_busy_while_switching(ui: UiHarness) -> None:
    held = ui.hold("/items?tab=shared")
    _load(ui)

    ui.page.click(_tab("shared"))
    held.wait_for_request()
    assert ui.page.get_attribute("#list-panel", "aria-busy") == "true"

    held.release(body=_rows("shared"))
    ui.page.wait_for_selector("#list-panel:has-text('shared の行')")
    ui.settle()
    assert ui.page.get_attribute("#list-panel", "aria-busy") is None


def _current_tab_in_url(ui: UiHarness) -> str | None:
    return ui.page.evaluate("new URL(location.href).searchParams.get('tab')")


def test_late_reply_of_an_earlier_tab_is_discarded(ui: UiHarness) -> None:
    """Shared then All: Shared's late reply must not overwrite All (list, tab, URL)."""
    shared = ui.hold("/items?tab=shared")
    ui.respond("/items?tab=all", body=_rows("all"))
    _load(ui)

    ui.page.click(_tab("shared"))
    shared.wait_for_request()
    ui.page.click(_tab("all"))
    ui.page.wait_for_selector("#list-panel:has-text('all の行')")
    shared.release(body=_rows("shared"))
    ui.page.wait_for_timeout(300)
    ui.settle()

    assert "shared の行" not in ui.page.inner_text("#list-panel")
    assert "all の行" in ui.page.inner_text("#list-panel")
    assert _selected(ui) == ["all"]
    assert _current_tab_in_url(ui) == "all"
    assert ui.page.get_attribute("#list-panel", "data-reload-url") == "/items?tab=all"


def test_panel_stays_busy_until_the_latest_tab_arrives(ui: UiHarness) -> None:
    shared = ui.hold("/items?tab=shared")
    all_tab = ui.hold("/items?tab=all")
    _load(ui)

    ui.page.click(_tab("shared"))
    shared.wait_for_request()
    ui.page.click(_tab("all"))
    all_tab.wait_for_request()
    shared.release(body=_rows("shared"))
    ui.page.wait_for_timeout(300)
    ui.settle()
    assert ui.page.get_attribute("#list-panel", "aria-busy") == "true"

    all_tab.release(body=_rows("all"))
    ui.page.wait_for_selector("#list-panel:has-text('all の行')")
    ui.settle()
    assert ui.page.get_attribute("#list-panel", "aria-busy") is None


def test_late_list_reload_does_not_overwrite_a_newer_tab(ui: UiHarness) -> None:
    reload_reply = ui.hold("/items?tab=mine")
    ui.respond("/items?tab=all", body=_rows("all"))
    _load(ui)
    ui.respond(
        "/reload-probe",
        headers={"HX-Trigger": json.dumps({"listReload": {"target": "#list-panel"}})},
    )

    ui.page.click("#reload-probe")
    reload_reply.wait_for_request()
    ui.page.click(_tab("all"))
    ui.page.wait_for_selector("#list-panel:has-text('all の行')")
    reload_reply.release(body=_rows("読み直し"))
    ui.page.wait_for_timeout(300)
    ui.settle()

    assert "読み直し の行" not in ui.page.inner_text("#list-panel")
    assert _selected(ui) == ["all"]


def test_switch_announces_the_count(ui: UiHarness) -> None:
    ui.respond("/items?tab=all", body=_rows("all"))
    _load(ui)
    status = ui.page.locator("[data-list-tabs] [data-list-tabs-status]")
    assert status.get_attribute("aria-live") == "polite"
    assert status.inner_text() == ""

    ui.page.click(_tab("all"))
    ui.page.wait_for_selector("#list-panel:has-text('all の行')")
    ui.settle()

    assert " ".join(status.inner_text().split()) == "すべて 40 件"


def test_back_button_returns_to_the_previous_tab(ui: UiHarness) -> None:
    ui.respond("/items?tab=shared", body=_rows("shared"))
    _load(ui)
    ui.page.click(_tab("shared"))
    ui.page.wait_for_selector("#list-panel:has-text('shared の行')")

    ui.page.go_back()
    ui.page.wait_for_selector("#list-panel:has-text('最初の行')")

    assert _selected(ui) == ["mine"]


def _history_cache(ui: UiHarness) -> str | None:
    return ui.page.evaluate("localStorage.getItem('htmx-history-cache')")


def test_switching_tabs_keeps_no_page_snapshot_in_local_storage(
    ui: UiHarness,
) -> None:
    """Lists must not survive a sign-out in htmx's history cache (U4 review P1)."""
    ui.respond("/items?tab=shared", body=_rows("shared"))
    _load(ui)

    ui.page.click(_tab("shared"))
    ui.page.wait_for_selector("#list-panel:has-text('shared の行')")
    ui.settle()

    assert _history_cache(ui) is None


def test_a_snapshot_left_by_an_earlier_session_is_removed_on_load(
    ui: UiHarness,
) -> None:
    _load(ui)
    ui.page.evaluate(
        "localStorage.setItem('htmx-history-cache', JSON.stringify([{url: '/x', content: '前の利用者'}]))"
    )

    _load(ui)  # the next page load (e.g. after signing in again)

    assert _history_cache(ui) is None


def test_list_reload_refetches_the_current_tab(ui: UiHarness) -> None:
    ui.respond("/items?tab=shared", body=_rows("shared"))
    _load(ui)
    ui.page.click(_tab("shared"))
    ui.page.wait_for_selector("#list-panel:has-text('shared の行')")
    ui.respond("/items?tab=shared", body=_rows("更新後"))
    ui.respond(
        "/reload-probe",
        headers={"HX-Trigger": json.dumps({"listReload": {"target": "#list-panel"}})},
    )

    ui.page.click("#reload-probe")
    ui.page.wait_for_selector("#list-panel:has-text('更新後 の行')")

    assert ui.requests.count("/items?tab=shared") == 2


def test_list_reload_never_uses_a_url_from_the_event(ui: UiHarness) -> None:
    ui.respond("/items?tab=mine", body=_rows("読み直し"))
    _load(ui)
    ui.respond(
        "/reload-probe",
        headers={
            "HX-Trigger": json.dumps(
                {"listReload": {"target": "#list-panel", "url": "/evil"}}
            )
        },
    )

    ui.page.click("#reload-probe")
    ui.page.wait_for_selector("#list-panel:has-text('読み直し の行')")

    assert "/evil" not in ui.requests


@pytest.mark.parametrize(
    "target", ["list-panel", "#missing", ".divide-y", "#list-panel li", "#reload-probe"]
)
def test_list_reload_ignores_invalid_targets(ui: UiHarness, target: str) -> None:
    _load(ui)
    ui.respond(
        "/reload-probe",
        headers={"HX-Trigger": json.dumps({"listReload": {"target": target}})},
    )

    ui.page.click("#reload-probe")
    ui.page.wait_for_timeout(100)
    ui.settle()

    assert ui.requests == ["/reload-probe"]


def test_dialog_closes_before_the_list_is_reloaded(ui: UiHarness) -> None:
    held = ui.hold("/items?tab=mine")
    _load(ui)
    ui.respond(
        "/share",
        headers={
            "HX-Trigger": json.dumps(
                {
                    "listReload": {"target": "#list-panel"},
                    "dialogClose": {"id": "share-dialog"},
                }
            )
        },
    )
    ui.page.click("[data-dialog-open='share-dialog']")
    ui.settle()

    ui.page.click("[data-testid='share-dialog-submit']")
    held.wait_for_request()

    state = ui.page.evaluate(
        """() => [document.getElementById('share-dialog').hidden,
                  document.querySelectorAll('[inert]').length]"""
    )
    assert state == [True, 0]
    held.release(body=_rows("共有後"))
    ui.page.wait_for_selector("#list-panel:has-text('共有後 の行')")


def test_tabs_have_no_axe_violations(ui: UiHarness) -> None:
    _load(ui)
    ui.check_a11y()
