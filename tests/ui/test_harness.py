"""Checks of the component-test harness itself (WF10, NFR6.2, NFR7.2).

If the built CSS is not loaded, every axe-core and visibility check that
follows is meaningless, so these run first and fail loudly.
"""

import pytest

from tests.ui.conftest import UiHarness


def test_built_css_is_loaded(ui: UiHarness) -> None:
    """tailwind.css is applied: it is in styleSheets and `hidden` hides elements."""
    ui.load_component("harness.html")

    sheets = ui.page.evaluate("Array.from(document.styleSheets).map(s => s.href || '')")
    assert any(href.endswith("/static/css/tailwind.css") for href in sheets), sheets
    assert ui.page.locator("#hidden-probe").is_hidden()
    assert (
        ui.page.evaluate(
            "getComputedStyle(document.getElementById('hidden-probe')).display"
        )
        == "none"
    )


def test_external_requests_are_blocked(ui: UiHarness) -> None:
    """Requests leaving testserver are aborted and never reach the network."""
    ui.load_component("harness.html")

    outcome = ui.page.evaluate(
        """() => fetch('https://cdn.example.com/lib.js')
                .then(() => 'reached', () => 'blocked')"""
    )

    assert outcome == "blocked"
    assert "https://cdn.example.com/lib.js" in ui.blocked_requests


def test_unregistered_testserver_request_fails_the_test(ui: UiHarness) -> None:
    """A testserver URL missing from the response table is reported as a failure."""
    ui.load_component("harness.html")

    ui.page.evaluate("() => fetch('/not-registered?x=1').then(() => null, () => null)")

    assert ui.unexpected_requests == ["http://testserver/not-registered?x=1"]
    with pytest.raises(pytest.fail.Exception, match="not-registered"):
        ui.verify_requests()
    ui.unexpected_requests.clear()


def test_registered_response_is_served(ui: UiHarness) -> None:
    """A response registered for a URL is returned with its status and headers."""
    ui.respond("/fragment?tab=mine", body="<p>ok</p>", headers={"X-Probe": "1"})
    ui.load_component("harness.html")

    result = ui.page.evaluate(
        """() => fetch('/fragment?tab=mine')
                .then(r => r.text().then(t => [r.status, r.headers.get('X-Probe'), t]))"""
    )

    assert result == [200, "1", "<p>ok</p>"]
