"""Focus ring and skip link (NFR6.7, NFR6.10, A6, A9)."""

import pytest

from tests.ui.conftest import UiHarness


def _tab_to(ui: UiHarness, element_id: str) -> None:
    for _ in range(60):
        ui.page.keyboard.press("Tab")
        if ui.page.evaluate("document.activeElement.id") == element_id:
            return
    raise AssertionError(f"Tab で #{element_id} に届かない")


@pytest.mark.parametrize(
    "element_id", ["focus-input", "focus-select", "focus-button", "focus-link"]
)
def test_keyboard_focus_shows_a_two_pixel_solid_outline(
    ui: UiHarness, element_id: str
) -> None:
    ui.load_component("focus.html")

    _tab_to(ui, element_id)

    style = ui.page.evaluate(
        """(id) => {
            const s = getComputedStyle(document.getElementById(id));
            return [s.outlineStyle, s.outlineWidth, s.outlineOffset];
        }""",
        element_id,
    )
    assert style == ["solid", "2px", "2px"]


def test_first_tab_reveals_the_skip_link_and_enter_moves_to_main(
    ui: UiHarness,
) -> None:
    ui.load_component("focus.html")

    ui.page.keyboard.press("Tab")

    skip = ui.page.locator("[data-testid='skip-link']")
    assert ui.page.evaluate("document.activeElement.dataset.testid") == "skip-link"
    assert skip.inner_text().strip() == "本文へ移動"
    box = skip.bounding_box()
    viewport = ui.page.viewport_size
    assert box is not None and viewport is not None
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"]
    assert box["width"] > 1 and box["height"] > 1

    ui.page.keyboard.press("Enter")

    assert ui.page.evaluate("document.activeElement.tagName") == "MAIN"
    assert ui.page.evaluate("document.activeElement.id") == "main"


def test_skip_link_is_hidden_until_focused(ui: UiHarness) -> None:
    ui.load_component("focus.html")

    box = ui.page.locator("[data-testid='skip-link']").bounding_box()

    assert box is not None
    assert box["width"] <= 1 or box["height"] <= 1 or box["y"] + box["height"] <= 0
