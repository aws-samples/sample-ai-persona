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
    ``ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS`` (7800 since U5: 60 minutes
    of work and 60 of plan waits) so the generation keeps its session (NFR5.10,
    NFR5.50). Also shows that a session opens on
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
    # Every row owns a dialog, so the confirm button is looked up inside this file's one
    page.get_by_test_id(f"advanced-delete-{source_file_id}").get_by_test_id(
        "advanced-source-delete-confirm"
    ).click()
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


# --- U3: 隔離と 1GB（BR19.3、BR20.1、BR20.2） ---------------------------------------
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

    BR19.1, BR19.3 (AC5.6.1, AC5.6.2, AC5.6.3): the session has no execution
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
    """A Shift_JIS CSV of about 1GB goes through to a result (US5.7, BR20.1).

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
# U4: documents (NFR1.15, NFR2.7, NFR7.3, NFR9.8)
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

    BR24.3, BR25.2, BR27.1, BR27.2: the blank page is left out, the documents
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
    """About 800,000 characters go through the evidence extraction (NFR1.15).

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

    BR23.1, BR28.1, BR28.2 (AC7.1.1, AC7.1.2): the result shows the segments,
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
    # The quotes are on the personas that use them, or for the whole (BR28.2)
    quotes = result.locator(
        "[data-testid='advanced-evidence'] q, [data-testid='advanced-overall-evidence'] q"
    )
    assert quotes.count() > 0, "no document evidence reached the result"
    created.persona_ids.extend(sorted(_persona_ids(session_context) - before))
    page.close()


# --- U5: the plan confirmation and the cancel (NFR1.23, NFR7.4, NFR9.10) ------

#: The plan of a small CSV comes within a few minutes; wait a little longer.
PLAN_TIMEOUT_MS = 10 * 60 * 1000
#: NFR1.23: a cancel takes effect within 10 seconds (plus a model call that
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
    """US8.1 (AC8.1.1): the card shows the plan, approving runs to the end."""
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
    """US9.3 (AC9.3.1, AC9.3.2): cancel, see 中止しました, start again at once.

    The seconds from the click to 中止しました are printed for code-summary.md
    (NFR1.23, NFR7.4).
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
    # BR33.3: the instruction stays; the same file starts again at once
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
