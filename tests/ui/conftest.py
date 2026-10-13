"""Harness for component behaviour tests (marker ``ui``, U3 WF10).

Pages are loaded from ``http://testserver/__ui__`` in headless Chromium. The app
is not started: one ``page.route("**/*")`` handler serves everything.

1. ``http://testserver/__ui__`` -> the HTML under test
2. ``http://testserver/static/...`` -> files under ``web/static/``
3. URLs registered per test with :meth:`UiHarness.respond` / :meth:`UiHarness.hold`
4. any other ``testserver`` URL -> aborted and reported as a test failure
5. anything outside ``testserver`` -> aborted (no CDN, no AWS)

Component pages are Jinja templates under ``tests/ui/pages/`` rendered with the
app's own Jinja environment. Full screens (``base.html``, the list screens, M11)
are fetched through FastAPI's ``TestClient`` under ``AUTH_MODE=local``.
"""

from __future__ import annotations

import mimetypes
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlsplit

import pytest

_THIS_DIR = Path(__file__).parent
_PAGES_DIR = _THIS_DIR / "pages"
_STATIC_DIR = _THIS_DIR.parent.parent / "web" / "static"

HOST = "testserver"
BASE_URL = f"http://{HOST}/__ui__"

#: axe-core rule tags applied to every page (NFR Design "axe-core の検査").
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]

_CHROMIUM_MISSING = (
    "Chromium が入っていません。`uv run playwright install chromium` で入れると"
    "部品のテスト（ui）を流せます"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if str(item.fspath).startswith(str(_THIS_DIR)):
            item.add_marker(pytest.mark.ui)


@dataclass
class Reply:
    """A canned response for one URL."""

    status: int = 200
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class HeldReply:
    """A response the test releases later (to observe loading states)."""

    page: Any
    route: Any = None

    def wait_for_request(self, timeout_ms: int = 5000) -> None:
        """Pump the event loop until the held request has arrived."""
        waited = 0
        while self.route is None and waited < timeout_ms:
            self.page.wait_for_timeout(20)
            waited += 20
        if self.route is None:
            pytest.fail("held request never arrived")

    def release(
        self, status: int = 200, body: str = "", headers: dict[str, str] | None = None
    ) -> None:
        """Answer the held request."""
        self.wait_for_request()
        _fulfill(self.route, Reply(status, body, dict(headers or {})))


def _fulfill(route: Any, reply: Reply) -> None:
    headers = {"Content-Type": "text/html; charset=utf-8", **reply.headers}
    route.fulfill(status=reply.status, headers=headers, body=reply.body)


class UiHarness:
    """One browser page wired to the single route handler."""

    def __init__(self, page: Any) -> None:
        self.page = page
        # 部品の画面はローカルで描くため、待ちは短くてよい（失敗を早く返す）
        page.set_default_timeout(5000)
        self.html = "<!DOCTYPE html><title>empty</title>"
        self.replies: dict[str, Reply | HeldReply] = {}
        self.requests: list[str] = []
        self.unexpected_requests: list[str] = []
        self.blocked_requests: list[str] = []
        page.route("**/*", self._handle)

    # --- route table -----------------------------------------------------
    def respond(
        self,
        url: str,
        *,
        status: int = 200,
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Register a response for ``url`` (path with optional query)."""
        self.replies[url] = Reply(status, body, dict(headers or {}))

    def hold(self, url: str) -> HeldReply:
        """Register a response that waits until the test releases it."""
        held = HeldReply(self.page)
        self.replies[url] = held
        return held

    def _handle(self, route: Any, request: Any) -> None:
        url = request.url
        parts = urlsplit(url)
        if parts.hostname != HOST:
            self.blocked_requests.append(url)
            route.abort()
            return
        if parts.path == "/__ui__":
            route.fulfill(
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=self.html,
            )
            return
        if parts.path.startswith("/static/"):
            self._serve_static(route, parts.path)
            return
        key = parts.path + (f"?{parts.query}" if parts.query else "")
        reply = self.replies.get(key) or self.replies.get(parts.path)
        if reply is None:
            self.unexpected_requests.append(url)
            route.abort()
            return
        self.requests.append(key)
        if isinstance(reply, HeldReply):
            reply.route = route
            return
        _fulfill(route, reply)

    def _serve_static(self, route: Any, path: str) -> None:
        file = (_STATIC_DIR / path.removeprefix("/static/")).resolve()
        if not file.is_relative_to(_STATIC_DIR.resolve()) or not file.is_file():
            self.unexpected_requests.append(path)
            route.abort()
            return
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        route.fulfill(
            status=200, headers={"Content-Type": content_type}, body=file.read_bytes()
        )

    def verify_requests(self) -> None:
        """Fail when a testserver URL outside the response table was requested."""
        if self.unexpected_requests:
            pytest.fail(
                "応答の表にない testserver への要求がありました: "
                + ", ".join(self.unexpected_requests)
            )

    # --- loading pages -----------------------------------------------------
    def load(self, html: str) -> None:
        """Serve ``html`` at the base URL and open it."""
        self.html = html
        self.page.goto(BASE_URL)

    def load_component(self, template: str, **context: Any) -> None:
        """Render ``tests/ui/pages/<template>`` with the app's Jinja env and open it."""
        self.load(render_page(template, **context))

    def load_screen(self, client: Any, path: str) -> None:
        """Fetch a full screen through ``TestClient`` and open its HTML."""
        response = client.get(path, headers={"accept": "text/html"})
        self.load(response.text)

    def settle(self, frames: int = 6) -> None:
        """Let Alpine effects, transitions and rAF callbacks run."""
        self.page.evaluate(
            """(n) => new Promise((resolve) => {
                const step = (left) => left <= 0
                    ? resolve()
                    : requestAnimationFrame(() => step(left - 1));
                step(n);
            })""",
            frames,
        )

    def active(self, script: str = "document.activeElement.id") -> Any:
        """Evaluate ``script`` about the focused element after the page settles."""
        self.settle()
        return self.page.evaluate(script)

    # --- accessibility ---------------------------------------------------------
    def check_a11y(self) -> None:
        """Run axe-core: violations fail the test, incomplete items are warned."""
        from axe_playwright_python.sync_playwright import Axe

        self.settle()
        # フェードインの途中（半透明）の文字でコントラストを測らないよう、有限の
        # アニメーションが終わるのを待つ（回り続けるスピナーは待たない）
        self.page.evaluate(
            """() => Promise.all(document.getAnimations()
                .filter((a) => a.effect && a.effect.getComputedTiming().iterations !== Infinity)
                .map((a) => a.finished.catch(() => null)))"""
        )
        results = Axe().run(
            self.page,
            options={
                "runOnly": {"type": "tag", "values": AXE_TAGS},
                "resultTypes": ["violations", "incomplete"],
            },
        )
        incomplete = results.response.get("incomplete", [])
        if incomplete:
            summary = "; ".join(
                f"{item['id']}: "
                + ", ".join(",".join(node["target"]) for node in item["nodes"])
                for item in incomplete
            )
            warnings.warn(
                f"axe-core incomplete ({len(incomplete)}): {summary}", stacklevel=2
            )
        if results.violations_count:
            pytest.fail(results.generate_report())


def _jinja_env() -> Any:
    from jinja2 import ChoiceLoader, FileSystemLoader

    from web.main import templates

    env = templates.env
    return env.overlay(loader=ChoiceLoader([FileSystemLoader(_PAGES_DIR), env.loader]))


def render_page(template: str, **context: Any) -> str:
    """Render a page template with the app's Jinja environment (globals included)."""
    context.setdefault("title", "部品のテスト")
    return str(_jinja_env().get_template(template).render(**context))


def render_macro(template: str, macro: str, *args: Any, **kwargs: Any) -> str:
    """Render one macro of ``web/templates/components/<template>`` to HTML."""
    module = _jinja_env().get_template(f"components/{template}").module
    return str(getattr(module, macro)(*args, **kwargs))


@pytest.fixture(scope="session")
def _chromium_ready(playwright: Any) -> None:
    """Skip locally (fail on CI) when Chromium is not installed."""
    if not Path(playwright.chromium.executable_path).exists():
        if os.environ.get("CI"):
            pytest.fail(_CHROMIUM_MISSING)
        pytest.skip(_CHROMIUM_MISSING)


@pytest.fixture
def ui(request: pytest.FixtureRequest, _chromium_ready: None) -> Generator[UiHarness]:
    """A page served by the single route handler; unexpected requests fail."""
    page = request.getfixturevalue("page")
    harness = UiHarness(page)
    yield harness
    harness.verify_requests()


@pytest.fixture
def screen_client(test_app: Any) -> Generator[Any]:
    """A logged-in TestClient that fetches full screens (no HX-Request header)."""
    from fastapi.testclient import TestClient

    from tests.conftest import login_as

    with TestClient(test_app, follow_redirects=False) as client:
        login_as(
            client,
            name="山田 花子",
            roles=("admin",),
            groups=("企画部", "マーケティング部"),
        )
        yield client
