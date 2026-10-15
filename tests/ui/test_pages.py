"""Full screens under axe-core: M11 and the four list screens (NFR6.2, NFR6.11, NFR6.12, WF8, WF9)."""

from datetime import datetime
from typing import Any, Generator
from unittest.mock import Mock, patch

import pytest

from src.models.discussion import Discussion
from src.models.persona import Persona
from src.models.sharing import ListTab, TabbedList
from src.models.survey import Survey
from src.models.survey_template import Question, SurveyTemplate
from tests.ui.conftest import UiHarness

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


#: The cursor the persona list hands to 「もっと見る」 (opaque to the screen).
PERSONA_NEXT = "persona-next"


def _persona_listing(personas: list[Persona], tab: str | None) -> TabbedList:
    """What PersonaManager.list_personas returns for the admin of screen_client."""
    active = ListTab.parse(tab) or ListTab.MINE
    owners = (
        {}
        if active is ListTab.MINE
        else {
            p.id: name for p, name in zip(personas, ["中村 由美", "所有者不明", None])
        }
    )
    shared = {
        p.id: names
        for p, names in zip(personas, [["企画部"], [], ["企画部", "マーケティング部"]])
    }
    return TabbedList(
        tab=active,
        items=personas,
        next_cursor=PERSONA_NEXT,
        counts={"mine": 30, "shared": 2, "all": 48},
        show_all=True,
        owners=owners,
        shared_with=shared,
        manageable=frozenset()
        if active is ListTab.SHARED
        else frozenset(p.id for p in personas),
    )


def _discussions(personas: list[Persona]) -> list[Discussion]:
    topics = ["新商品の価格について", "店舗の使いやすさについて"]
    return [
        Discussion.create_new(
            topic=topic,
            participants=[p.id for p in personas[:2]],
            participant_names={p.id: p.name for p in personas[:2]},
        )
        for topic in topics
    ]


#: The cursor the discussion list hands to 「もっと見る」.
DISCUSSION_NEXT = "discussion-next"


def _discussion_listing(discussions: list[Discussion], tab: str | None) -> TabbedList:
    """What DiscussionManager.list_discussions returns for the admin (U6, M6)."""
    active = ListTab.parse(tab) or ListTab.MINE
    return TabbedList(
        tab=active,
        items=discussions,
        next_cursor=DISCUSSION_NEXT,
        counts={"mine": 12, "shared": 1, "all": 20},
        show_all=True,
        owners={}
        if active is ListTab.MINE
        else {d.id: "中村 由美" for d in discussions},
        shared_with={discussions[0].id: ["企画部"]},
        manageable=frozenset()
        if active is ListTab.SHARED
        else frozenset(d.id for d in discussions),
    )


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
    persona_manager.list_personas.side_effect = lambda _p, tab, _c: _persona_listing(
        personas, tab
    )
    discussions = _discussions(personas)
    discussion_manager = Mock()
    # U6: 議論の一覧もタブ別。参加者は保存したときの名前（participant_names）で出す
    discussion_manager.list_discussions.side_effect = (
        lambda _p, tab, _c, *_a: _discussion_listing(discussions, tab)
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
    # ペルソナ管理は最初のページをタブと一緒にサーバーで描く（U4）
    ui.load_screen(client, path)
    if path.startswith("/persona/management"):
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


@pytest.mark.parametrize("tab", ["mine", "shared", "all"])
def test_discussion_tabs_have_no_axe_violations(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock], tab: str
) -> None:
    """議論・インタビュー結果の 3 つのタブ（U6、M6、BR2.1）。"""
    _open(ui, screen_client, f"/discussion/results?tab={tab}")

    selected = ui.page.locator("[role=tab][aria-selected=true]")
    assert selected.get_attribute("data-tab") == tab
    assert (ui.page.locator("[data-testid='bulk-select-bar']").count() == 0) is (
        tab == "shared"
    )
    assert (
        ui.page.locator("[data-testid='discussion-share-button']").count() == 0
    ) is (tab == "shared")
    ui.check_a11y()


@pytest.mark.parametrize("tab", ["mine", "shared", "all"])
def test_persona_tabs_have_no_axe_violations(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock], tab: str
) -> None:
    """ペルソナ管理の 3 つのタブ（U4、M6、NFR6.13）。"""
    _open(ui, screen_client, f"/persona/management?tab={tab}")

    selected = ui.page.locator("[role=tab][aria-selected=true]")
    assert selected.get_attribute("data-tab") == tab
    assert (ui.page.locator("[data-testid='bulk-select-bar']").count() == 0) is (
        tab == "shared"
    )
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
            f"/persona/list/partial?tab=mine&cursor={PERSONA_NEXT}&append=1",
            "#persona-grid",
        ),
        (
            "/discussion/results",
            f"/discussion/results?tab=mine&cursor={DISCUSSION_NEXT}&append=1",
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


@pytest.mark.parametrize("manageable", [True, False], ids=["owner", "shared"])
def test_persona_detail_has_no_axe_violations(
    ui: UiHarness, screen_client: Any, screens: dict[str, Mock], manageable: bool
) -> None:
    """ペルソナの詳細（U5、M8）: 所有者と共有された人の見え方で違反 0 件。"""
    from dataclasses import replace

    from src.managers.persona_manager import (
        BindingRow,
        DatasetBindingsView,
        KbBindingView,
        PersonaDetail,
    )
    from tests.ui.conftest import render_page

    persona = replace(_personas()[0], id="p1")
    manager = screens["web.routers.persona.get_persona_manager"]
    manager.get_persona_detail.side_effect = lambda _p, _pid: PersonaDetail(
        persona,
        manageable=manageable,
        owner_label="自分" if manageable else "中村 由美",
        shared_with=["企画部"],
    )
    manager.avatar_upload_enabled.return_value = True
    rows = [
        BindingRow(target_id="d1", name="行動データ", usable=True, dedicated=True),
        BindingRow(target_id="d2", name="購買データ", usable=False),
    ]
    ui.respond(
        "/persona/p1/dataset-bindings",
        body=render_page(
            "persona/partials/dataset_bindings.html",
            persona_id="p1",
            view=DatasetBindingsView("p1", manageable, rows),
        ),
    )
    ui.respond(
        "/persona/p1/kb-binding",
        body=render_page(
            "persona/partials/kb_binding.html",
            persona_id="p1",
            view=KbBindingView("p1", manageable, None),
        ),
    )

    ui.load_screen(screen_client, "/persona/p1")
    ui.page.wait_for_selector("[data-testid='dataset-binding-row']", state="attached")
    ui.settle()

    assert ui.page.locator("h1").count() == 1
    assert (ui.page.locator("#memory-section-content").count() == 1) is manageable
    ui.check_a11y()
