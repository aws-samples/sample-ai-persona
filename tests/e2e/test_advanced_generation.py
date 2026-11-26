"""Manual E2E of the advanced generation (beta) thin slice.

What only real AWS can show: the multipart upload
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
from collections.abc import Generator
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Locator, Page, expect

from tests.e2e.conftest import DATA_DIR, CreatedRecords

SAMPLE_CSV = DATA_DIR / "advanced_sample.csv"
PERSONA_COUNT = 2
# Upload, completion and the head inspection (within a few seconds).
INSPECT_TIMEOUT_MS = 3 * 60 * 1000
# The generation's own limit is 60 minutes ; wait a little longer.
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
    # Earlier uploads of the same name stay listed ("(2)", "(3)"); the new
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
    # Shares come from the tool output and are written as text
    expect(result.get_by_test_id("advanced-share-badge").first).to_contain_text(
        "全体の"
    )
    expect(result.get_by_test_id("advanced-segment-row").first).to_be_visible()

    # The analysis record opens before saving
    result.get_by_role("button", name="分析の詳細").click()
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
    ``ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS`` (7800: 60 minutes
    of work and 60 of plan waits) so the generation keeps its session. Also shows that a session opens on
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


# --- Reuse, deletion and double completion ---------------------------------
# Real S3 only: the copy onto the same key before the preparation, the
# deletion of every version and the completion of one upload twice.


def _source_id(row: Locator) -> str:
    value = row.get_attribute("data-source-file-id")
    assert value, "the row has no source file ID"
    return value


def _delete_row(page: Page, source_file_id: str) -> None:
    row = page.locator(
        f'[data-testid="advanced-upload-row"][data-source-file-id="{source_file_id}"]'
    )
    row.get_by_test_id("advanced-source-delete-open").click()
    # Every row owns a dialog, so the confirm button is looked up inside this file's one
    page.get_by_test_id(f"advanced-delete-{source_file_id}").get_by_test_id(
        "advanced-source-delete-confirm"
    ).click()
    expect(row).to_have_count(0)


def test_a_file_is_used_again_without_uploading_it_again(
    session_context: BrowserContext, run_tag: str
) -> None:
    """The same file runs two generations; its expiry is shown and kept."""
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
    """No deletion while a generation runs; afterwards the row goes away."""
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
    """A second completion answers 200 with the same row."""
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


# --- Isolation and 1GB -----------------------------------------------------
# Real AWS only. Run with ``-k "isolation or large"``.


def _sandbox_from_env() -> "tuple[object, str]":
    code_interpreter_id = os.environ.get("E2E_CODE_INTERPRETER_ID", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    if not code_interpreter_id or not region:
        pytest.skip("set E2E_CODE_INTERPRETER_ID and AWS_REGION to run this check")

    from src.services.sandbox_service import CodeInterpreterSandbox

    return (
        CodeInterpreterSandbox(
            code_interpreter_id=code_interpreter_id,
            region_name=region,
            library_path=str(LIBRARY_PATH),
        ),
        region,
    )


def _shell(sandbox: object, session: object, command: str) -> str:
    content = sandbox._invoke(session, "executeCommand", {"command": command})  # type: ignore[attr-defined]
    return str(content.get("stdout", ""))


def test_isolation_s3_is_not_readable_from_the_session_and_nothing_carries_over() -> (
    None
):
    """No direct S3 read from inside a session; a new session starts empty.

    The session has no execution
    role, so a direct S3 read is refused, and a file left in one generation's
    session is not in the next generation's session.
    """
    bucket = os.environ.get("E2E_S3_BUCKET", "").strip()
    if not bucket:
        pytest.skip("set E2E_S3_BUCKET (the dev bucket) to run the isolation check")
    sandbox, _ = _sandbox_from_env()
    from src.config import config

    lifetime = config.ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS
    marker = f"isolation-{uuid.uuid4().hex[:8]}"
    first = sandbox.start_session(f"e2e-iso-a-{uuid.uuid4().hex[:8]}", lifetime)  # type: ignore[attr-defined]
    try:
        direct_read = (
            'python3 -c "import boto3,sys\n'
            "try:\n"
            f"    boto3.client('s3').list_objects_v2(Bucket='{bucket}', MaxKeys=1)\n"
            "    print('READ')\n"
            "except Exception as e:\n"
            "    print('DENIED', type(e).__name__)\n"
            '"'
        )
        out = _shell(sandbox, first, direct_read)
        assert "READ" not in out.split(), f"S3 was readable from the session: {out}"
        assert "DENIED" in out
        _shell(sandbox, first, f"echo left-behind > ./{marker}.txt")
        assert marker in _shell(sandbox, first, "ls")
    finally:
        sandbox.stop_session(first)  # type: ignore[attr-defined]

    second = sandbox.start_session(f"e2e-iso-b-{uuid.uuid4().hex[:8]}", lifetime)  # type: ignore[attr-defined]
    try:
        assert marker not in _shell(sandbox, second, "ls")
    finally:
        sandbox.stop_session(second)  # type: ignore[attr-defined]


def test_large_csv_generates_without_running_out_of_memory(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """A Shift_JIS CSV of about 1GB goes through to a result.

    Make the file first::

        uv run python scripts/make_large_sample_csv.py --size-mb 1000 \\
            --columns 30 --out /path/large.csv

    and point ``E2E_LARGE_CSV`` at it. The time from the start to the result is
    printed (about 30 minutes is the aim; a slower run does not fail). The
    preparation time is in the ``advanced_generation.prepared`` log line
    (``duration_ms``) of the same generation.
    """
    path = os.environ.get("E2E_LARGE_CSV", "").strip()
    if not path:
        pytest.skip("set E2E_LARGE_CSV to the file made by make_large_sample_csv.py")
    large = Path(path)
    assert large.is_file(), f"E2E_LARGE_CSV is not a file: {path}"

    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    page.get_by_test_id("advanced-file-input").set_input_files(large)
    row = page.get_by_test_id("advanced-upload-row").filter(has_text=large.name).first
    # A 1GB upload and its inspection take longer than the sample's
    expect(row).to_have_attribute("data-state", "ready", timeout=60 * 60 * 1000)
    row.get_by_test_id("advanced-upload-select").check()

    started = time.monotonic()
    _generate(page, run_tag)
    elapsed = time.monotonic() - started
    print(f"large csv: start to result {elapsed / 60:.1f} minutes")  # noqa: T201

    result = page.get_by_test_id("advanced-result")
    expect(result.get_by_test_id("advanced-candidate")).to_have_count(PERSONA_COUNT)
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

SAMPLE_INTERVIEW = DATA_DIR / "interview_sample.txt"


def _document_files(run_tag: str) -> list[dict]:
    """A small PDF (ASCII text) and a docx with headings, made here (no data)."""
    from tests.document_fixtures import docx_bytes, pdf_bytes

    pdf = pdf_bytes(
        [
            "Interview with customer A (30s, woman, office worker).\n"
            "Value: spending the weekend with her family matters most to her.\n"
            "Pain: waiting thirty minutes at the counter after work is too long.",
            "",
            "Goal: she wants to finish her shopping within twenty minutes.\n"
            "Behavior: she checks reviews on her phone before deciding.",
        ]
    )
    docx = docx_bytes(
        [
            (1, f"面談の記録 {run_tag}"),
            "回答者B（40代・男性・自営業）: 価値: 地元の店を応援したいという気持ちが強い。",
            "回答者B: 不満: 手続きの説明が分かりにくく何度も問い合わせることになる。",
            (2, "これから"),
            "回答者B: 目標: 仕事と家庭の時間の配分を見直したいと思っている。",
        ]
    )
    return [
        {
            "name": f"{run_tag}-interview.pdf",
            "mimeType": "application/pdf",
            "buffer": pdf,
        },
        {
            "name": f"{run_tag}-interview.docx",
            "mimeType": "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            "buffer": docx,
        },
    ]


def _ready_row(page: Page, name: str, timeout: int = INSPECT_TIMEOUT_MS) -> Locator:
    row = page.get_by_test_id("advanced-upload-row").filter(has_text=name).first
    expect(row).to_have_attribute("data-state", "ready", timeout=timeout)
    return row


def test_documents_small_pdf_and_docx_use_the_full_text(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """A PDF (one page without text) and a docx give personas from the full text.

    The blank page is left out, the documents
    are under 200,000 characters, and every persona rests on quoted evidence.
    """
    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    files = _document_files(run_tag)
    page.get_by_test_id("advanced-file-input").set_input_files(files)
    for file in files:
        row = _ready_row(page, file["name"])
        expect(row.get_by_test_id("advanced-upload-status")).to_contain_text(
            "前準備で確定"
        )
        row.get_by_test_id("advanced-upload-select").check()
    expect(page.get_by_test_id("advanced-document-estimate")).to_contain_text("万字")

    _generate(page, run_tag)

    log = page.get_by_test_id("advanced-log").inner_text()
    assert "文字がないため除外" in log
    assert "全文を使います" in log
    result = page.get_by_test_id("advanced-result")
    expect(result.get_by_test_id("advanced-documents")).to_contain_text("3 ページ")
    expect(result.get_by_test_id("advanced-candidate")).to_have_count(PERSONA_COUNT)
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


def test_large_document_extracts_evidence(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """About 800,000 characters go through the evidence extraction.

    Make the file first::

        uv run python scripts/make_large_sample_document.py --chars 800000 \\
            --format docx --headings 40 --out /path/large_interview.docx

    and point ``E2E_LARGE_DOCUMENT`` at it. The time from the start to the
    result, and the progress lines, are printed; the preparation and the
    extraction times and the tokens are in the ``document_prepared`` and
    ``evidence_extracted`` log lines of the app.
    """
    path = os.environ.get("E2E_LARGE_DOCUMENT", "").strip()
    if not path:
        pytest.skip("set E2E_LARGE_DOCUMENT to a file of make_large_sample_document.py")
    large = Path(path)
    assert large.is_file(), f"E2E_LARGE_DOCUMENT is not a file: {path}"

    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    page.get_by_test_id("advanced-file-input").set_input_files(large)
    _ready_row(page, large.name, timeout=30 * 60 * 1000).get_by_test_id(
        "advanced-upload-select"
    ).check()

    started = time.monotonic()
    _generate(page, run_tag)
    elapsed = time.monotonic() - started
    log = page.get_by_test_id("advanced-log").inner_text()
    print(f"large document: start to result {elapsed / 60:.1f} minutes")  # noqa: T201
    print(log)  # noqa: T201

    assert "根拠を取り出します" in log
    assert "根拠の抽出" in log
    result = page.get_by_test_id("advanced-result")
    expect(result.get_by_test_id("advanced-candidate")).to_have_count(PERSONA_COUNT)
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


def test_table_and_document_together(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """One table and one document: segments from the table, quotes from the text.

    The result shows the segments,
    the document used and its evidence (assigned to a segment or for the whole).
    """
    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    page.get_by_test_id("advanced-file-input").set_input_files(
        [SAMPLE_CSV, SAMPLE_INTERVIEW]
    )
    for name in (SAMPLE_CSV.name, SAMPLE_INTERVIEW.name):
        _ready_row(page, name).get_by_test_id("advanced-upload-select").check()

    _generate(page, run_tag)

    result = page.get_by_test_id("advanced-result")
    expect(result.get_by_test_id("advanced-segment-row").first).to_be_visible()
    expect(result.get_by_test_id("advanced-documents")).to_contain_text(
        SAMPLE_INTERVIEW.name
    )
    expect(result.get_by_test_id("advanced-candidate")).to_have_count(PERSONA_COUNT)
    # The quotes are on the personas that use them, or for the whole
    quotes = result.locator(
        "[data-testid='advanced-evidence'] q, [data-testid='advanced-overall-evidence'] q"
    )
    assert quotes.count() > 0, "no document evidence reached the result"
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


# --- The plan confirmation and the cancel ---------------------------------

#: The plan of a small CSV comes within a few minutes; wait a little longer.
PLAN_TIMEOUT_MS = 10 * 60 * 1000
#: A cancel takes effect within 10 seconds (plus a model call that
#: was running, which may take minutes).
CANCEL_TIMEOUT_MS = 6 * 60 * 1000


def _start_without_waiting(page: Page, run_tag: str, *, confirm_plan: bool) -> None:
    page.get_by_test_id("advanced-purpose").fill(
        f"{run_tag} 年代と購入の傾向で分かれるお客さまの層を知りたい"
    )
    page.get_by_test_id("advanced-persona-count").select_option(str(PERSONA_COUNT))
    if confirm_plan:
        page.get_by_test_id("advanced-confirm-plan").check()
    button = page.get_by_test_id("advanced-generate-button")
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_test_id("advanced-progress")).to_be_visible()


def test_plan_is_confirmed_and_the_generation_completes(
    session_context: BrowserContext, created: CreatedRecords, run_tag: str
) -> None:
    """The card shows the plan, approving runs to the end."""
    before = _persona_ids(session_context)
    page = session_context.new_page()
    _open_advanced_page(page)
    _upload_sample(page)
    _start_without_waiting(page, run_tag, confirm_plan=True)

    card = page.get_by_test_id("advanced-plan-card")
    expect(card).to_be_visible(timeout=PLAN_TIMEOUT_MS)
    expect(page.get_by_test_id("advanced-stage-awaiting_plan_approval")).to_be_visible()
    expect(card.get_by_test_id("advanced-plan-purpose")).not_to_be_empty()
    card.get_by_test_id("advanced-plan-approve").click()
    expect(page.get_by_test_id("advanced-plan-accepted")).to_be_visible()

    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    failure = page.get_by_test_id("advanced-failure")
    assert failure.is_hidden(), f"generation failed: {failure.inner_text()}"
    expect(
        page.get_by_test_id("advanced-result").get_by_test_id("advanced-candidate")
    ).to_have_count(PERSONA_COUNT)
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


def test_a_running_generation_is_cancelled_and_started_again(
    session_context: BrowserContext, run_tag: str
) -> None:
    """Cancel, see 中止しました, start again at once.

    The seconds from the click to 中止しました are printed.
    """
    page = session_context.new_page()
    _open_advanced_page(page)
    _upload_sample(page)
    _start_without_waiting(page, run_tag, confirm_plan=False)
    # Cancel while it runs: once the preparation has reported something
    expect(page.get_by_test_id("advanced-log")).not_to_be_empty(timeout=PLAN_TIMEOUT_MS)

    clicked = time.monotonic()
    page.get_by_test_id("advanced-cancel-button").click()
    expect(page.get_by_test_id("advanced-cancelling")).to_be_visible()
    cancelled = page.get_by_test_id("advanced-cancelled")
    expect(cancelled).to_be_visible(timeout=CANCEL_TIMEOUT_MS)
    seconds = time.monotonic() - clicked
    print(f"\n[U5 E2E] seconds from the cancel click to 中止しました: {seconds:.1f}")
    expect(page.get_by_test_id("advanced-failure")).to_be_hidden()
    # The instruction stays; the same file starts again at once
    expect(page.get_by_test_id("advanced-purpose")).to_have_value(
        re.compile(re.escape(run_tag))
    )
    button = page.get_by_test_id("advanced-generate-button")
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_test_id("advanced-already-running")).to_have_count(0)
    new_panel = page.get_by_test_id("advanced-progress")
    expect(new_panel).to_be_visible()
    expect(page.get_by_test_id("advanced-cancelled")).to_be_hidden()
    # Leave nothing running behind
    page.get_by_test_id("advanced-cancel-button").click()
    expect(page.get_by_test_id("advanced-cancelled")).to_be_visible(
        timeout=CANCEL_TIMEOUT_MS
    )
    page.close()


# ---------------------------------------------------------------------------
# The administrator's setting and the AI detailed analysis
# ---------------------------------------------------------------------------
#
# The local user is an administrator. ① changes the setting and puts the
# values it read back at the end. ② turns the AI detailed analysis on (and
# off again at the end), and generates from two CSVs with a common ID column
# (made-up values: customers, one row each, and their purchases).

CUSTOMERS_CSV = DATA_DIR / "advanced_customers.csv"
PURCHASES_CSV = DATA_DIR / "advanced_purchases.csv"
SETTINGS_TIMEOUT_MS = 30 * 1000


def _open_section(page: Page) -> Locator:
    page.goto("/settings/")
    form = page.get_by_test_id("advanced-setting-form")
    expect(form).to_be_visible(timeout=SETTINGS_TIMEOUT_MS)
    return form


def _section_state(form: Locator) -> dict:
    switch = form.get_by_test_id("advanced-setting-detailed")
    return {
        "everyone": form.get_by_test_id("advanced-setting-everyone").is_checked(),
        "detailed": switch.get_attribute("aria-checked") == "true",
        "departments": [
            o.get_attribute("value")
            for o in form.get_by_test_id("department-option").all()
            if o.is_checked()
        ],
        "version": int(form.locator("input[name='version']").input_value()),
    }


def _save_section(page: Page, form: Locator) -> Locator:
    form.get_by_test_id("advanced-setting-save").click()
    expect(page.locator("#flash-messages")).to_contain_text(
        "保存しました", timeout=SETTINGS_TIMEOUT_MS
    )
    return page.get_by_test_id("advanced-setting-form")


def _real_options(form: Locator) -> Locator:
    """Department choices that exist now (a vanished one cannot be saved)."""
    return form.locator(
        "[data-testid='department-option']:not([data-department-vanished])"
    )


def _vanished_checked(form: Locator) -> int:
    return form.locator(
        "[data-testid='department-option'][data-department-vanished]:checked"
    ).count()


def _restore_section(page: Page, before: dict) -> None:
    form = _open_section(page)
    if _section_state(form) == before:
        return  # nothing was changed: leave the stored setting as it is
    radio = (
        "advanced-setting-everyone"
        if before["everyone"]
        else ("advanced-setting-departments")
    )
    form.get_by_test_id(radio).check()
    for option in form.get_by_test_id("department-option").all():
        option.set_checked(option.get_attribute("value") in before["departments"])
    switch = form.get_by_test_id("advanced-setting-detailed")
    if (switch.get_attribute("aria-checked") == "true") != before["detailed"]:
        switch.click()
    _save_section(page, form)


def test_settings_section_saves_departments_and_the_version(
    session_context: BrowserContext,
) -> None:
    """① A department is opened, the version goes up, the notice shows."""
    page = session_context.new_page()
    form = _open_section(page)
    before = _section_state(form)
    options = _real_options(form)
    if options.count() == 0:
        pytest.skip("no department (Cognito group:) exists in the dev environment")
    if _vanished_checked(form):
        pytest.skip(
            "the stored setting holds a department this environment does not have;"
            " saving it would be refused (BR34.3)"
        )
    try:
        form.get_by_test_id("advanced-setting-departments").check()
        first = options.first
        if not first.is_checked():
            first.check()
        saved = _save_section(page, form)
        after = _section_state(saved)
        assert after["version"] == before["version"] + 1
        assert not after["everyone"]
        assert first.get_attribute("value") in after["departments"]
        page.reload()
        assert _section_state(_open_section(page))["version"] == after["version"]
    finally:
        _restore_section(page, before)
        page.close()


def _upload_named(page: Page, path: Path) -> Locator:
    page.get_by_test_id("advanced-file-input").set_input_files(path)
    row = page.get_by_test_id("advanced-upload-row").filter(has_text=path.name).first
    expect(row).to_have_attribute("data-state", "ready", timeout=INSPECT_TIMEOUT_MS)
    row.get_by_test_id("advanced-upload-select").check()
    return row


def test_detailed_analysis_joins_two_tables_and_records_the_code(
    session_context: BrowserContext, run_tag: str
) -> None:
    """② With the detailed analysis on, two CSVs join on customer_id."""
    page = session_context.new_page()
    form = _open_section(page)
    before = _section_state(form)
    try:
        switch = form.get_by_test_id("advanced-setting-detailed")
        if switch.get_attribute("aria-checked") != "true":
            if _vanished_checked(form):
                pytest.skip(
                    "the stored setting holds a department this environment does"
                    " not have; turn the detailed analysis on by hand first"
                )
            switch.click()
            _save_section(page, form)

        _open_advanced_page(page)
        box = page.get_by_test_id("advanced-detailed-analysis")
        expect(box).to_be_checked()
        _upload_named(page, CUSTOMERS_CSV)
        _upload_named(page, PURCHASES_CSV)
        started = time.monotonic()
        _generate(page, run_tag)
        print(
            f"\n[U6 E2E] detailed generation seconds: {time.monotonic() - started:.0f}"
        )

        result = page.get_by_test_id("advanced-result")
        result.get_by_role("button", name="分析の詳細").click()
        record = page.get_by_test_id("advanced-record")
        expect(record).to_be_visible()
        kinds = record.get_by_test_id("advanced-record-code-kind")
        expect(kinds.filter(has_text="AI による詳細分析").first).to_be_visible()
        # Either "結合した列: ..." (join_tables) or "使った表: ..." (a merge of the
        # code's own) names the purchases table: both tables were used.
        expect(record.get_by_test_id("advanced-record-joined").first).to_contain_text(
            PURCHASES_CSV.name
        )
        page.keyboard.press("Escape")
    finally:
        _restore_section(page, before)
        page.close()


# Files this module uploads. Each user may hold only ADVANCED_UPLOAD_MAX_FILES
# files, so leftovers from earlier runs stop later uploads from starting at all.
_OWN_UPLOAD_NAME = re.compile(
    r"^(E2E-\d+-|advanced_sample|interview_sample|advanced_customers|advanced_purchases)"
)


def _delete_own_uploads(context: BrowserContext) -> None:
    """Delete the files this module uploads; other files and busy rows are left alone."""
    page = context.new_page()
    try:
        page.goto("/persona/generation/advanced")
        rows = page.get_by_test_id("advanced-upload-row")
        targets = [
            row.get_attribute("data-source-file-id")
            for row in rows.all()
            if row.get_attribute("data-state") in ("ready", "error")
            and _OWN_UPLOAD_NAME.match(row.locator("[data-role=name]").inner_text())
        ]
        for source_file_id in targets:
            context.request.delete(
                f"/persona/advanced/sources/{source_file_id}",
                headers={"HX-Request": "true", "X-Requested-With": "XMLHttpRequest"},
            )
    finally:
        page.close()


@pytest.fixture(scope="module", autouse=True)
def _clean_uploads(session_context: BrowserContext) -> Generator[None, None, None]:
    _delete_own_uploads(session_context)
    yield
    _delete_own_uploads(session_context)
