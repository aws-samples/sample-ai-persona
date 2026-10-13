"""Full screens under axe-core: M11 and the four list screens (NFR6.2, NFR6.11, NFR6.12, WF8, WF9)."""

from datetime import datetime
from typing import Any, Generator
from unittest.mock import Mock, patch

import pytest

from src.models.discussion import Discussion
from src.models.persona import Persona
from src.models.survey import Survey
from src.models.survey_template import Question, SurveyTemplate
from tests.ui.conftest import UiHarness
from web.routers._pagination import encode_cursor

NOW = datetime(2026, 10, 1, 9, 30)
NEXT = {"id": "next-page"}


def _personas() -> list[Persona]:
    names = [
        ("佐藤 太郎", "会社員"),
        ("鈴木 花子", "デザイナー"),
        ("高橋 次郎", "学生"),
    ]
    return [
        Persona.create_new(
            name=name,
            age=30 + i,
            occupation=job,
            background="東京で働いている。休日は家族と過ごすことが多い。",
            values=["効率性", "品質"],
            pain_points=["時間不足"],
            goals=["スキル向上"],
            tags=["都市部"],
        )
        for i, (name, job) in enumerate(names)
    ]


def _discussions(personas: list[Persona]) -> list[Discussion]:
    topics = ["新商品の価格について", "店舗の使いやすさについて"]
    return [
        Discussion.create_new(
            topic=topic,
            participants=[p.id for p in personas[:2]] + ["unknown-persona-id"],
        )
        for topic in topics
    ]


def _surveys() -> list[Survey]:
    statuses = ["completed", "running", "error", "pending"]
    return [
        Survey(
            id=f"s{i}",
            name=f"満足度調査 {i}",
            description="来店者の満足度",
            template_id="t1",
            persona_count=100,
            filters=None,
            status=status,
            s3_result_path=None,
            insight_report=None,
            created_at=NOW,
            updated_at=NOW,
            datasource="nemotron",
        )
        for i, status in enumerate(statuses)
    ]


def _templates() -> list[SurveyTemplate]:
    questions = [
        Question(id="q1", text="満足度を教えてください", question_type="scale_rating"),
        Question(
            id="q2",
            text="よく使う機能",
            question_type="multiple_choice",
            options=["A", "B"],
        ),
        Question(id="q3", text="ご意見", question_type="free_text"),
    ]
    return [
        SurveyTemplate(
            id="t1",
            name="満足度テンプレート",
            questions=questions,
            created_at=NOW,
            updated_at=NOW,
        )
    ]


@pytest.fixture
def screens() -> Generator[dict[str, Mock]]:
    """Patch the Managers behind the four list screens with a few rows each."""
    personas = _personas()
    persona_manager = Mock()
    persona_manager.get_all_personas.return_value = (personas, NEXT)
    persona_manager.get_persona_count.return_value = 30
    persona_manager.get_persona.side_effect = lambda pid: next(
        (p for p in personas if p.id == pid), None
    )
    discussion_manager = Mock()
    discussion_manager.get_discussion_history.return_value = (
        _discussions(personas),
        NEXT,
    )
    execution_manager = Mock()
    execution_manager.get_all_surveys.return_value = _surveys()
    template_manager = Mock()
    template_manager.get_all_templates.return_value = _templates()
    managers = {
        "web.routers.persona.get_persona_manager": persona_manager,
        "web.routers.discussion.get_persona_manager": persona_manager,
        "web.routers.discussion.get_discussion_manager": discussion_manager,
        "web.routers.survey.get_execution_manager": execution_manager,
        "web.routers.survey.get_template_manager": template_manager,
    }
    patchers = [
        patch(target, return_value=manager) for target, manager in managers.items()
    ]
    for patcher in patchers:
        patcher.start()
    yield managers
    for patcher in patchers:
        patcher.stop()


def _open(ui: UiHarness, client: Any, path: str) -> None:
    if path == "/persona/management":
        ui.respond(
            "/persona/list/partial", body=client.get("/persona/list/partial").text
        )
    ui.load_screen(client, path)
    if path == "/persona/management":
        ui.page.wait_for_selector("#persona-grid")
    ui.settle()


LIST_SCREENS = [
    "/persona/management",
    "/discussion/results",
    "/survey/results",
    "/survey/templates",
]


def test_not_found_page_has_no_axe_violations(
    ui: UiHarness, screen_client: Any
) -> None:
    ui.load_screen(screen_client, "/no-such-page")

    assert ui.page.title() == "ページが見つかりません - AIペルソナシステム"
    ui.check_a11y()


@pytest.mark.parametrize("path", LIST_SCREENS)
def test_list_screen_has_no_axe_violations(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock], path: str
) -> None:
    _open(ui, screen_client, path)

    assert ui.page.locator("h1").count() == 1
    assert ui.page.locator("main h2").count() >= 1
    ui.check_a11y()


@pytest.mark.parametrize(
    ("path", "selector", "expected"),
    [
        (
            "/survey/results",
            "[data-testid='survey-status']",
            ["完了", "実行中", "エラー", "待機中"],
        ),
        (
            "/discussion/results",
            "[data-testid='discussion-mode']",
            ["簡易議論", "簡易議論"],
        ),
    ],
)
def test_status_badges_carry_text(
    ui: UiHarness,
    screen_client: Any,
    screens: dict[str, Mock],
    path: str,
    selector: str,
    expected: list[str],
) -> None:
    _open(ui, screen_client, path)

    assert [t.strip() for t in ui.page.locator(selector).all_inner_texts()] == expected


def test_icon_only_delete_buttons_are_named(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock]
) -> None:
    _open(ui, screen_client, "/survey/results")

    names = ui.page.locator("[data-testid='survey-delete-button']").evaluate_all(
        "(buttons) => buttons.map((b) => b.getAttribute('aria-label'))"
    )
    assert names[0] == "アンケート「満足度調査 0」を削除"


@pytest.mark.parametrize(
    ("path", "more_url", "grid"),
    [
        (
            "/persona/management",
            f"/persona/list/partial?cursor={encode_cursor(NEXT)}&append=1",
            "#persona-grid",
        ),
        (
            "/discussion/results",
            f"/discussion/results?cursor={encode_cursor(NEXT)}&sort=newest&append=1",
            "#discussion-grid",
        ),
    ],
)
def test_load_more_marks_the_list_busy(
    ui: UiHarness,
    screen_client: Any,
    screens: dict[str, Mock],
    path: str,
    more_url: str,
    grid: str,
) -> None:
    held = ui.hold(more_url)
    _open(ui, screen_client, path)

    ui.page.click("button:has-text('もっと見る')")
    held.wait_for_request()
    assert ui.page.get_attribute(grid, "aria-busy") == "true"

    held.release(body="")
    ui.settle()
    assert ui.page.get_attribute(grid, "aria-busy") is None


@pytest.mark.parametrize(
    ("path", "list_id"),
    [
        ("/persona/management", "persona-list"),
        ("/discussion/results", "discussion-list"),
        ("/survey/results", "survey-list"),
        ("/survey/templates", "template-list"),
    ],
)
def test_list_part_is_wrapped_in_an_id(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock], path: str, list_id: str
) -> None:
    _open(ui, screen_client, path)

    assert ui.page.locator(f"main #{list_id}").count() == 1
