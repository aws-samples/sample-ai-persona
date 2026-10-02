"""§3.4 インタビューモード / §5 履歴 regression flow."""

from playwright.sync_api import Page, expect

from tests.e2e.conftest import LLM_TIMEOUT_MS, CreatedRecords, discussion_id_from_url


def test_interview_chat_save_and_delete(
    page: Page,
    generated_personas: list[dict[str, str]],
    created: CreatedRecords,
    run_tag: str,
) -> None:
    page.goto("/discussion/setup")
    page.locator('label:has(input[name="mode"][value="interview"])').click()
    page.locator(f"#persona-card-{generated_personas[0]['id']}").click()
    page.locator('button:has-text("インタビューを開始")').click()
    page.wait_for_url("**/interview/chat/**")

    persona_messages = page.locator("#chat-messages .persona-message")
    before = persona_messages.count()
    question = "普段の買い物で一番困っていることは何ですか？"
    page.locator("#message-input").fill(question)
    page.locator("#send-button").click()

    expect(page.locator("#chat-messages .user-message").last).to_contain_text(question)
    expect(persona_messages.nth(before)).to_be_visible(timeout=LLM_TIMEOUT_MS)
    expect(page.locator("#send-button")).to_be_enabled(timeout=LLM_TIMEOUT_MS)

    session_name = f"{run_tag} インタビュー"
    page.locator("#save-session-btn").click()
    page.locator("#session-name-input").fill(session_name)
    page.locator("#confirm-save-btn").click()
    page.wait_for_url("**/discussion/*", timeout=30_000)
    discussion_id = discussion_id_from_url(page.url)
    created.discussion_ids.append(discussion_id)
    expect(page.get_by_text(question).first).to_be_visible()

    page.goto(f"/discussion/results?mode=interview&search={run_tag}")
    expect(page.locator(f"#discussion-card-{discussion_id}")).to_be_visible()

    page.goto(f"/discussion/{discussion_id}")
    page.locator(f'button[hx-delete="/discussion/{discussion_id}"]').click()
    page.wait_for_url("**/discussion/results")
    created.discussion_ids.remove(discussion_id)
    expect(page.locator(f"#discussion-card-{discussion_id}")).to_have_count(0)
