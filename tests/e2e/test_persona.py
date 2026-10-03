"""§1 ペルソナ生成 / §2 ペルソナ管理 regression flows."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import DATA_DIR, LLM_TIMEOUT_MS, CreatedRecords

IMAGE_FILE = DATA_DIR.parent.parent / "test_file" / "test_image.jpeg"


def _search_management(page: Page, query: str) -> None:
    page.goto("/persona/management")
    page.wait_for_load_state("networkidle")
    search = page.locator("#search-input")
    search.fill(query)
    # The search fires on keyup (fill() sends none), so press a key to trigger it.
    with page.expect_response(lambda r: "/persona/list/partial?search=" in r.url):
        search.press("End")


def _card_ids_for_name(page: Page, name: str) -> set[str]:
    """IDs of management cards whose name matches exactly."""
    _search_management(page, name)
    cards = page.locator(f'#persona-grid [data-persona-name="{name}"]')
    return {
        (cards.nth(i).get_attribute("id") or "").removeprefix("persona-card-")
        for i in range(cards.count())
    }


def test_generated_personas_listed_in_management(
    page: Page, generated_personas: list[dict[str, str]]
) -> None:
    for persona in generated_personas:
        _search_management(page, persona["name"])
        expect(page.locator(f"#persona-card-{persona['id']}")).to_be_visible()


def test_persona_detail_and_edit(
    page: Page, generated_personas: list[dict[str, str]], run_tag: str
) -> None:
    persona = generated_personas[0]
    page.goto(f"/persona/{persona['id']}")
    expect(page.locator("h1").first).to_have_text(persona["name"])

    page.locator(f'button[hx-get="/persona/{persona["id"]}/edit"]').click()
    new_name = f"{persona['name']} {run_tag}"
    page.locator("#name").fill(new_name)
    page.locator("#tags").fill(run_tag)
    page.locator("form[hx-put] button[type=submit]").click()

    expect(page.locator("h1").first).to_have_text(new_name)
    persona["name"] = new_name

    page.reload()
    expect(page.locator("h1").first).to_have_text(new_name)


def test_persona_delete_from_management(
    page: Page, generated_personas: list[dict[str, str]], created: CreatedRecords
) -> None:
    persona = generated_personas[2]
    _search_management(page, persona["name"])
    card = page.locator(f"#persona-card-{persona['id']}")
    expect(card).to_be_visible()

    card.locator(f'button[hx-delete="/persona/{persona["id"]}"]').click()
    expect(card).to_have_count(0)
    created.persona_ids.remove(persona["id"])

    _search_management(page, persona["name"])
    expect(page.locator(f"#persona-card-{persona['id']}")).to_have_count(0)


def test_single_persona_generate_and_save(page: Page, created: CreatedRecords) -> None:
    """Generation count 1 renders the single-persona view saved via /persona/save."""
    page.goto("/persona/generation")
    page.locator("#file-input").set_input_files(DATA_DIR / "interview_sample.txt")
    page.locator("#custom_prompt").fill("50代の自営業者のペルソナを作ってください")
    page.locator("#persona_count").fill("1")
    page.locator("#generate-btn").click()

    save_form = page.locator('#generation-result form[hx-post="/persona/save"]')
    expect(save_form).to_be_visible(timeout=LLM_TIMEOUT_MS)
    name = save_form.locator('input[name="name"]').input_value()
    # /persona/save issues a new ID (unlike /persona/save-selected), so the
    # saved persona is the card with this name that did not exist before.
    before = _card_ids_for_name(page.context.new_page(), name)

    save_form.locator('button[type="submit"]').click()
    expect(page.locator("#generation-result")).to_contain_text(
        f"ペルソナ「{name}」を保存しました"
    )
    new_ids = _card_ids_for_name(page, name) - before
    assert len(new_ids) == 1, f"expected one new persona named {name}: {new_ids}"
    persona_id = new_ids.pop()
    created.persona_ids.append(persona_id)

    page.goto(f"/persona/{persona_id}")
    expect(page.locator("h1").first).to_have_text(name)


def test_persona_avatar_upload_and_reset(
    page: Page, generated_personas: list[dict[str, str]]
) -> None:
    persona_id = generated_personas[1]["id"]
    page.goto(f"/persona/{persona_id}")
    file_input = page.locator(
        f'form[hx-post="/persona/{persona_id}/avatar"] input[name="file"]'
    )
    if file_input.count() == 0:
        pytest.skip("Avatar upload is disabled (S3_BUCKET_NAME is not set)")

    avatar = page.locator(".persona-detail-avatar").first
    file_input.set_input_files(IMAGE_FILE)
    expect(avatar).to_have_attribute(
        "data-avatar-url", re.compile(rf"^/persona/{persona_id}/avatar\?v=")
    )
    avatar_url = avatar.get_attribute("data-avatar-url") or ""
    response = page.request.get(avatar_url)
    assert response.ok
    assert response.headers["content-type"].startswith("image/")

    page.locator('button:has-text("自動アバターに戻す")').click()
    expect(page.locator(".persona-detail-avatar").first).to_have_attribute(
        "data-avatar-url", ""
    )
    expect(page.locator('button:has-text("自動アバターに戻す")')).to_have_count(0)
