"""Fixtures for the fixed browser E2E regression suite.

The suite drives a running app (default ``http://localhost:8000``) against real
AWS (Bedrock / DynamoDB / S3). It is excluded from the default pytest run via
``addopts = -m "not e2e"``; run it explicitly with ``uv run pytest -m e2e``.

Every record created by the suite carries the per-run tag (``E2E-<timestamp>``)
and its ID is registered in ``created``; teardown deletes only those IDs.

Preconditions (skills and docs refer here):
- The app is running (``uv run python run_htmx.py``); otherwise the suite skips.
- Chromium is installed once: ``uv run playwright install chromium``.
- The app process sees the same ``AWS_REGION`` as ``.env``. ``load_dotenv`` does
  not override variables already set in the shell, so a stale ``AWS_REGION``
  silently points Bedrock / S3 / DuckDB at another region (e.g. survey preview
  fails with an S3 301). Start it as ``AWS_REGION=<.env value> uv run python
  run_htmx.py`` if the shell exports a different one.
- Local only: there is no login step, so Cognito-protected staging is unsupported.
- Takes ~10 minutes and makes real Bedrock calls; run it only with user consent.
"""

import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

_THIS_DIR = Path(__file__).parent
DATA_DIR = _THIS_DIR / "data"

# Real Bedrock calls: generation / discussion / report take minutes.
LLM_TIMEOUT_MS = 10 * 60 * 1000


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if str(item.fspath).startswith(str(_THIS_DIR)):
            item.add_marker(pytest.mark.e2e)


@dataclass
class CreatedRecords:
    """IDs of records created during this run (the only ones teardown deletes)."""

    persona_ids: list[str] = field(default_factory=list)
    discussion_ids: list[str] = field(default_factory=list)


@pytest.fixture(scope="session")
def base_url() -> str:
    """Target app URL (override with E2E_BASE_URL)."""
    url = os.environ.get("E2E_BASE_URL", "http://localhost:8000").rstrip("/")
    try:
        urllib.request.urlopen(f"{url}/health", timeout=5)
    except (urllib.error.URLError, OSError):
        pytest.skip(f"App is not running at {url} (start it with run_htmx.py)")
    return url


@pytest.fixture(scope="session")
def run_tag() -> str:
    """Unique marker embedded in names/topics created by this run."""
    return f"E2E-{time.strftime('%Y%m%d%H%M%S')}"


@pytest.fixture(scope="session")
def created() -> CreatedRecords:
    return CreatedRecords()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict, base_url: str) -> dict:
    return {**browser_context_args, "base_url": base_url, "locale": "ja-JP"}


@pytest.fixture(autouse=True)
def _accept_confirm_dialogs(page: Page) -> None:
    """Accept hx-confirm / window.confirm dialogs."""
    page.on("dialog", lambda dialog: dialog.accept())


@pytest.fixture(scope="session")
def session_context(
    browser: Browser, browser_context_args: dict, created: CreatedRecords
) -> Generator[BrowserContext, None, None]:
    """Session-wide context used for shared setup and teardown."""
    context = browser.new_context(**browser_context_args)
    yield context
    headers = {"HX-Request": "true"}
    for discussion_id in created.discussion_ids:
        context.request.delete(f"/discussion/{discussion_id}", headers=headers)
    for persona_id in created.persona_ids:
        context.request.delete(f"/persona/{persona_id}", headers=headers)
    context.close()


@pytest.fixture(scope="session")
def generated_personas(
    session_context: BrowserContext, created: CreatedRecords
) -> list[dict[str, str]]:
    """Generate and save 3 personas through the §1 generation UI.

    Returns ``[{"id": ..., "name": ...}, ...]``. Shared by persona, discussion
    and interview tests so the expensive generation runs once per session.
    """
    page = session_context.new_page()
    page.goto("/persona/generation")
    page.locator("#file-input").set_input_files(DATA_DIR / "interview_sample.txt")
    expect(page.locator("#file-list")).to_contain_text("interview_sample.txt")
    page.locator("#persona_count").fill("3")
    page.locator("#generate-btn").click()

    candidates = page.locator('#save-personas-form input[name="persona_check"]')
    expect(candidates).to_have_count(3, timeout=LLM_TIMEOUT_MS)

    personas = []
    for i in range(3):
        checkbox = candidates.nth(i)
        persona_id = checkbox.get_attribute("value") or ""
        name = page.locator(f'label[for="persona-{persona_id}"] .font-semibold').first
        personas.append({"id": persona_id, "name": name.inner_text().strip()})
        checkbox.check()

    page.locator("#save-selected-btn").click()
    expect(page.locator("#save-result")).to_contain_text("3人のペルソナを保存しました")
    created.persona_ids.extend(p["id"] for p in personas)
    page.close()
    return personas


def discussion_id_from_url(url: str) -> str:
    match = re.search(r"/discussion/([0-9a-f-]{36})", url)
    assert match, f"discussion id not found in {url}"
    return match.group(1)


def start_discussion(
    page: Page,
    *,
    mode: str,
    persona_ids: list[str],
    topic: str,
    streaming: bool,
    rounds: int | None = None,
    on_stream: Callable[[Page], None] | None = None,
) -> str:
    """Start a discussion from /discussion/setup and return its ID on completion.

    ``mode`` is the setup radio value (``traditional`` / ``agent``). With
    ``streaming`` the 「リアルタイム表示」 path (SSE) is used and at least one
    message must appear in the live stream before the result is shown;
    ``on_stream`` then runs extra assertions against the live stream before
    the final result is awaited.
    """
    page.goto("/discussion/setup")
    page.locator(f'label:has(input[name="mode"][value="{mode}"])').click()
    for persona_id in persona_ids:
        page.locator(f"#persona-card-{persona_id}").click()
    page.locator('textarea[name="topic"]').fill(topic)
    if rounds is not None:
        page.locator('input[name="rounds"]').fill(str(rounds))
    page.locator('input[x-model="streamingMode"]').set_checked(streaming)

    if streaming:
        page.locator('button:has-text("リアルタイム議論を開始")').click()
        expect(page.locator("#streaming-messages .message-item").first).to_be_visible(
            timeout=LLM_TIMEOUT_MS
        )
        if on_stream is not None:
            on_stream(page)
    else:
        page.locator('button[type="submit"]:has-text("議論を開始")').click()

    detail_link = page.locator(
        '#discussion-result a[href^="/discussion/"]:not([href="/discussion/results"])'
    )
    expect(detail_link.first).to_be_visible(timeout=LLM_TIMEOUT_MS)
    return discussion_id_from_url(detail_link.first.get_attribute("href") or "")
