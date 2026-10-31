"""Survey and organization-asset screens of U7 (marker ``ui``, Playwright + axe-core).

M6  テンプレート・結果の一覧のタブ（キーボードだけで切り替える）
M10 組織資産の利用範囲のダイアログ（部署の選択・外すときの注意・削除されたグループ・Escape）
    カスタムデータの取り込みの利用範囲の欄（既定は admin のみ）
    member のペルソナ元データの画面（管理の操作がない）

BR3.2、BR4.1、BR5.2、NFR6.20〜NFR6.22、AC5.3.3。
"""

from datetime import datetime
from typing import Any, Generator
from unittest.mock import Mock, patch

import pytest

from src.models.authz import Department, ResourceType
from src.models.custom_persona_data import CustomPersonaData, PersonaDataListing
from src.models.sharing import AssetScopeDialog, ListTab, TabbedList
from src.models.survey import Survey, TemplateSnapshot
from src.models.survey_template import Question, SurveyTemplate
from tests.ui.conftest import UiHarness, render_page

NOW = datetime(2026, 10, 15, 9, 30)
DEPARTMENTS = (Department("sales", "営業部"), Department("plan", "企画部"))


def _template(tid: str, name: str) -> SurveyTemplate:
    return SurveyTemplate(
        id=tid,
        name=name,
        questions=[Question(id="q1", text="満足度は？", question_type="scale_rating")],
        created_at=NOW,
        updated_at=NOW,
    )


MINE = _template("t1", "自分のテンプレート")
SHARED = _template("t2", "共有されたテンプレート")


def _survey(sid: str, name: str) -> Survey:
    return Survey(
        id=sid,
        name=name,
        description="",
        template_id="t1",
        persona_count=100,
        filters=None,
        status="completed",
        s3_result_path=None,
        insight_report=None,
        created_at=NOW,
        updated_at=NOW,
        datasource="nemotron",
        template_snapshot=TemplateSnapshot.from_template(MINE),
    )


def _listing(items: list, tab: ListTab) -> TabbedList:
    return TabbedList(
        tab=tab,
        items=items,
        counts={"mine": 1, "shared": 1},
        owners={} if tab is ListTab.MINE else {i.id: "中村 由美" for i in items},
        shared_with={},
        manageable=frozenset()
        if tab is ListTab.SHARED
        else frozenset(i.id for i in items),
    )


@pytest.fixture
def survey_lists() -> Generator[None]:
    templates = Mock()
    templates.list_templates.side_effect = lambda _p, tab, _c, *_a: _listing(
        [MINE], ListTab.MINE
    )
    executions = Mock()
    executions.list_surveys.side_effect = lambda _p, tab, _c, *_a: _listing(
        [_survey("s1", "自分の結果")], ListTab.MINE
    )
    with (
        patch("web.routers.survey.get_template_manager", return_value=templates),
        patch("web.routers.survey.get_execution_manager", return_value=executions),
    ):
        yield


# =============================================================================
# M6: 一覧のタブ（キーボードだけ）
# =============================================================================


@pytest.mark.parametrize(
    ("path", "panel", "partial", "shared_items", "shared_text"),
    [
        (
            "/survey/templates",
            "survey-template-list",
            "survey/partials/template_list.html",
            [SHARED],
            "共有されたテンプレート",
        ),
        (
            "/survey/results",
            "survey-list",
            "survey/partials/result_list.html",
            [_survey("s2", "共有された結果")],
            "共有された結果",
        ),
    ],
)
def test_tabs_switch_with_the_keyboard_only(
    ui: UiHarness,
    screen_client: Any,
    survey_lists: None,
    path: str,
    panel: str,
    partial: str,
    shared_items: list,
    shared_text: str,
) -> None:
    ui.respond(
        f"{path}?tab=shared",
        body=render_page(
            partial,
            listing=_listing(shared_items, ListTab.SHARED),
            current_search="",
            is_append=False,
        ),
    )
    ui.load_screen(screen_client, path)
    ui.settle()

    ui.page.focus(f"#{panel}-tab-mine")
    ui.page.keyboard.press("ArrowRight")
    assert ui.active() == f"{panel}-tab-shared"
    ui.page.keyboard.press("Enter")
    ui.page.wait_for_selector(f"#{panel} >> text={shared_text}")

    selected = ui.page.locator("[role=tab][aria-selected=true]")
    assert selected.get_attribute("data-tab") == "shared"
    # 共有されたものには変える操作を出さない
    assert ui.page.locator(f"#{panel} [data-dialog-open='share-dialog']").count() == 0
    ui.check_a11y()


# =============================================================================
# M10: 利用範囲のダイアログ
# =============================================================================

SCOPE_URL = "/settings/datasets/d1/scope"


def _scope_dialog(selected: frozenset[str], vanished: tuple[str, ...] = ()) -> str:
    return render_page(
        "settings/partials/asset_scope_dialog.html",
        scope=AssetScopeDialog(
            resource_type=ResourceType.DATASET,
            resource_id="d1",
            name="売上データ",
            choices=DEPARTMENTS,
            selected=selected | frozenset(vanished),
            vanished=vanished,
        ),
        dialog_id="asset-scope-dialog",
        save_url=SCOPE_URL,
    )


def _dataset_list(scope: list[str]) -> str:
    return render_page(
        "settings/partials/dataset_list.html",
        datasets=[
            {
                "id": "d1",
                "name": "売上データ",
                "description": "",
                "columns": [{"name": "a"}],
                "row_count": 3,
                "created_at": "2026-10-15T09:30:00",
            }
        ],
        dataset_scopes={"d1": scope},
    )


def _open_scope(ui: UiHarness, dialog_html: str) -> None:
    ui.respond(SCOPE_URL, body=dialog_html)
    ui.load_component("asset_scope.html", list_html=_dataset_list(["営業部"]))
    ui.page.click("[data-testid='dataset-scope-button']")
    ui.page.wait_for_selector("#asset-scope-dialog:not([hidden])", state="attached")
    ui.settle()


def _option(ui: UiHarness, name: str) -> Any:
    return ui.page.locator(
        f"#asset-scope-dialog [data-department-option][data-department-name='{name}']"
    )


def _warning(ui: UiHarness) -> Any:
    return ui.page.locator("#asset-scope-dialog [data-scope-remove-warning]")


def test_choose_departments_and_save(ui: UiHarness) -> None:
    from web.asset_scope_views import saved_response

    # サーバーが返すのと同じ通知（一覧の読み直しと、閉じる）
    saved = {"HX-Trigger": saved_response("#dataset-list").headers["HX-Trigger"]}
    ui.respond("/settings/datasets", body=_dataset_list(["営業部", "企画部"]))
    _open_scope(ui, _scope_dialog(frozenset({"sales"})))
    ui.check_a11y()

    _option(ui, "企画部").check()
    assert not _warning(ui).is_visible()
    # 保存（同じ URL への POST）は、閉じて一覧を読み直す応答にする
    ui.respond(SCOPE_URL, body="", headers=saved)
    ui.page.click("[data-testid='asset-scope-save']")
    ui.page.wait_for_selector("#asset-scope-dialog", state="detached")

    ui.page.wait_for_selector("#dataset-list >> text=営業部、企画部")
    assert ui.requests.count(SCOPE_URL) == 2
    ui.verify_requests()


def test_unchecking_an_initial_department_warns_before_saving(ui: UiHarness) -> None:
    _open_scope(ui, _scope_dialog(frozenset({"sales"})))
    assert not _warning(ui).is_visible()

    _option(ui, "営業部").uncheck()
    ui.settle()

    assert _warning(ui).is_visible()
    assert _warning(ui).get_attribute("role") == "alert"
    # 戻すと注意は消える
    _option(ui, "営業部").check()
    ui.settle()
    assert not _warning(ui).is_visible()
    ui.check_a11y()


def test_deleted_group_is_shown_and_can_be_removed(ui: UiHarness) -> None:
    _open_scope(ui, _scope_dialog(frozenset({"sales"}), vanished=("gone",)))

    vanished = _option(ui, "（削除されたグループ）")
    assert vanished.count() == 1 and vanished.is_checked()
    vanished.uncheck()
    ui.settle()

    assert _warning(ui).is_visible()
    form = ui.page.evaluate(
        """() => Array.from(new FormData(
            document.querySelector('[data-asset-scope-form]')).entries())"""
    )
    assert ["departments_initial", "gone"] in form
    assert ["departments", "gone"] not in form


def test_escape_closes_and_returns_focus(ui: UiHarness) -> None:
    _open_scope(ui, _scope_dialog(frozenset()))

    ui.page.keyboard.press("Escape")
    ui.settle()

    assert ui.page.locator("#asset-scope-dialog").count() == 0
    assert ui.active("document.activeElement.dataset.testid") == "dataset-scope-button"


# =============================================================================
# カスタムデータの取り込み（利用範囲の欄、既定は admin のみ）
# =============================================================================


def test_import_form_has_a_scope_field_defaulting_to_admin_only(ui: UiHarness) -> None:
    mapping = render_page(
        "survey/partials/column_mapping.html",
        filename="customers.csv",
        temp_key="persona-dataset/temp/x.csv",
        columns=["persona", "age"],
        samples={"persona": ["a"], "age": ["30"]},
        auto_mapping={"persona": "persona"},
        standard_columns={"persona": {"label": "ペルソナ", "required": True}},
        is_dwh=False,
        dataset_name="customers",
        suggested_extra=[],
        scope_choices=DEPARTMENTS,
    )
    # 実画面と同じく、カード（h2）の中にマッピングの見出し（h3）が来る
    ui.load_component(
        "fragment.html",
        container_id="custom-upload-result",
        fragment="<h2>カスタムペルソナデータの追加</h2>" + mapping,
    )
    ui.settle()

    field = ui.page.locator("[data-testid='custom-data-scope-field']")
    assert field.locator("input[name=departments]").count() == 2
    assert field.locator("input[name=departments]:checked").count() == 0
    assert "admin だけが使えます" in field.inner_text()
    assert (
        ui.page.locator("[data-testid='custom-data-name']").input_value() == "customers"
    )
    ui.check_a11y()


# =============================================================================
# 待ちと取れなかった件数の表示（frontend-components.md、FR3.3、FR5.3、BR2.5、
# BR4.4、NFR4.9b、FR4.1）。Jinja と画面の取得だけで確かめる（ブラウザは使わない）
# =============================================================================

BLOCKED_AT = datetime(2026, 10, 30, 1, 0)


def _with(survey: Survey, **fields: Any) -> Survey:
    from dataclasses import replace

    return replace(survey, **fields)


def _status_badges(*surveys: Survey) -> list[str]:
    import re

    html = render_page(
        "survey/partials/result_list.html",
        listing=_listing(list(surveys), ListTab.MINE),
        current_search="",
        is_append=False,
    )
    badges = re.findall(r'data-testid="survey-status"[^>]*>(.*?)</span>', html, re.S)
    return [" ".join(b.split()) for b in badges]


def test_list_tells_queued_blocked_and_old_waiting_apart():
    queued = _with(_survey("s1", "待ち"), status="queued")
    blocked = _with(
        _survey("s2", "止まった待ち"), status="queued", start_blocked_since=BLOCKED_AT
    )
    old = _with(_survey("s3", "旧記録"), status="pending")

    assert _status_badges(queued, blocked, old) == [
        "待ち",
        "開始できずに待っています",
        "待機中",
    ]


def test_list_keeps_the_other_labels():
    labels = _status_badges(
        _survey("s1", "完了"),
        _with(_survey("s2", "実行中"), status="running"),
        _with(_survey("s3", "失敗"), status="error"),
    )
    assert labels == ["完了", "実行中", "エラー"]


def test_list_never_shows_an_order_or_a_start_time():
    """BR2.5: 待ちの間は順番も予定時刻も出さない。"""
    html = render_page(
        "survey/partials/result_list.html",
        listing=_listing([_with(_survey("s1", "待ち"), status="queued")], ListTab.MINE),
        current_search="",
        is_append=False,
    )
    for word in ("番目", "予定", "開始予定"):
        assert word not in html


def _detail_html(screen_client: Any, survey: Survey) -> str:
    from src.models.survey import SurveyDetail

    executions = Mock()
    executions.get_survey_detail.return_value = SurveyDetail(
        survey=survey, manageable=True, template_name="t", analysis_available=True
    )
    with patch("web.routers.survey.get_execution_manager", return_value=executions):
        response = screen_client.get(f"/survey/results/{survey.id}")
    assert response.status_code == 200
    return response.text


def test_detail_of_a_queued_survey_explains_the_wait(screen_client):
    html = _detail_html(screen_client, _with(_survey("s1", "待ち"), status="queued"))
    assert 'data-testid="survey-queued-note"' in html
    assert "前のアンケートが終わったら始まります" in html


def test_detail_of_a_blocked_survey_says_it_cannot_start(screen_client):
    survey = _with(
        _survey("s1", "止まった待ち"), status="queued", start_blocked_since=BLOCKED_AT
    )
    html = _detail_html(screen_client, survey)
    assert "開始できずに待っています" in html


def test_detail_shows_how_many_answers_are_missing(screen_client):
    """FR5.3・BR4.4: 取れなかった件数があれば結果の上に出す。"""
    survey = _with(_survey("s1", "完了"), failed_count=3)
    html = _detail_html(screen_client, survey)
    note = html.split('data-testid="survey-failed-count"', 1)[1].split("</", 1)[0]
    assert "3 件の回答を取得できませんでした（CSV では空欄）" in note


@pytest.mark.parametrize("failed_count", [0, None])
def test_detail_without_missing_answers_has_no_note(screen_client, failed_count):
    survey = _with(_survey("s1", "完了"), failed_count=failed_count)
    assert 'data-testid="survey-failed-count"' not in _detail_html(
        screen_client, survey
    )


def test_start_form_minimum_is_ten():
    """FR4.1: 件数の下限の表示は 10。"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "web/templates/survey/start.html"
    ).read_text(encoding="utf-8")
    assert 'min="10"' in source
    assert "（10〜1,000）" in source
    assert "（10〜10,000）" in source
    assert 'min="100"' not in source


@pytest.mark.parametrize(("count", "warned"), [(9, True), (10, False)])
def test_filter_preview_warns_below_ten(count, warned):
    """FR4.1: 対象の人数の注意は、下限の 10 名を下回るときだけ出す。"""
    from src.models.survey import MIN_SURVEY_PERSONAS
    from web.main import templates

    html = templates.get_template("survey/partials/filter_preview.html").render(
        count=count,
        total=500,
        stats={},
        has_filter=True,
        min_count=MIN_SURVEY_PERSONAS,
    )
    assert ("アンケート実行には10名以上が必要です" in html) is warned
    assert ("bg-yellow-50" in html) is warned


# =============================================================================
# member のペルソナ元データの画面
# =============================================================================


def test_member_persona_data_screen_has_no_management(
    ui: UiHarness, test_app: Any
) -> None:
    from fastapi.testclient import TestClient

    from tests.conftest import MEMBER_A

    record = CustomPersonaData.create_new(
        name="営業向けデータ",
        columns=["persona"],
        row_count=150,
        registered_by="admin",
        shared_group_ids=frozenset({"sales"}),
        now=NOW,
    )
    manager = Mock()
    manager.check_nemotron_status.return_value = {"exists": False, "size_mb": 0}
    manager.list_persona_data.return_value = PersonaDataListing(
        custom=(record,), manageable=False
    )
    with (
        patch("web.routers.survey.get_dataset_manager", return_value=manager),
        TestClient(test_app, follow_redirects=False) as member,
    ):
        MEMBER_A.login(member)
        ui.load_screen(member, "/survey/persona-data")
    ui.settle()

    assert "営業向けデータ" in ui.page.locator("#custom-dataset-list").inner_text()
    for testid in (
        "nemotron-download-button",
        "custom-data-upload",
        "custom-data-delete-button",
        "custom-data-scope-button",
    ):
        assert ui.page.locator(f"[data-testid='{testid}']").count() == 0, testid
    assert ui.page.locator("[data-testid='persona-data-member-note']").is_visible()
    ui.check_a11y()
