"""§6 マスアンケート regression flows (up to target preview; no survey execution).

§6.1 CSV upload with column mapping, §6.2 template CRUD and AI draft generation,
§6.3 start page target preview. Executing a survey (Bedrock batch inference) and
§6.4 results are out of scope because a run takes far too long for regression.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.conftest import LLM_TIMEOUT_MS

# /survey/start computes filter options from S3 Parquet via DuckDB (slow when cold).
PARQUET_TIMEOUT_MS = 120_000

SAMPLE_CSV = (
    Path(__file__).parents[2] / "sample_data" / "sample_custom_personas_500.csv"
)


@dataclass
class SurveyRecords:
    """Survey-side records created by this module (the only ones teardown deletes)."""

    dataset_names: list[str] = field(default_factory=list)
    template_ids: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def survey_records(
    session_context: BrowserContext,
) -> Generator[SurveyRecords, None, None]:
    records = SurveyRecords()
    yield records
    headers = {"HX-Request": "true"}
    for template_id in records.template_ids:
        session_context.request.delete(
            f"/survey/templates/{template_id}", headers=headers
        )
    for name in records.dataset_names:
        session_context.request.delete(
            f"/survey/persona-data/custom/{name}", headers=headers
        )


@pytest.fixture(scope="module")
def survey_csv(tmp_path_factory: pytest.TempPathFactory, run_tag: str) -> Path:
    """Copy of the sample CSV whose file stem (= dataset name) carries the run tag."""
    path = tmp_path_factory.mktemp("survey") / f"{run_tag}_personas.csv"
    shutil.copyfile(SAMPLE_CSV, path)
    return path


def _require_dataset(records: SurveyRecords) -> str:
    if not records.dataset_names:
        pytest.skip("custom dataset was not uploaded")
    return records.dataset_names[0]


def _require_template(records: SurveyRecords) -> str:
    if not records.template_ids:
        pytest.skip("survey template was not created")
    return records.template_ids[0]


def test_custom_dataset_upload_with_mapping(
    page: Page, survey_csv: Path, survey_records: SurveyRecords
) -> None:
    dataset_name = survey_csv.stem
    page.goto("/survey/persona-data")
    page.locator("#custom-csv-input").set_input_files(survey_csv)
    page.locator(
        'form[hx-post="/survey/persona-data/upload-custom"] [type=submit]'
    ).click()

    persona_select = page.locator('select[name="mapping_persona"]')
    expect(persona_select).to_be_visible(timeout=60_000)
    if not persona_select.input_value():
        persona_select.select_option("顧客プロフィール")

    page.locator('button[hx-post="/survey/persona-data/preview-prompt"]').click()
    expect(page.locator("#prompt-preview")).not_to_be_empty()

    page.locator(
        'form[hx-post="/survey/persona-data/confirm-mapping"] [type=submit]'
    ).click()
    expect(page.locator("#custom-upload-result")).to_contain_text(
        f"{dataset_name} をアップロードしました", timeout=120_000
    )
    survey_records.dataset_names.append(dataset_name)
    expect(page.locator("#custom-dataset-list")).to_contain_text(dataset_name)


def test_template_create_and_edit(
    page: Page, survey_records: SurveyRecords, run_tag: str
) -> None:
    name = f"{run_tag} テンプレート"
    page.goto("/survey/templates/new")
    page.locator('#template-form input[name="name"]').fill(name)

    page.locator("button[\\@click=\"addQuestion('multiple_choice')\"]").click()
    page.locator("button[\\@click=\"addQuestion('free_text')\"]").click()
    page.locator("button[\\@click=\"addQuestion('scale_rating')\"]").click()

    texts = page.locator('input[x-model="q.text"]')
    expect(texts).to_have_count(3)
    texts.nth(0).fill("定期購入サービスを利用していますか？")
    options = page.locator('input[x-model="q.options[optIdx]"]')
    expect(options).to_have_count(2)
    options.nth(0).fill("利用している")
    options.nth(1).fill("利用していない")
    texts.nth(1).fill("解約時に困った経験を教えてください")
    texts.nth(2).fill("現在のサービスへの満足度を教えてください")

    page.locator('#template-form button[type="submit"]').click()
    page.wait_for_url("**/survey/templates")
    card = page.locator('[id^="template-card-"]').filter(has_text=name)
    expect(card).to_have_count(1)
    expect(card).to_contain_text("3")
    template_id = (card.get_attribute("id") or "").removeprefix("template-card-")
    survey_records.template_ids.append(template_id)

    card.locator(f'a[href="/survey/templates/{template_id}/edit"]').click()
    edited_name = f"{name} 改"
    page.locator('#template-form input[name="name"]').fill(edited_name)
    page.locator('#template-form button[type="submit"]').click()
    page.wait_for_url("**/survey/templates")
    expect(page.locator(f"#template-card-{template_id}")).to_contain_text(edited_name)


def test_survey_start_target_preview(page: Page, survey_records: SurveyRecords) -> None:
    dataset_name = _require_dataset(survey_records)
    template_id = _require_template(survey_records)
    page.goto("/survey/start", timeout=PARQUET_TIMEOUT_MS)
    page.locator('select[name="template_id"]').select_option(template_id)
    page.locator('select[name="datasource"]').select_option(f"custom:{dataset_name}")
    expect(page.locator("#filter-fields")).not_to_be_empty(timeout=PARQUET_TIMEOUT_MS)

    page.locator('button:has-text("対象ペルソナをプレビュー")').click()
    expect(page.locator("#filter-preview")).to_contain_text(
        "500 / 500 名", timeout=PARQUET_TIMEOUT_MS
    )


def test_ai_draft_generation(page: Page) -> None:
    page.goto("/survey/templates/new")
    page.locator('button:has-text("AIで生成")').click()
    page.locator('textarea[x-model="aiInput"]').fill(
        "定期購入サービスの解約理由を30〜40代の利用者に聞きたいです"
    )
    page.locator('button:has-text("送信")').click()

    draft_btn = page.locator('button:has-text("ドラフト生成")')
    expect(draft_btn).to_be_enabled(timeout=LLM_TIMEOUT_MS)
    draft_btn.click()
    page.wait_for_function(
        "document.querySelectorAll('select[x-model=\"q.question_type\"]').length >= 3",
        timeout=LLM_TIMEOUT_MS,
    )
    # Draft only; leaving without saving keeps no data behind.


def test_template_delete_from_list(page: Page, survey_records: SurveyRecords) -> None:
    template_id = _require_template(survey_records)
    page.goto("/survey/templates")
    card = page.locator(f"#template-card-{template_id}")
    card.locator(f'button[hx-delete="/survey/templates/{template_id}"]').click()
    expect(card).to_have_count(0)
    survey_records.template_ids.remove(template_id)


def test_custom_dataset_delete_from_list(
    page: Page, survey_records: SurveyRecords
) -> None:
    dataset_name = _require_dataset(survey_records)
    page.goto("/survey/persona-data")
    dataset_list = page.locator("#custom-dataset-list")
    expect(dataset_list).to_contain_text(dataset_name)
    dataset_list.locator(
        f'button[hx-delete="/survey/persona-data/custom/{dataset_name}"]'
    ).click()
    expect(dataset_list).not_to_contain_text(dataset_name)
    survey_records.dataset_names.remove(dataset_name)
