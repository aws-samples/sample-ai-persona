"""Discussion and interview screens of U6 (marker ``ui``, Playwright + axe-core).

M9  参照権限なし・削除済みの表示（議論の詳細）
M12 会話を続けられなくなったとき（議論の設定画面・インタビュー）
M13 参加するペルソナの選択（タブ・選択の保持・〔共有〕・記憶と利用権限の注意）

BR2.1、BR5.3、BR7.3〜BR7.5、NFR1.60、NFR6.17〜NFR6.19、AC4.2.1〜AC4.2.3、
AC4.3.1〜AC4.3.3。
"""

import json
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import Mock, patch

import pytest

from src.models.discussion import Discussion, DiscussionView, ParticipantView
from src.models.interview_session import InterviewSession, InterviewView
from src.models.message import Message
from src.models.persona import Persona
from src.models.sharing import ListTab, TabbedList
from tests.ui.conftest import UiHarness, render_page

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)


def _persona(pid: str, name: str, owner: str = "me") -> Persona:
    return Persona(
        id=pid,
        name=name,
        age=30,
        occupation="会社員",
        background="東京で働いている。",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
        created_at=NOW,
        updated_at=NOW,
        owner_sub=owner,
    )


SATO = _persona("p1", "佐藤 太郎")
ITO = _persona("p2", "伊藤 さくら", owner="other")


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


SSE_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}


# =============================================================================
# M9: 議論の詳細の参加者の状態
# =============================================================================


def _view(manageable: bool) -> DiscussionView:
    discussion = Discussion.create_new(
        topic="新商品の価格について",
        participants=["p1", "p2", "p3"],
        mode="classic",
        participant_names={"p1": "佐藤 太郎", "p2": "鈴木 一郎", "p3": "田中 美咲"},
    )
    messages = [
        Message.create_new("p1", "佐藤 太郎", "3,000 円なら買います"),
        Message.create_new("p2", "鈴木 一郎", "価格は 3,000 円以下でないと..."),
    ]
    participants = [
        ParticipantView("p1", "佐藤 太郎", "readable"),
        ParticipantView("p2", "鈴木 一郎", "no_access"),
        ParticipantView("p3", "田中 美咲", "deleted"),
    ]
    return DiscussionView(discussion, messages, [], participants, manageable)


@pytest.fixture
def detail_manager() -> Generator[Mock]:
    manager = Mock()
    manager.get_default_categories.return_value = [
        {"name": "顧客ニーズ", "description": "ニーズ"}
    ]
    manager.get_document_urls.return_value = {}
    with patch("web.routers.discussion.get_discussion_manager", return_value=manager):
        yield manager


@pytest.mark.parametrize("manageable", [True, False], ids=["owner", "shared"])
def test_detail_marks_participants_in_text_and_has_no_axe_violations(
    ui: UiHarness, screen_client: Any, detail_manager: Mock, manageable: bool
) -> None:
    view = _view(manageable)
    detail_manager.get_discussion.return_value = view

    ui.load_screen(screen_client, f"/discussion/{view.id}")
    ui.settle()

    participants = ui.page.locator("[data-testid='discussion-participant']")
    texts = [t.replace("\n", " ") for t in participants.all_inner_texts()]
    assert "〔参照権限なし〕" in texts[1] and "〔削除済み〕" in texts[2]
    assert ui.page.locator("a[href='/persona/p1']").count() == 1
    assert ui.page.locator("a[href='/persona/p2']").count() == 0
    assert ui.page.locator("a[href='/persona/p3']").count() == 0
    # 発言の話者にも同じ文字を付ける（M9）
    speaker = ui.page.locator("[data-testid='message-speaker']", has_text="鈴木 一郎")
    assert "〔参照権限なし〕" in speaker.inner_text()
    assert (
        ui.page.locator("[data-testid='discussion-share-button']").count() == 1
    ) is (manageable)
    ui.check_a11y()


# =============================================================================
# M13・M12: 議論の設定画面
# =============================================================================


def _selectable(tab: str, personas: list[Persona], owners: dict[str, str]) -> str:
    from web.routers import persona as persona_router

    return render_page(
        "persona/partials/persona_list.html",
        persona_avatar_url=persona_router.templates.env.globals["persona_avatar_url"],
        listing=TabbedList(
            tab=ListTab(tab),
            items=personas,
            counts={"mine": 1, "shared": 1},
            owners=owners,
        ),
        selectable=True,
        search="",
        is_append=False,
    )


def _open_setup(ui: UiHarness, client: Any) -> None:
    ui.respond(
        "/persona/list/partial?selectable=1&tab=mine",
        body=_selectable("mine", [SATO], {}),
    )
    ui.respond(
        "/persona/list/partial?selectable=1&tab=shared",
        body=_selectable("shared", [ITO], {"p2": "中村 由美"}),
    )
    ui.load_screen(client, "/discussion/setup")
    ui.page.wait_for_selector("#persona-card-p1")
    ui.settle()


def _choose_both(ui: UiHarness) -> None:
    ui.page.click("#persona-card-p1")
    ui.page.focus("[data-testid='persona-select-tab-shared']")
    ui.page.keyboard.press("Enter")
    ui.page.wait_for_selector("#persona-card-p2")
    ui.page.click("#persona-card-p2")
    ui.settle()


def test_selection_tabs_keep_the_selection_and_mark_shared(
    ui: UiHarness, screen_client: Any
) -> None:
    _open_setup(ui, screen_client)
    _choose_both(ui)

    card = ui.page.locator("#persona-card-p2").inner_text()
    assert "〔共有〕" in card and "中村 由美" in card
    selected = ui.page.locator("[data-testid='selected-personas']").inner_text()
    assert "佐藤 太郎" in selected and "伊藤 さくら" in selected
    assert ui.page.locator("[data-testid='shared-memory-note']").is_visible()
    assert ui.page.locator("[data-testid='unusable-assets-note']").is_visible()

    # 自分のものだけに戻すと記憶の注意は消える
    ui.page.locator("[data-testid='selected-persona-remove-p2']").click()
    ui.settle()
    assert not ui.page.locator("[data-testid='shared-memory-note']").is_visible()
    ui.check_a11y()


def test_a_participant_gone_while_running_shows_the_stop_panel(
    ui: UiHarness, screen_client: Any
) -> None:
    _open_setup(ui, screen_client)
    _choose_both(ui)
    ui.respond(
        "/discussion/stream",
        body=_sse(
            {"type": "participants", "personas": [], "mode": "traditional"},
            {"type": "stopped"},
        ),
        headers=SSE_HEADERS,
    )
    ui.page.fill("#topic", "新商品のマーケティングについて")
    ui.page.click("button:has-text('リアルタイム議論を開始')")

    panel = ui.page.wait_for_selector("[data-conversation-stopped]")
    assert panel.get_attribute("role") == "alert"
    assert ui.active(
        "document.activeElement.hasAttribute('data-conversation-stopped-heading')"
    )
    assert ui.page.input_value("#topic") == "新商品のマーケティングについて"
    ui.check_a11y()


def test_documents_list_is_cleared_after_the_discussion_is_saved(
    ui: UiHarness, screen_client: Any
) -> None:
    _open_setup(ui, screen_client)
    _choose_both(ui)
    ui.page.evaluate(
        """() => {
            const el = document.querySelector('.discussion-layout');
            Alpine.$data(el).uploadedDocuments = [
                {file_id: 'f1', filename: '資料.pdf', file_size: 10, mime_type: 'application/pdf'}
            ];
        }"""
    )
    ui.respond(
        "/discussion/stream",
        body=_sse(
            {
                "type": "complete",
                "discussion_id": "d1",
                "message_count": 2,
                "insight_count": 1,
            }
        ),
        headers=SSE_HEADERS,
    )
    ui.respond("/discussion/result-partial/d1", body="<div id='result'>結果</div>")
    ui.page.fill("#topic", "新商品のマーケティングについて")
    ui.page.click("button:has-text('リアルタイム議論を開始')")
    ui.page.wait_for_selector("#result")
    ui.settle()

    count = ui.page.evaluate(
        "() => Alpine.$data(document.querySelector('.discussion-layout')).uploadedDocuments.length"
    )
    assert count == 0
    assert ui.page.locator("[data-testid='documents-reupload-note']").is_visible()


# =============================================================================
# M12: インタビュー
# =============================================================================


def _interview_view() -> InterviewView:
    session = InterviewSession.create_new(
        owner_sub="me",
        persona_ids=["p1"],
        persona_names={"p1": "佐藤 太郎"},
        memory_mode="disabled",
        now=NOW,
    )
    return InterviewView(
        session=session,
        messages=[Message.create_new("user", "ユーザー", "前の質問", "user_message")],
    )


@pytest.fixture
def chat(screen_client: Any) -> Generator[InterviewView]:
    view = _interview_view()
    interviews = Mock()
    interviews.get_interview_session.return_value = view
    personas = Mock()
    personas.get_personas_for_use.return_value = [SATO]
    with (
        patch("web.routers.interview.get_interview_manager", return_value=interviews),
        patch("web.routers.interview.get_persona_manager", return_value=personas),
    ):
        yield view


def test_chat_stops_and_keeps_the_typed_message(
    ui: UiHarness, screen_client: Any, chat: InterviewView
) -> None:
    ui.respond(
        f"/interview/{chat.id}/message/stream",
        body=_sse(
            {
                "type": "error",
                "message": "ページが見つかりません",
                "error_type": "conversation_stopped",
            }
        ),
        headers=SSE_HEADERS,
    )
    ui.load_screen(screen_client, f"/interview/chat/{chat.id}")
    ui.page.fill("#message-input", "価格は 5,000 円でも買いますか？")
    ui.page.click("#send-button")

    panel = ui.page.wait_for_selector("[data-conversation-stopped]")
    assert panel.get_attribute("role") == "alert"
    ui.settle()
    assert ui.page.input_value("#message-input") == "価格は 5,000 円でも買いますか？"
    assert ui.page.is_disabled("#send-button")
    assert ui.page.is_disabled("#save-session-btn")
    assert ui.active(
        "document.activeElement.hasAttribute('data-conversation-stopped-heading')"
    )
    ui.check_a11y()


def test_save_is_disabled_while_a_reply_is_in_progress(
    ui: UiHarness, screen_client: Any, chat: InterviewView
) -> None:
    held = ui.hold(f"/interview/{chat.id}/message/stream")
    ui.load_screen(screen_client, f"/interview/chat/{chat.id}")
    ui.page.fill("#message-input", "こんにちは")
    ui.page.click("#send-button")
    held.wait_for_request()
    assert ui.page.is_disabled("#save-session-btn")

    held.release(body=_sse({"type": "complete"}), headers=SSE_HEADERS)
    ui.page.wait_for_function(
        "() => !document.getElementById('save-session-btn').disabled"
    )


def test_a_chat_that_cannot_continue_is_shown_stopped(
    ui: UiHarness, screen_client: Any, chat: InterviewView
) -> None:
    from src.models.errors import ParticipantNotFoundError

    with patch(
        "web.routers.interview.get_persona_manager",
        return_value=Mock(
            get_personas_for_use=Mock(side_effect=ParticipantNotFoundError("gone"))
        ),
    ):
        ui.load_screen(screen_client, f"/interview/chat/{chat.id}")
    ui.settle()

    assert ui.page.locator("[data-conversation-stopped]").count() == 1
    assert "前の質問" in ui.page.locator("#chat-messages").inner_text()
    assert ui.page.is_disabled("#send-button")
    assert ui.page.is_disabled("#save-session-btn")
    ui.check_a11y()
