"""§6 マスアンケート: 複数画像（最大3枚）のテンプレートを作り、実行して結果まで確かめる。

テンプレートの画像の複数アップロード（枠・通知・仮の名前）、保存、名前の重複の拒否、
アンケートの実行（Step Functions）、結果画面の画像表示。実行には ``SURVEY_STATE_MACHINE_ARN`` の
設定が要る（手元の .env）。ペルソナ数は下限の 10 人で、実 Bedrock を呼ぶ。
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import pytest
from PIL import Image
from playwright.sync_api import BrowserContext, Page, expect

from src.managers.survey_template_manager import MAX_TEMPLATE_IMAGES
from src.models.survey import MIN_SURVEY_PERSONAS

# /survey/start computes filter options from S3 Parquet via DuckDB (slow when cold).
PARQUET_TIMEOUT_MS = 120_000
# A run of the minimum size: the runner round, Step Functions and the result writer.
RUN_TIMEOUT_S = 10 * 60
POLL_INTERVAL_S = 10

IMAGE_NAMES = ["広告A", "広告B", "広告C"]
COLORS = [(200, 60, 60), (60, 160, 90), (60, 90, 200), (220, 180, 40)]
DROP_ZONE_TEXT = "クリックまたはドラッグ&ドロップで画像をアップロード"


@dataclass
class Records:
    """Records created by this module (the only ones teardown deletes)."""

    template_ids: list[str] = field(default_factory=list)
    survey_ids: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def records(session_context: BrowserContext) -> Generator[Records, None, None]:
    created = Records()
    yield created
    headers = {"HX-Request": "true"}
    for survey_id in created.survey_ids:
        session_context.request.delete(f"/survey/results/{survey_id}", headers=headers)
    for template_id in created.template_ids:
        session_context.request.delete(
            f"/survey/templates/{template_id}", headers=headers
        )


@pytest.fixture(scope="module")
def image_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Four JPEGs (one more than the limit) and a GIF (a format the form refuses)."""
    folder = tmp_path_factory.mktemp("images")
    files: dict[str, Path] = {}
    for index, color in enumerate(COLORS):
        path = folder / f"design_{index}.jpg"
        Image.new("RGB", (480, 320), color).save(path, "JPEG")
        files[path.name] = path
    gif = folder / "animation.gif"
    Image.new("RGB", (64, 64), (0, 0, 0)).save(gif, "GIF")
    files[gif.name] = gif
    return files


def _require_template(records: Records) -> str:
    if not records.template_ids:
        pytest.skip("template was not created")
    return records.template_ids[0]


def test_upload_keeps_to_the_limit_and_names_each_image(
    page: Page,
    records: Records,
    run_tag: str,
    image_files: dict[str, Path],
) -> None:
    dialogs: list[str] = []
    # conftest accepts every dialog; this listener only records the messages
    page.on("dialog", lambda dialog: dialogs.append(dialog.message))

    name = f"{run_tag} 複数画像"
    page.goto("/survey/templates/new")
    page.locator('#template-form input[name="name"]').fill(name)

    # 4 JPEGs + 1 GIF at once: the GIF takes no slot, the 4th JPEG is over the limit
    selected = ["design_0.jpg", "design_1.jpg", "animation.gif"]
    selected += ["design_2.jpg", "design_3.jpg"]
    page.locator('input[x-ref="imageInput"]').set_input_files(
        [str(image_files[file]) for file in selected]
    )

    names = page.locator('input[x-model="img.name"]')
    expect(names).to_have_count(MAX_TEMPLATE_IMAGES, timeout=60_000)
    expect(
        page.get_by_text(f"{MAX_TEMPLATE_IMAGES}/{MAX_TEMPLATE_IMAGES}枚")
    ).to_be_visible()
    # the drop zone closes once the images are full
    expect(page.get_by_text(DROP_ZONE_TEXT)).to_be_hidden()

    assert len(dialogs) == 1, dialogs
    assert "animation.gif" in dialogs[0]
    assert "1枚は追加しませんでした" in dialogs[0]

    # each image got its own provisional name (never left blank or repeated)
    provisional = [names.nth(i).input_value() for i in range(MAX_TEMPLATE_IMAGES)]
    assert all(provisional)
    assert len(set(provisional)) == MAX_TEMPLATE_IMAGES

    for index, image_name in enumerate(IMAGE_NAMES):
        names.nth(index).fill(image_name)

    page.locator("button[\\@click=\"addQuestion('scale_rating')\"]").click()
    page.locator('input[x-model="q.text"]').first.fill(
        "広告A、広告B、広告Cのうち、広告Aの印象の良さを5段階で教えてください"
    )
    page.locator('#template-form button[type="submit"]').click()
    page.wait_for_url("**/survey/templates")

    card = page.locator('[id^="template-card-"]').filter(has_text=name)
    expect(card).to_have_count(1)
    expect(card).to_contain_text("画像あり")
    template_id = (card.get_attribute("id") or "").removeprefix("template-card-")
    records.template_ids.append(template_id)


def test_saved_images_come_back_in_the_edit_form(page: Page, records: Records) -> None:
    template_id = _require_template(records)
    page.goto(f"/survey/templates/{template_id}/edit")
    names = page.locator('input[x-model="img.name"]')
    expect(names).to_have_count(MAX_TEMPLATE_IMAGES)
    saved = [names.nth(i).input_value() for i in range(MAX_TEMPLATE_IMAGES)]
    assert saved == IMAGE_NAMES
    expect(page.get_by_text(DROP_ZONE_TEXT)).to_be_hidden()


def test_duplicate_image_names_are_refused_on_save(
    page: Page, records: Records
) -> None:
    template_id = _require_template(records)
    page.goto(f"/survey/templates/{template_id}/edit")
    names = page.locator('input[x-model="img.name"]')
    expect(names).to_have_count(MAX_TEMPLATE_IMAGES)
    # the same as 広告A once width, spacing and case are folded
    names.nth(1).fill(" 広告ａ ")
    page.locator('#template-form button[type="submit"]').click()
    expect(page.locator("body")).to_contain_text("画像の名前が重複しています")
    assert page.url.endswith("/edit")


def test_survey_runs_with_all_images_and_completes(
    page: Page, records: Records, run_tag: str
) -> None:
    template_id = _require_template(records)
    survey_name = f"{run_tag} 画像アンケート"

    page.goto("/survey/start", timeout=PARQUET_TIMEOUT_MS)
    page.locator('select[name="template_id"]').select_option(template_id)
    expect(page.locator("#filter-fields")).not_to_be_empty(timeout=PARQUET_TIMEOUT_MS)
    page.locator('input[name="name"]').fill(survey_name)
    page.locator('input[name="persona_count"]').fill(str(MIN_SURVEY_PERSONAS))
    page.locator('#survey-form button[type="submit"]').click()
    page.wait_for_url("**/survey/results", timeout=60_000)

    card = page.locator('[id^="survey-card-"]').filter(has_text=survey_name)
    expect(card).to_have_count(1, timeout=30_000)
    survey_id = (card.get_attribute("id") or "").removeprefix("survey-card-")
    records.survey_ids.append(survey_id)

    deadline = time.monotonic() + RUN_TIMEOUT_S
    status = "queued"
    while time.monotonic() < deadline:
        page.goto(f"/survey/results/{survey_id}")
        if page.locator(
            '[data-testid="survey-report-none"], #report-generate-btn'
        ).count():
            status = "completed"
            break
        if page.locator(".bg-red-50").count():
            status = "error"
            break
        time.sleep(POLL_INTERVAL_S)
    assert status == "completed", f"survey ended as {status!r}"

    # every image of the snapshot is shown, signed by the app, and loads
    for image_name in IMAGE_NAMES:
        expect(page.locator(f'img[alt="{image_name}"]')).to_have_count(1)
    loaded = page.evaluate(
        """() => [...document.querySelectorAll('img[alt^="広告"]')]
            .map((img) => img.complete && img.naturalWidth > 0)"""
    )
    assert loaded == [True] * len(IMAGE_NAMES)
    expect(page.locator('[data-testid="survey-failed-count"]')).to_have_count(0)
