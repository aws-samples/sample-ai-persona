"""Bulk selection (C5, WF6, NFR6.2, NFR6.5, NFR6.11, B10, B11)."""

from typing import Any

import pytest

from tests.ui.conftest import UiHarness, render_macro

ROWS = [("p1", "佐藤 太郎"), ("p2", "鈴木 花子"), ("p3", "高橋 次郎")]
ALL = "[data-bulk-all]"
ACTION = "[data-bulk-action]"
COUNT = "[data-bulk-count]"


def _load(ui: UiHarness, **overrides: Any) -> None:
    context: dict[str, Any] = {"rows": ROWS, "selectable": True}
    context.update(overrides)
    ui.load_component("bulk_select.html", **context)


def _state(ui: UiHarness) -> dict[str, Any]:
    ui.settle()
    return ui.page.evaluate(
        """() => {
            const all = document.querySelector('[data-bulk-all]');
            return {
                checked: all.checked,
                indeterminate: all.indeterminate,
                action: !document.querySelector('[data-bulk-action]').disabled,
                count: document.querySelector('[data-bulk-count]').textContent.trim(),
                rows: Array.from(document.querySelectorAll('[data-bulk-row]')).map((r) => r.checked),
            };
        }"""
    )


def _row(value: str) -> str:
    return f"[data-bulk-row][value='{value}']"


def test_row_checkbox_is_named_after_the_row(ui: UiHarness) -> None:
    _load(ui)

    assert ui.page.get_attribute(_row("p1"), "aria-label") == "佐藤 太郎 を選択"
    assert ui.page.get_attribute(_row("p1"), "name") == "ids"


def test_nothing_selected_disables_the_action(ui: UiHarness) -> None:
    _load(ui)

    assert _state(ui) == {
        "checked": False,
        "indeterminate": False,
        "action": False,
        "count": "0 件を選択中",
        "rows": [False, False, False],
    }
    assert ui.page.get_attribute(COUNT, "aria-live") == "polite"
    assert ui.page.inner_text(ACTION).strip() == "共有先を設定"


def test_some_selected_makes_select_all_mixed(ui: UiHarness) -> None:
    _load(ui)

    ui.page.focus(_row("p2"))
    ui.page.keyboard.press(" ")

    state = _state(ui)
    assert (state["checked"], state["indeterminate"], state["action"]) == (
        False,
        True,
        True,
    )
    assert state["count"] == "1 件を選択中"


def test_every_row_selected_checks_select_all(ui: UiHarness) -> None:
    _load(ui)

    for value, _ in ROWS:
        ui.page.check(_row(value))

    state = _state(ui)
    assert (state["checked"], state["indeterminate"]) == (True, False)
    assert state["count"] == "3 件を選択中"


def test_select_all_selects_from_mixed_then_clears(ui: UiHarness) -> None:
    _load(ui)
    ui.page.check(_row("p1"))

    ui.page.click(ALL)
    assert _state(ui)["rows"] == [True, True, True]

    ui.page.click(ALL)
    state = _state(ui)
    assert state["rows"] == [False, False, False]
    assert (state["checked"], state["indeterminate"], state["action"]) == (
        False,
        False,
        False,
    )


def test_rows_added_by_more_start_unselected(ui: UiHarness) -> None:
    more = "".join(
        f"<li>{render_macro('bulk_select.html', 'bulk_select_row', v, n)}</li>"
        for v, n in [("p4", "伊藤 四郎"), ("p5", "渡辺 五郎")]
    )
    ui.respond("/more", body=more)
    _load(ui)
    ui.page.click(ALL)

    ui.page.click("#more-button")
    ui.page.wait_for_selector(_row("p5"))

    state = _state(ui)
    assert state["rows"] == [True, True, True, False, False]
    assert (state["checked"], state["indeterminate"]) == (False, True)
    assert state["count"] == "3 件を選択中"


def test_not_selectable_renders_nothing(ui: UiHarness) -> None:
    _load(ui, selectable=False)

    assert ui.page.locator("[data-bulk-select]").count() == 0
    assert ui.page.locator("[data-bulk-row]").count() == 0


@pytest.mark.parametrize("selected", [0, 2])
def test_bulk_select_has_no_axe_violations(ui: UiHarness, selected: int) -> None:
    _load(ui)
    for value, _ in ROWS[:selected]:
        ui.page.check(_row(value))
    ui.check_a11y()
