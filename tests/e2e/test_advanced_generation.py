"""Manual E2E of the advanced generation (beta) thin slice (U1, US5.1, US10.2, NFR9.4).

What only real AWS can show (team.md Testing Posture): the multipart upload
with presigned part URLs and the bucket CORS (ETag), the head inspection, the
Code Interpreter session (no execution role, presigned download) and the
models, then saving a candidate and its analysis record.

Run it after the dev deployment, against a local app that points at the dev
stack (``uv run pytest tests/e2e/test_advanced_generation.py -m e2e``):

- The app's ``.env`` has ``CODE_INTERPRETER_ID``, the three table names and the
  bucket of the dev stack (CDK outputs / the task definition), and the local
  user may use the beta (an admin while AppSettings has no release value).
- The bucket's CORS allows ``http://localhost:8000`` on the dev Stage only.
- The session lifetime check talks to Code Interpreter directly with the
  shell's AWS credentials. It needs ``E2E_CODE_INTERPRETER_ID`` (the same ID)
  and ``AWS_REGION``, and takes about 16 minutes.

The test data (``data/advanced_sample.csv``) is made-up values only.
"""

import os
import re
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Locator, Page, expect

from tests.e2e.conftest import DATA_DIR, CreatedRecords

SAMPLE_CSV = DATA_DIR / "advanced_sample.csv"
PERSONA_COUNT = 2
# Upload, completion and the head inspection (BR3.1: within a few seconds).
INSPECT_TIMEOUT_MS = 3 * 60 * 1000
# The generation's own limit is 60 minutes (NFR5.14); wait a little longer.
GENERATION_TIMEOUT_MS = 65 * 60 * 1000
# Longer than Code Interpreter's default session timeout (900 seconds).
SESSION_CHECK_WAIT_SECONDS = 16 * 60

LIBRARY_PATH = Path(__file__).resolve().parents[2] / "sandbox" / "persona_analysis.py"


def _persona_ids(context: BrowserContext) -> set[str]:
    response = context.request.get("/api/personas")
    assert response.ok, f"/api/personas returned {response.status}"
    return {p["id"] for p in response.json()}


def _open_advanced_page(page: Page) -> None:
    page.goto("/persona/generation/advanced")
    if not re.search(r"/persona/generation/advanced", page.url):
        pytest.fail(
            "the advanced generation page redirected to "
            f"{page.url}: check ENABLE_ADVANCED_GENERATION, CODE_INTERPRETER_ID "
            "and that the local user may use the beta"
        )
    expect(page.get_by_test_id("advanced-generation-page")).to_be_visible()


def _upload_sample(page: Page) -> Locator:
    page.get_by_test_id("advanced-file-input").set_input_files(SAMPLE_CSV)
    # U2: earlier uploads of the same name stay listed ("(2)", "(3)"); the new
    # row is put first
    row = (
        page.get_by_test_id("advanced-upload-row")
        .filter(has_text=SAMPLE_CSV.name)
        .first
    )
    # The parts are PUT straight to S3 (CORS, ETag), then completed and inspected
    expect(row).to_have_attribute("data-state", "ready", timeout=INSPECT_TIMEOUT_MS)
    expect(row.get_by_test_id("advanced-upload-status")).to_contain_text("準備OK")
    row.get_by_test_id("advanced-upload-select").check()
    return row


def _generate(page: Page, run_tag: str) -> None:
    page.get_by_test_id("advanced-purpose").fill(
        f"{run_tag} 年代と購入の傾向で分かれるお客さまの層を知りたい"
    )
    page.get_by_test_id("advanced-persona-count").select_option(str(PERSONA_COUNT))
    button = page.get_by_test_id("advanced-generate-button")
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_test_id("advanced-progress")).to_be_visible()

    # Either the result or the failure panel ends the wait
    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    failure = page.get_by_test_id("advanced-failure")
    assert failure.is_hidden(), f"generation failed: {failure.inner_text()}"


def test_upload_generate_save_and_badge(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """A small CSV goes from upload to a saved persona with the beta badge."""
    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    _upload_sample(page)
    _generate(page, run_tag)

    result = page.get_by_test_id("advanced-result")
    candidates = result.get_by_test_id("advanced-candidate")
    expect(candidates).to_have_count(PERSONA_COUNT)
    # Shares come from the tool output and are written as text (BR8.4)
    expect(result.get_by_test_id("advanced-share-badge").first).to_contain_text(
        "全体の"
    )
    expect(result.get_by_test_id("advanced-segment-row").first).to_be_visible()

    # The analysis record opens before saving (US10.2)
    result.get_by_role("button", name="分析の記録").click()
    expect(page.get_by_test_id("advanced-record")).to_be_visible()
    page.keyboard.press("Escape")

    candidates.first.get_by_test_id("advanced-candidate-check").check()
    result.get_by_test_id("advanced-save-button").click()
    expect(page.locator("#advanced-save-result")).to_contain_text(
        "1人のペルソナを保存しました"
    )

    saved = sorted(_persona_ids(session_context) - before)
    created.persona_ids.extend(saved)
    assert len(saved) == 1, f"expected 1 saved persona, found {saved}"

    page.goto(f"/persona/{saved[0]}")
    expect(page.get_by_test_id("persona-advanced-badge")).to_contain_text(
        "高度な生成（ベータ）"
    )
    expect(page.get_by_test_id("persona-advanced-summary")).to_contain_text(
        "分析の要約"
    )
    page.close()


def test_code_interpreter_session_outlives_15_minutes() -> None:
    """The session opened with the app's lifetime is still usable after 16 minutes.

    Code Interpreter's default session timeout is 900 seconds; the app asks for
    ``ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS`` (3900) so the 60-minute
    generation keeps its session (NFR5.10). Also shows that a session opens on
    the Code Interpreter without an execution role.
    """
    code_interpreter_id = os.environ.get("E2E_CODE_INTERPRETER_ID", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    if not code_interpreter_id or not region:
        pytest.skip("set E2E_CODE_INTERPRETER_ID and AWS_REGION to run this check")

    from src.config import config
    from src.services.sandbox_service import CodeInterpreterSandbox

    sandbox = CodeInterpreterSandbox(
        code_interpreter_id=code_interpreter_id,
        region_name=region,
        library_path=str(LIBRARY_PATH),
    )
    session = sandbox.start_session(
        f"e2e-{uuid.uuid4().hex[:12]}",
        config.ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS,
    )
    try:
        time.sleep(SESSION_CHECK_WAIT_SECONDS)
        # A library call on a source that is not there: the session answers with
        # the library's own error. An expired session raises SandboxServiceError.
        result = sandbox.call(session, "profile_dataset", {"source_id": "missing"})
        assert result.get("ok") is False
        assert "error" in result
    finally:
        sandbox.stop_session(session)


# --- U2: 使い回し・削除・二重の確定（cicd-pipeline.md §3） -----------------------
# Real S3 only: the copy onto the same key before the preparation (BR13.4), the
# deletion of every version (BR14.1) and the completion of one upload twice.


def _source_id(row: Locator) -> str:
    value = row.get_attribute("data-source-file-id")
    assert value, "the row has no source file ID"
    return value


def _delete_row(page: Page, source_file_id: str) -> None:
    row = page.locator(
        f'[data-testid="advanced-upload-row"][data-source-file-id="{source_file_id}"]'
    )
    row.get_by_test_id("advanced-source-delete-open").click()
    page.get_by_test_id("advanced-source-delete-confirm").click()
    expect(row).to_have_count(0)


def test_a_file_is_used_again_without_uploading_it_again(
    session_context: BrowserContext, run_tag: str
) -> None:
    """US4.1: the same file runs two generations; its expiry is shown and kept."""
    page = session_context.new_page()
    _open_advanced_page(page)
    source_file_id = _source_id(_upload_sample(page))
    _generate(page, run_tag)

    # Opened again: the file is still listed, ready, with the day it goes away
    _open_advanced_page(page)
    row = page.locator(
        f'[data-testid="advanced-upload-row"][data-source-file-id="{source_file_id}"]'
    )
    expect(row).to_have_attribute("data-state", "ready")
    expect(row.get_by_test_id("advanced-upload-expiry")).to_contain_text("/")
    row.get_by_test_id("advanced-upload-select").check()
    _generate(page, f"{run_tag} 2 回目")

    _open_advanced_page(page)
    _delete_row(page, source_file_id)
    page.close()


def test_a_file_in_use_cannot_be_deleted_and_a_free_one_is_gone(
    session_context: BrowserContext, run_tag: str
) -> None:
    """US4.2: no deletion while a generation runs; afterwards the row goes away."""
    page = session_context.new_page()
    _open_advanced_page(page)
    source_file_id = _source_id(_upload_sample(page))
    page.get_by_test_id("advanced-purpose").fill(f"{run_tag} 削除の確認")
    page.get_by_test_id("advanced-generate-button").click()
    expect(page.get_by_test_id("advanced-progress")).to_be_visible()

    # While it runs, the reopened list shows the button as unusable with a reason
    other = session_context.new_page()
    _open_advanced_page(other)
    busy = other.locator(
        f'[data-testid="advanced-upload-row"][data-source-file-id="{source_file_id}"]'
    )
    expect(busy.get_by_test_id("advanced-source-delete-open")).to_have_attribute(
        "aria-disabled", "true"
    )
    expect(busy).to_contain_text("生成中のため削除できません")
    response = other.request.delete(
        f"/persona/advanced/sources/{source_file_id}",
        headers={"X-Requested-With": "XMLHttpRequest", "HX-Request": "true"},
    )
    assert response.status == 503
    other.close()

    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    _open_advanced_page(page)
    _delete_row(page, source_file_id)
    _open_advanced_page(page)
    expect(page.locator(f'[data-source-file-id="{source_file_id}"]')).to_have_count(0)
    page.close()


def test_completing_twice_gives_both_tabs_the_same_result(
    session_context: BrowserContext,
) -> None:
    """US2.4 (AC2.4.2): a second completion answers 200 with the same row."""
    page = session_context.new_page()
    _open_advanced_page(page)
    row = _upload_sample(page)
    source_file_id = _source_id(row)

    response = page.request.post(
        f"/persona/advanced/uploads/{source_file_id}/complete",
        data={"parts": []},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status == 200
    assert response.json()["status"] == "ready"
    _delete_row(page, source_file_id)
    page.close()
