"""§3.2 簡易議論 / §3.3 しっかり議論 / §4 議論結果の確認と保存 / §5 履歴 regression flows."""

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.conftest import (
    LLM_TIMEOUT_MS,
    CreatedRecords,
    start_discussion,
)


def _run_discussion(
    session_context: BrowserContext,
    created: CreatedRecords,
    personas: list[dict[str, str]],
    topic: str,
    **kwargs: object,
) -> dict[str, str]:
    page = session_context.new_page()
    discussion_id = start_discussion(
        page,
        persona_ids=[p["id"] for p in personas],
        topic=topic,
        **kwargs,  # type: ignore[arg-type]
    )
    created.discussion_ids.append(discussion_id)
    page.close()
    return {"id": discussion_id, "topic": topic}


@pytest.fixture(scope="module")
def classic_discussion(
    session_context: BrowserContext,
    generated_personas: list[dict[str, str]],
    created: CreatedRecords,
    run_tag: str,
) -> dict[str, str]:
    """簡易議論 with 2 personas, normal display (htmx POST /discussion/start)."""
    return _run_discussion(
        session_context,
        created,
        generated_personas[:2],
        f"{run_tag} 定期購入サービスの解約しやすさについて",
        mode="traditional",
        streaming=False,
    )


def _expect_persona_messages(page: Page, persona_ids: list[str]) -> None:
    messages = page.locator(".discussion-messages .message-item")
    expect(messages.first).to_be_visible()
    for persona_id in persona_ids:
        expect(
            messages.locator(f'[data-avatar-seed="{persona_id}"]').first
        ).to_be_attached()


def test_classic_discussion_messages_and_insights(
    page: Page,
    classic_discussion: dict[str, str],
    generated_personas: list[dict[str, str]],
) -> None:
    page.goto(f"/discussion/{classic_discussion['id']}")
    expect(page.get_by_text(classic_discussion["topic"]).first).to_be_visible()
    _expect_persona_messages(page, [p["id"] for p in generated_personas[:2]])

    page.locator('button:has-text("インサイト")').first.click()
    expect(page.locator("#insights-container")).to_contain_text("%")


def test_classic_discussion_streaming(
    session_context: BrowserContext,
    page: Page,
    generated_personas: list[dict[str, str]],
    created: CreatedRecords,
    run_tag: str,
) -> None:
    """簡易議論 with 「リアルタイム表示」 (SSE /discussion/stream)."""
    personas = generated_personas[:2]
    discussion = _run_discussion(
        session_context,
        created,
        personas,
        f"{run_tag} 店舗とECの使い分けについて",
        mode="traditional",
        streaming=True,
    )
    page.goto(f"/discussion/{discussion['id']}")
    _expect_persona_messages(page, [p["id"] for p in personas])


def test_agent_discussion_one_round(
    session_context: BrowserContext,
    page: Page,
    generated_personas: list[dict[str, str]],
    created: CreatedRecords,
    run_tag: str,
) -> None:
    """しっかり議論 (1 round) with 「リアルタイム表示」, the primary path.

    Agent mode streams message_start / message_delta / message_end per turn
    (unlike 簡易議論's whole ``message`` events), so the live stage and the
    conversation log are checked while the stream is running.
    """
    personas = generated_personas[:2]

    def check_live_stream(live: Page) -> None:
        # message_start / message_delta: the active speaker bubble on the stage
        expect(live.locator("#stage-active .active-content")).not_to_be_empty()
        log = live.locator("#streaming-messages")
        # message_end: each persona's turn is committed to the log with content
        for persona in personas:
            turn = log.locator(".message-item").filter(has_text=persona["name"])
            expect(turn.locator(".streaming-content").first).not_to_be_empty(
                timeout=LLM_TIMEOUT_MS
            )
        expect(log.locator(".facilitator-bubble").first).to_be_attached(
            timeout=LLM_TIMEOUT_MS
        )
        expect(log).not_to_contain_text("[応答を取得できませんでした]")

    discussion = _run_discussion(
        session_context,
        created,
        personas,
        f"{run_tag} 問い合わせ窓口に求めることについて",
        mode="agent",
        streaming=True,
        rounds=1,
        on_stream=check_live_stream,
    )
    page.goto(f"/discussion/{discussion['id']}")
    _expect_persona_messages(page, [p["id"] for p in personas])
    expect(
        page.locator(".discussion-messages .facilitator-bubble").first
    ).to_be_visible()

    page.goto(f"/discussion/results?mode=agent&search={run_tag}")
    card = page.locator(f"#discussion-card-{discussion['id']}")
    expect(card).to_contain_text("しっかり議論")


def test_report_generate_save_export_delete(
    page: Page, classic_discussion: dict[str, str]
) -> None:
    discussion_id = classic_discussion["id"]
    page.goto(f"/discussion/{discussion_id}")
    page.locator('button:has-text("レポート")').first.click()

    page.locator('#report-generator button:has-text("サマリ")').click()
    page.locator("#generate-report-btn").click()
    save_btn = page.locator(
        '#report-display button[type="submit"]:has-text("保存する")'
    )
    expect(save_btn).to_be_visible(timeout=LLM_TIMEOUT_MS)
    save_btn.click()

    saved = page.locator(
        f'#reports-container [hx-get^="/discussion/{discussion_id}/report/"]'
    )
    expect(saved).to_have_count(1)
    report_id = (saved.get_attribute("hx-get") or "").rsplit("/", 1)[-1]
    report_url = f"/discussion/{discussion_id}/report/{report_id}"

    for fmt in ("md", "txt"):
        page.locator('#reports-container button[title="エクスポート"]').click()
        with page.expect_download() as download_info:
            page.locator(f'a[href="{report_url}/export?format={fmt}"]').click()
        download = download_info.value
        assert download.suggested_filename.endswith(f".{fmt}")
        assert download.path().read_text(encoding="utf-8").strip()

    page.locator(f'#reports-container button[hx-delete="{report_url}"]').click()
    expect(page.locator("#reports-container")).to_contain_text("レポートを削除しました")

    page.reload()
    page.locator('button:has-text("レポート")').first.click()
    expect(page.locator(f'#reports-container [hx-get="{report_url}"]')).to_have_count(0)


def test_history_search_and_delete(
    page: Page,
    classic_discussion: dict[str, str],
    created: CreatedRecords,
    run_tag: str,
) -> None:
    discussion_id = classic_discussion["id"]
    page.goto(f"/discussion/results?mode=classic&search={run_tag}")
    card = page.locator(f"#discussion-card-{discussion_id}")
    expect(card).to_be_visible()
    expect(card).to_contain_text("簡易議論")

    card.locator(f'button[hx-delete="/discussion/{discussion_id}"]').click()
    expect(card).to_have_count(0)
    created.discussion_ids.remove(discussion_id)
