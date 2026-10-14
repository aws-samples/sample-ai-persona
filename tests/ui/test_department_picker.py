"""Department picker (C6, WF7, NFR6.2, NFR6.5, NFR6.11, B12, B24)."""

from typing import Any

import pytest

from src.models.authz import Department
from tests.ui.conftest import UiHarness

# 約束 C6 の choices は list[Department]（src/models/authz.py）。実物を渡す
DEPARTMENTS = [
    Department(group_id="sales", display_name="営業部"),
    Department(group_id="plan", display_name="企画部"),
    Department(group_id="hr", display_name="人事部"),
]
SAVE = "[data-testid='picker-save']"
REMOVING = "[data-department-picker] [data-department-removing]"


def _load(ui: UiHarness, **overrides: Any) -> None:
    context: dict[str, Any] = {
        "choices": DEPARTMENTS,
        "selected": frozenset({"sales"}),
        "mixed": None,
        "mode": "share",
    }
    context.update(overrides)
    ui.load_component("department_picker.html", **context)
    ui.settle()


def _load_mixed(ui: UiHarness) -> None:
    # 5 件を対象に開いた: 営業部は全件、企画部は 2 件、人事部はどれにもない
    _load(ui, selected=frozenset(), mixed={"sales": (5, 5), "plan": (2, 5)})


def _option(dept_id: str) -> str:
    return f"[data-department-option][value='{dept_id}']"


def _states(ui: UiHarness) -> dict[str, str]:
    ui.settle()
    return ui.page.evaluate(
        """() => Object.fromEntries(Array.from(
            document.querySelectorAll('[data-department-option]')).map((o) => [
                o.value, o.indeterminate ? 'mixed' : (o.checked ? 'checked' : 'unchecked')]))"""
    )


def _sent(ui: UiHarness) -> list[list[str]]:
    return ui.page.evaluate(
        "() => Array.from(new FormData(document.getElementById('picker-form')).entries())"
    )


def test_fieldset_legend_and_labels(ui: UiHarness) -> None:
    _load(ui)

    assert ui.page.locator(
        "fieldset[data-department-picker] > legend"
    ).inner_text().strip() == ("共有するグループ（複数選択可）")
    labels = ui.page.locator("[data-department-picker] label").all_inner_texts()
    assert [label.strip() for label in labels] == ["営業部", "企画部", "人事部"]


def test_department_model_gives_value_name_and_selection(ui: UiHarness) -> None:
    """The macro reads Department.group_id / display_name (contract C6)."""
    _load(ui)

    options = ui.page.locator("[data-department-option]").evaluate_all(
        """(inputs) => inputs.map((o) => [
            o.value, o.closest('label').innerText.trim(), o.checked])"""
    )
    assert options == [
        ["sales", "営業部", True],
        ["plan", "企画部", False],
        ["hr", "人事部", False],
    ]


def test_scope_mode_uses_its_own_legend(ui: UiHarness) -> None:
    _load(ui, mode="scope")

    assert ui.page.locator("[data-department-picker] legend").inner_text().strip() == (
        "利用できるグループ（複数選択可）"
    )


def test_single_target_starts_from_the_current_setting(ui: UiHarness) -> None:
    _load(ui)

    assert _states(ui) == {"sales": "checked", "plan": "unchecked", "hr": "unchecked"}
    assert ui.page.is_disabled(SAVE)
    ui.page.check(_option("plan"))
    assert ui.page.is_enabled(SAVE)
    ui.page.uncheck(_option("plan"))
    assert ui.page.is_disabled(SAVE)


def test_mixed_starts_with_checked_partial_and_unchecked(ui: UiHarness) -> None:
    _load_mixed(ui)

    assert _states(ui) == {"sales": "checked", "plan": "mixed", "hr": "unchecked"}
    note_id = ui.page.get_attribute(_option("plan"), "aria-describedby")
    assert note_id
    assert ui.page.locator(f"#{note_id}").inner_text().strip() == "5 件中 2 件で共有中"
    assert ui.page.is_disabled(SAVE)


def test_mixed_option_cycles_checked_unchecked_mixed(ui: UiHarness) -> None:
    _load_mixed(ui)

    seen = []
    for _ in range(3):
        ui.page.click(_option("plan"))
        seen.append(_states(ui)["plan"])

    assert seen == ["checked", "unchecked", "mixed"]
    assert ui.page.is_disabled(SAVE)


#: Departments that were checked or mixed when the mixed picker opened (BR1.12).
INITIAL = [("departments_initial", "plan"), ("departments_initial", "sales")]


def test_sent_values_follow_the_states(ui: UiHarness) -> None:
    _load_mixed(ui)
    assert sorted(map(tuple, _sent(ui))) == [
        ("departments", "sales"),
        *INITIAL,
        ("departments_unchanged", "plan"),
    ]

    ui.page.click(_option("plan"))
    ui.page.check(_option("hr"))
    ui.page.uncheck(_option("sales"))

    assert sorted(map(tuple, _sent(ui))) == [
        ("departments", "hr"),
        ("departments", "plan"),
        *INITIAL,
    ]


def test_initial_values_are_the_departments_shared_when_opened(ui: UiHarness) -> None:
    """{name}_initial は開いたときに選択か中間だった部署だけ。押しても変わらない（U4、C6）。"""
    _load(ui)
    initial = [("departments_initial", "sales")]
    assert [tuple(v) for v in _sent(ui) if v[0] == "departments_initial"] == initial

    ui.page.uncheck(_option("sales"))
    ui.page.check(_option("hr"))

    assert [tuple(v) for v in _sent(ui) if v[0] == "departments_initial"] == initial


def test_vanished_department_comes_last_and_can_be_removed(ui: UiHarness) -> None:
    """消えた部署（admin のとき）は「（存在しないグループ）」として最後に出る（U4）。"""
    _load(ui, selected=frozenset({"sales", "old-dept"}), vanished=["old-dept"])

    labels = ui.page.locator("[data-department-picker] label").all_inner_texts()
    assert [label.strip() for label in labels][-1] == "（存在しないグループ）"
    assert _states(ui)["old-dept"] == "checked"

    ui.page.uncheck(_option("old-dept"))

    sent = sorted(map(tuple, _sent(ui)))
    assert ("departments", "old-dept") not in sent
    assert ("departments_initial", "old-dept") in sent
    assert "（存在しないグループ）" in ui.page.locator(REMOVING).inner_text()


def test_another_htmx_swap_keeps_the_picker_state(ui: UiHarness) -> None:
    """A swap elsewhere on the page must not re-initialise the picker."""
    ui.respond("/elsewhere", body="<p>更新した</p>")
    _load_mixed(ui)
    ui.page.click(_option("plan"))
    assert _states(ui)["plan"] == "checked"
    assert not ui.page.is_disabled(SAVE)

    ui.page.click("#elsewhere-swap")
    ui.page.wait_for_selector("#elsewhere:has-text('更新した')")
    ui.settle()

    assert _states(ui)["plan"] == "checked"
    assert sorted(map(tuple, _sent(ui))) == [
        ("departments", "plan"),
        ("departments", "sales"),
        *INITIAL,
    ]
    assert not ui.page.is_disabled(SAVE)


def test_removed_departments_are_announced_with_counts(ui: UiHarness) -> None:
    _load_mixed(ui)
    removing = ui.page.locator(REMOVING)
    assert removing.get_attribute("aria-live") == "polite"
    assert removing.inner_text().strip() == ""

    ui.page.uncheck(_option("sales"))
    ui.page.click(_option("plan"))
    ui.page.click(_option("plan"))
    ui.settle()

    text = removing.inner_text()
    assert "営業部（5 件）" in text
    assert "企画部（2 件）" in text
    assert "人事部" not in text


def test_removing_every_choice_asks_for_confirmation(ui: UiHarness) -> None:
    ui.respond("/share-save", status=200)
    _load(ui)
    ui.page.uncheck(_option("sales"))

    ui.page.click(SAVE)
    ui.settle()

    confirm = ui.page.locator("[data-department-picker] [data-department-confirm]")
    assert confirm.is_visible()
    assert ui.active(
        "document.activeElement.hasAttribute('data-department-confirm-back')"
    )
    assert ui.requests == []

    ui.page.keyboard.press("Enter")
    ui.settle()
    assert confirm.is_hidden()
    assert ui.requests == []

    ui.page.click(SAVE)
    ui.page.click("[data-department-picker] [data-department-confirm-ok]")
    ui.page.wait_for_function("() => true")
    ui.settle()
    assert ui.requests == ["/share-save"]


def test_partial_change_saves_without_confirmation(ui: UiHarness) -> None:
    ui.respond("/share-save", status=200)
    _load(ui)
    ui.page.check(_option("plan"))

    ui.page.click(SAVE)
    ui.settle()
    ui.page.wait_for_timeout(50)

    assert ui.requests == ["/share-save"]


def test_no_choices_explains_why_and_disables_saving(ui: UiHarness) -> None:
    _load(ui, choices=[], selected=frozenset())

    text = ui.page.locator(
        "[data-department-picker] [data-department-empty]"
    ).inner_text()
    assert "選べるグループがありません" in text
    assert ui.page.is_disabled(SAVE)


@pytest.mark.parametrize(("count", "columns"), [(8, 1), (9, 2)])
def test_more_than_eight_departments_use_two_columns(
    ui: UiHarness, count: int, columns: int
) -> None:
    ui.page.set_viewport_size({"width": 1280, "height": 900})
    choices = [
        Department(group_id=f"d{i}", display_name=f"部署{i}") for i in range(count)
    ]
    _load(ui, choices=choices, selected=frozenset())

    template = ui.page.evaluate(
        "getComputedStyle(document.querySelector('[data-department-options]')).gridTemplateColumns"
    )
    assert len(template.split()) == columns


@pytest.mark.parametrize("mixed", [False, True])
def test_picker_has_no_axe_violations(ui: UiHarness, mixed: bool) -> None:
    if mixed:
        _load_mixed(ui)
    else:
        _load(ui)
    ui.check_a11y()
