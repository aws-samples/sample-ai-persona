"""Survey ルーターの API テスト（U7）。

本物の U7 の Manager と判定の部品を moto の上で動かす（tests/api/u7_world.py）。
BR1.2〜BR1.4、BR1.9、BR2.1、BR2.6、BR2.8、BR2.11、BR2.12、BR4.1、BR4.2、BR5.2、
NFR1.66〜NFR1.74、NFR1.79、NFR4.14、AC2.1.2、AC2.1.3、AC2.2.4、AC2.5.1〜AC2.5.3、
AC4.1.1〜AC4.1.5、AC4.4.1、AC4.4.2、AC4.4.4、AC5.1.1、AC5.1.3、AC5.2.2。
"""

import json
from contextlib import ExitStack
from unittest.mock import Mock, patch
from urllib.parse import quote, urlencode

import pytest

from src.models.authz import ResourceType
from tests.api.u7_world import ADMIN_USER, ME, OTHER, PEER_SUB, ApiWorld

TEMPLATE = ResourceType.SURVEY_TEMPLATE
SURVEY = ResourceType.SURVEY
_UPLOADED = "s3://bucket/uploads/local:dev@example.com/survey_image/f1_a.png"
_QUESTIONS = json.dumps(
    [{"question_type": "multiple_choice", "text": "Q", "options": ["A", "B"]}]
)


@pytest.fixture
def aw(client, survey_world):
    """ME signed in, with the real U7 Managers behind the routers."""
    ME.login(client)
    world = ApiWorld(survey_world)
    with ExitStack() as stack:
        world.patch_routers(stack)
        yield world


def _as(client, user) -> None:
    user.login(client)


def _preview_route(file_path: str) -> str:
    return "/survey/image-preview/file?" + urlencode({"file_path": file_path})


# =============================================================================
# 画面の入口（今のまま）
# =============================================================================


def test_survey_index_loads(client):
    assert client.get("/survey/").status_code == 200


def test_template_new_form_loads(client):
    assert client.get("/survey/templates/new").status_code == 200


# =============================================================================
# テンプレートの一覧（BR1.2、AC2.1.2、AC2.1.3、AC4.1.1、AC4.1.4、AC5.1.1）
# =============================================================================


class TestTemplateList:
    def test_tabs_counts_and_only_readable_rows(self, aw, client):
        mine = aw.template(name="自分のテンプレート")
        shared = aw.template(PEER_SUB, name="共有されたテンプレート")
        aw.world.share(TEMPLATE, shared.id, "sales")
        aw.template("c", name="他人のテンプレート")

        html = client.get("/survey/templates").text

        assert 'data-testid="list-tab-mine"' in html
        assert "自分のテンプレート" in html
        assert "他人のテンプレート" not in html
        assert "共有されたテンプレート" not in html
        shared_html = client.get("/survey/templates?tab=shared").text
        assert "共有されたテンプレート" in shared_html
        assert "利用者b" in shared_html
        assert mine.id not in shared_html

    def test_zero_rows_explain_how_to_start(self, aw, client):
        html = client.get("/survey/templates").text
        assert 'data-testid="survey-template-list-empty"' in html
        shared = client.get("/survey/templates?tab=shared").text
        assert "共有されると" in shared

    def test_member_asking_for_all_gets_mine(self, aw, client):
        aw.template("c", name="他人のテンプレート")
        html = client.get("/survey/templates?tab=all").text
        assert "他人のテンプレート" not in html
        assert 'data-testid="list-tab-all"' not in html

    def test_admin_all_tab(self, aw, client):
        aw.template("c", name="他人のテンプレート")
        aw.template(None, name="古いテンプレート")
        _as(client, ADMIN_USER)
        html = client.get("/survey/templates?tab=all").text
        assert "他人のテンプレート" in html
        assert "古いテンプレート" in html
        assert "所有者不明" in html

    def test_search_inside_the_tab(self, aw, client):
        aw.template(name="顧客満足度")
        aw.template(name="新商品")
        html = client.get("/survey/templates?search=顧客").text
        assert "顧客満足度" in html
        assert "新商品" not in html

    def test_own_rows_offer_share_edit_and_delete(self, aw, client):
        aw.template(name="自分のテンプレート")
        html = client.get("/survey/templates").text
        assert 'data-testid="survey-template-share-button"' in html
        assert 'data-testid="survey-template-delete-button"' in html
        assert 'data-testid="bulk-select-bar"' in html

    def test_shared_rows_offer_no_changes(self, aw, client):
        shared = aw.template(PEER_SUB, name="共有されたテンプレート")
        aw.world.share(TEMPLATE, shared.id, "sales")
        html = client.get("/survey/templates?tab=shared").text
        assert 'data-testid="survey-template-share-button"' not in html
        assert 'data-testid="survey-template-delete-button"' not in html


# =============================================================================
# テンプレートの読む・変える・消す（BR1.3、BR1.4、AC4.1.3、AC4.4.1）
# =============================================================================


class TestTemplateAccess:
    def test_owner_opens_the_edit_screen(self, aw, client):
        template = aw.template(name="編集する")
        html = client.get(f"/survey/templates/{template.id}/edit").text
        assert "編集する" in html

    @pytest.mark.parametrize("which", ["unrelated", "missing"])
    def test_unrelated_and_missing_are_the_same_404(self, aw, client, which):
        template = aw.template("c")
        target = template.id if which == "unrelated" else "missing-id"
        response = client.get(f"/survey/templates/{target}/edit")
        assert response.status_code == 404

    def test_shared_reader_cannot_edit(self, aw, client):
        shared = aw.template(PEER_SUB)
        aw.world.share(TEMPLATE, shared.id, "sales")
        assert client.get(f"/survey/templates/{shared.id}/edit").status_code == 403
        response = client.put(
            f"/survey/templates/{shared.id}",
            data={"name": "x", "questions_json": _QUESTIONS},
        )
        assert response.status_code == 403
        assert client.delete(f"/survey/templates/{shared.id}").status_code == 403

    def test_unrelated_update_and_delete_are_404(self, aw, client):
        template = aw.template("c")
        response = client.put(
            f"/survey/templates/{template.id}",
            data={"name": "x", "questions_json": _QUESTIONS},
        )
        assert response.status_code == 404
        assert client.delete(f"/survey/templates/{template.id}").status_code == 404
        assert aw.world.template_store.get(template.id) is not None

    def test_create_records_me_as_the_owner(self, aw, client):
        response = client.post(
            "/survey/templates", data={"name": "新しい", "questions_json": _QUESTIONS}
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/survey/templates"
        listed = aw.templates.list_templates(ME.principal(), "mine", None)
        assert [t.name for t in listed.items] == ["新しい"]

    def test_owner_updates_and_deletes(self, aw, client):
        template = aw.template(name="旧")
        response = client.put(
            f"/survey/templates/{template.id}",
            data={"name": "新", "questions_json": _QUESTIONS},
        )
        assert response.status_code == 200
        assert aw.world.template_store.get(template.id).name == "新"
        assert client.delete(f"/survey/templates/{template.id}").status_code == 200
        assert aw.world.template_store.get(template.id) is None

    def test_image_that_cannot_be_attached_is_not_found(self, aw, client):
        from src.models.errors import CodedError, ErrorCode

        aw.world.files.attach_all.side_effect = CodedError(
            "not attachable", code=ErrorCode.FILE_NOT_FOUND
        )
        images = json.dumps(
            [
                {
                    "id": "i",
                    "name": "画像",
                    "file_path": f"s3://b/uploads/{ME.sub}/survey_image/"
                    "11111111-1111-4111-8111-111111111111_a.png",
                    "mime_type": "image/png",
                    "original_filename": "a.png",
                }
            ]
        )
        response = client.post(
            "/survey/templates",
            data={"name": "画像", "questions_json": _QUESTIONS, "images_json": images},
        )
        assert response.status_code == 404


# =============================================================================
# アンケートの開始（BR2.1、BR2.12、AC2.5.1、AC2.5.2、AC4.4.5）
# =============================================================================


class TestStart:
    def test_start_page_lists_readable_templates_and_usable_data(self, aw, client):
        aw.template(name="自分のテンプレート")
        aw.template("c", name="他人のテンプレート")
        usable = aw.world.custom_data("営業向けデータ", "sales")
        aw.world.custom_data("企画向けデータ", "plan")
        html = client.get("/survey/start").text
        assert "自分のテンプレート" in html
        assert "他人のテンプレート" not in html
        assert "営業向けデータ" in html
        assert f"custom:{usable.id}" in html
        assert "企画向けデータ" not in html

    def test_shared_template_tab_on_the_start_page(self, aw, client):
        shared = aw.template(PEER_SUB, name="共有されたテンプレート")
        aw.world.share(TEMPLATE, shared.id, "sales")
        html = client.get("/survey/start?tab=shared").text
        assert "共有されたテンプレート" in html

    def test_execute_with_an_unreadable_template_is_404_and_runs_nothing(
        self, aw, client
    ):
        template = aw.template("c")
        response = client.post(
            "/survey/execute",
            data={"template_id": template.id, "persona_count": "100"},
        )
        assert response.status_code == 404
        aw.batch.get_filtered_count.assert_not_called()
        assert aw.world.client.scan(TableName=aw.world.surveys_table)["Count"] == 0

    def test_execute_with_unusable_custom_data_is_404(self, aw, client):
        template = aw.template()
        data = aw.world.custom_data("企画向け", "plan")
        response = client.post(
            "/survey/execute",
            data={
                "template_id": template.id,
                "persona_count": "100",
                "datasource": f"custom:{data.id}",
            },
        )
        assert response.status_code == 404
        assert aw.world.client.scan(TableName=aw.world.surveys_table)["Count"] == 0

    def test_execute_starts_my_survey(self, aw, client):
        template = aw.template(name="使う")
        response = client.post(
            "/survey/execute",
            data={"template_id": template.id, "persona_count": "100"},
        )
        assert response.status_code == 200
        listed = aw.executions.list_surveys(ME.principal(), "mine", None)
        assert len(listed.items) == 1
        assert listed.items[0].template_snapshot.name == "使う"
        assert listed.items[0].status == "queued"

    def test_execute_wakes_the_runner_without_waiting(self, aw, client):
        """NFR1.4・WF1: 受付は保存して進行役を 1 回起こすだけ（推論を待たない）。"""
        template = aw.template()

        response = client.post(
            "/survey/execute",
            data={"template_id": template.id, "persona_count": "10"},
        )

        assert response.status_code == 200
        aw.runner_loop.wake.assert_called_once_with()
        aw.runner.start_execution.assert_not_called()

    def test_ten_personas_are_accepted(self, aw, client):
        """FR4.1: 下限は 10。"""
        template = aw.template()
        assert (
            client.post(
                "/survey/execute",
                data={"template_id": template.id, "persona_count": "10"},
            ).status_code
            == 200
        )
        response = client.post(
            "/survey/execute",
            data={"template_id": template.id, "persona_count": "9"},
        )
        assert response.status_code == 400
        assert response.headers["HX-Retarget"] == "#execute-error"

    def test_no_runner_is_a_config_error_and_nothing_is_saved(self, aw, client):
        """BR9.1・FR10.1: 手元では CONFIG のエラーを開始画面に出し、何も保存しない。"""
        from src.models.errors import ErrorCode
        from web.error_messages import user_message_for_code

        aw.runner_configured = False
        template = aw.template()

        response = client.post(
            "/survey/execute",
            data={"template_id": template.id, "persona_count": "10"},
        )

        assert response.headers["HX-Retarget"] == "#execute-error"
        assert response.headers["X-Render-Response"] == "true"
        message = user_message_for_code(ErrorCode.SURVEY_RUNNER_NOT_CONFIGURED.value)
        assert message in response.text
        assert aw.world.client.scan(TableName=aw.world.surveys_table)["Count"] == 0
        aw.runner_loop.wake.assert_not_called()

    def test_filter_options_and_preview_judge_the_datasource(self, aw, client):
        data = aw.world.custom_data("企画向け", "plan")
        source = f"custom:{data.id}"
        assert (
            client.get(f"/survey/filter-options?datasource={source}").status_code == 404
        )
        response = client.post("/survey/preview", data={"datasource": source})
        assert response.status_code == 404
        aw.batch.get_filtered_count.assert_not_called()

    def test_preview_judges_with_the_minimum_from_the_model(
        self, aw, client, monkeypatch
    ):
        """FR4.1: 最少人数はテンプレートに直接書かず、MIN_SURVEY_PERSONAS を使う。"""
        import web.routers.survey as survey_router

        monkeypatch.setattr(survey_router, "MIN_SURVEY_PERSONAS", 600)

        response = client.post("/survey/preview", data={"datasource": "nemotron"})

        assert response.status_code == 200
        assert "border-yellow-300" in response.text


# =============================================================================
# 結果（BR1.2〜BR1.4、BR2.6、BR2.8、BR2.11、AC4.1.2、AC4.4.4）
# =============================================================================


class TestResults:
    def _shared(self, aw):
        survey = aw.survey(PEER_SUB, name="共有された結果")
        aw.world.share(SURVEY, survey.id, "sales")
        return survey

    def test_list_tabs_and_names_from_the_snapshot(self, aw, client):
        aw.survey(name="自分の結果")
        self._shared(aw)
        aw.survey("c", name="他人の結果")
        html = client.get("/survey/results").text
        assert "自分の結果" in html
        assert "写しのテンプレート" in html
        assert "他人の結果" not in html
        shared = client.get("/survey/results?tab=shared").text
        assert "共有された結果" in shared

    def test_zero_results(self, aw, client):
        html = client.get("/survey/results").text
        assert 'data-testid="survey-result-list-empty"' in html

    def test_owner_detail_offers_delete_share_and_report(self, aw, client):
        survey = aw.survey(name="自分の結果")
        html = client.get(f"/survey/results/{survey.id}").text
        assert 'data-testid="survey-delete-button"' in html
        assert 'data-testid="survey-share-button"' in html
        assert 'data-testid="survey-report-generate"' in html

    def test_shared_reader_detail_is_read_only(self, aw, client):
        survey = self._shared(aw)
        html = client.get(f"/survey/results/{survey.id}").text
        assert "共有された結果" in html
        assert 'data-testid="survey-delete-button"' not in html
        assert 'data-testid="survey-share-button"' not in html
        assert 'data-testid="survey-report-generate"' not in html

    def test_shared_reader_cannot_delete_or_generate(self, aw, client):
        survey = self._shared(aw)
        assert client.delete(f"/survey/results/{survey.id}").status_code == 403
        response = client.get(
            f"/survey/results/{survey.id}/report/generate",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 403
        aw.ai.generate_standard_report_streaming.assert_not_called()

    @pytest.mark.parametrize(
        "path",
        [
            "/survey/results/{id}",
            "/survey/results/{id}/personas",
            "/survey/results/{id}/visual",
            "/survey/results/{id}/download",
            "/survey/results/{id}/images/img1",
        ],
    )
    @pytest.mark.parametrize("which", ["unrelated", "missing"])
    def test_unrelated_and_missing_are_404(self, aw, client, path, which):
        survey = aw.survey("c")
        target = survey.id if which == "unrelated" else "missing-id"
        response = client.get(path.format(id=target), follow_redirects=False)
        assert response.status_code == 404

    def test_download_redirects_to_a_signed_url(self, aw, client):
        survey = self._shared(aw)
        response = client.get(
            f"/survey/results/{survey.id}/download", follow_redirects=False
        )
        assert response.status_code in (302, 307)
        assert "results.csv" in response.headers["location"]

    def test_download_streams_the_csv_in_proxy_mode(self, aw, client, monkeypatch):
        """閉域対応 U4 BR2.2、BR4.1: 本文が S3 の中身と同じで、日本語の名前で落ちる"""
        from src.config import config

        monkeypatch.setattr(config, "ASSET_DELIVERY", "proxy")
        survey = self._shared(aw)
        stored = aw.world.s3.download_file(survey.s3_result_path)

        response = client.get(
            f"/survey/results/{survey.id}/download", follow_redirects=False
        )

        assert response.status_code == 200
        assert response.content == stored
        assert response.headers["Content-Type"].startswith("text/csv")
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment;")
        assert f"filename*=UTF-8''{quote('共有された結果.csv')}" in disposition
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "private, no-store"

    def test_too_large_csv_is_refused_in_proxy_mode(self, aw, client, monkeypatch):
        """閉域対応 U4 BR2.3: 上限を超えると ASSET_TOO_LARGE（本文は流さない）"""
        from src.config import config
        from src.models.asset_delivery import SizeLimit
        from src.models.errors import ErrorCode
        from web.error_messages import http_status_for

        monkeypatch.setattr(config, "ASSET_DELIVERY", "proxy")
        monkeypatch.setattr(
            "src.managers.components.asset_delivery.max_bytes",
            lambda limit, upload_max_bytes: 1 if limit is SizeLimit.SURVEY_CSV else 0,
        )
        survey = self._shared(aw)

        response = client.get(
            f"/survey/results/{survey.id}/download", follow_redirects=False
        )

        assert response.status_code == http_status_for(ErrorCode.ASSET_TOO_LARGE)
        assert response.headers.get("X-Content-Type-Options") is None

    @pytest.mark.parametrize("mode", ["presigned", "proxy"])
    def test_stranger_download_is_404_in_both_modes(
        self, aw, client, monkeypatch, mode
    ):
        from src.config import config

        monkeypatch.setattr(config, "ASSET_DELIVERY", mode)
        survey = aw.survey("c")
        response = client.get(
            f"/survey/results/{survey.id}/download", follow_redirects=False
        )
        assert response.status_code == 404

    def test_result_image_is_streamed_in_proxy_mode(self, aw, client, monkeypatch):
        from src.config import config
        from src.models.survey_template import TemplateImage

        monkeypatch.setattr(config, "ASSET_DELIVERY", "proxy")
        key = "uploads/b/survey_image/11111111-1111-4111-8111-111111111111_a.png"
        aw.world.s3.upload_file(b"png-bytes", key)
        image = TemplateImage(
            id="img1",
            name="商品",
            file_path=aw.world.s3.path_of(key),
            mime_type="image/png",
            original_filename="a.png",
        )
        survey = aw.world.survey(PEER_SUB, aw.world.template(PEER_SUB, images=[image]))
        aw.world.share(SURVEY, survey.id, "sales")

        response = client.get(
            f"/survey/results/{survey.id}/images/img1", follow_redirects=False
        )

        assert response.status_code == 200
        assert response.content == b"png-bytes"
        assert response.headers["Content-Disposition"].startswith("inline;")

    def test_analysis_for_a_shared_reader(self, aw, client):
        survey = self._shared(aw)
        assert client.get(f"/survey/results/{survey.id}/personas").status_code == 200
        assert client.get(f"/survey/results/{survey.id}/visual").status_code == 200

    def test_result_image_only_from_the_snapshot(self, aw, client):
        from src.models.survey_template import TemplateImage

        key = "uploads/b/survey_image/11111111-1111-4111-8111-111111111111_a.png"
        aw.world.s3.upload_file(b"png", key)
        image = TemplateImage(
            id="img1",
            name="商品",
            file_path=aw.world.s3.path_of(key),
            mime_type="image/png",
            original_filename="a.png",
        )
        template = aw.world.template(PEER_SUB, images=[image])
        survey = aw.world.survey(PEER_SUB, template)
        aw.world.share(SURVEY, survey.id, "sales")
        response = client.get(
            f"/survey/results/{survey.id}/images/img1", follow_redirects=False
        )
        assert response.status_code in (302, 307)
        other = client.get(
            f"/survey/results/{survey.id}/images/other", follow_redirects=False
        )
        assert other.status_code == 404
        detail = client.get(f"/survey/results/{survey.id}").text
        assert f"/survey/results/{survey.id}/images/img1" in detail

    def test_owner_deletes(self, aw, client):
        survey = aw.survey()
        assert client.delete(f"/survey/results/{survey.id}").status_code == 200
        assert aw.world.survey_store.get(survey.id) is None

    def test_deleting_a_running_survey_stops_it_and_wakes_the_runner(self, aw, client):
        """BR7.1・WF6: 実行中を消したら実行を止め、進行役を 1 回起こして次を始める。"""
        arn = "arn:aws:states:r:1:execution:sm:x"
        survey = aw.survey(status="running", execution_arn=arn)

        assert client.delete(f"/survey/results/{survey.id}").status_code == 200

        aw.runner.stop_execution.assert_called_once_with(arn)
        aw.runner_loop.wake.assert_called_once_with()

    def test_detail_of_an_interrupted_old_survey_shows_the_error(self, aw, client):
        """BR8.1: 切り替え前の running は中断のエラーとして出る。"""
        from src.models.errors import ErrorCode
        from web.error_messages import user_message_for_code

        survey = aw.survey(status="running")

        html = client.get(f"/survey/results/{survey.id}").text

        message = user_message_for_code(ErrorCode.SURVEY_EXECUTION_INTERRUPTED.value)
        assert message in html


# =============================================================================
# ペルソナ元データ（BR4.1、BR4.2、BR5.2、NFR1.73、NFR1.74、AC2.5.3、AC5.2.2）
# =============================================================================

_MANAGEMENT_ROUTES = [
    ("post", "/survey/persona-data/download-nemotron", {}),
    ("post", "/survey/persona-data/preview-prompt", {"data": {"temp_key": "k"}}),
    ("post", "/survey/persona-data/confirm-mapping", {"data": {"temp_key": "k"}}),
    ("post", "/survey/persona-data/dwh-extract", {"data": {"condition": "x"}}),
    ("post", "/survey/persona-data/dwh-preview", {"data": {"temp_key": "k"}}),
    ("post", "/survey/persona-data/dwh-confirm", {"data": {"temp_key": "k"}}),
    ("delete", "/survey/persona-data/custom/{id}", {}),
    ("get", "/survey/persona-data/custom/{id}/scope", {}),
    (
        "post",
        "/survey/persona-data/custom/{id}/scope",
        {"data": {"departments": "sales"}},
    ),
]


class TestPersonaData:
    def test_member_page_has_no_management_and_only_usable_data(self, aw, client):
        aw.world.custom_data("営業向け", "sales")
        aw.world.custom_data("企画向け", "plan")
        html = client.get("/survey/persona-data").text
        assert "営業向け" in html
        assert "企画向け" not in html
        for testid in (
            "nemotron-download-button",
            "custom-data-upload",
            "custom-data-delete-button",
            "custom-data-scope-button",
        ):
            assert f'data-testid="{testid}"' not in html
        assert "管理者に依頼してください" in html

    def test_admin_page_has_the_management_and_every_record(self, aw, client):
        aw.world.custom_data("営業向け", "sales")
        aw.world.custom_data("admin だけ")
        _as(client, ADMIN_USER)
        html = client.get("/survey/persona-data").text
        assert "営業向け" in html and "admin だけ" in html
        assert 'data-testid="custom-data-upload"' in html
        assert 'data-testid="custom-data-scope-button"' in html
        assert "利用範囲: admin のみ" in html
        assert "利用範囲: 営業部" in html

    @pytest.mark.parametrize(("method", "path", "kwargs"), _MANAGEMENT_ROUTES)
    def test_member_management_routes_are_404_and_change_nothing(
        self, aw, client, method, path, kwargs
    ):
        data = aw.world.custom_data("営業向け", "sales")
        response = getattr(client, method)(path.format(id=data.id), **kwargs)
        assert response.status_code == 404
        assert aw.world.custom_store.get(data.id) is not None
        aw.batch.download_nemotron_dataset.assert_not_called()

    def test_member_upload_is_404_and_nothing_is_written(self, aw, client):
        response = client.post(
            "/survey/persona-data/upload-custom",
            files={"file": ("a.csv", b"persona\nx\n", "text/csv")},
        )
        assert response.status_code == 404
        listed = aw.world.s3.s3_client.list_objects_v2(
            Bucket="u7-test-bucket", Prefix="persona-dataset/"
        )
        assert listed.get("KeyCount", 0) == 0

    def test_detail_by_id_for_users_in_scope(self, aw, client):
        usable = aw.world.custom_data("営業向け", "sales")
        other = aw.world.custom_data("企画向け", "plan")
        detail = client.get(f"/survey/persona-data/custom/{usable.id}/detail")
        assert detail.status_code == 200
        assert "営業向け" in detail.text
        assert (
            client.get(f"/survey/persona-data/custom/{other.id}/detail").status_code
            == 404
        )

    def test_admin_scope_dialog_and_save(self, aw, client):
        data = aw.world.custom_data("営業向け", "sales")
        _as(client, ADMIN_USER)
        dialog = client.get(f"/survey/persona-data/custom/{data.id}/scope").text
        assert 'name="departments"' in dialog
        assert 'name="departments_initial"' in dialog
        assert 'role="alert"' in dialog
        response = client.post(
            f"/survey/persona-data/custom/{data.id}/scope",
            data={"departments": "plan", "departments_initial": "sales"},
        )
        assert response.status_code == 200
        assert "dialogClose" in response.headers["HX-Trigger"]
        assert aw.world.custom_store.get(data.id).shared_group_ids == frozenset(
            {"plan"}
        )

    def test_admin_imports_with_a_name_and_a_scope(self, aw, client):
        _as(client, ADMIN_USER)
        aw.world.s3.upload_file(b"persona\nx\n", "persona-dataset/temp/a.csv")
        response = client.post(
            "/survey/persona-data/confirm-mapping",
            data={
                "temp_key": "persona-dataset/temp/a.csv",
                "dataset_name": "取り込んだデータ",
                "departments": "sales",
                "mapping_persona": "persona",
            },
        )
        assert response.status_code == 200
        records = aw.world.custom_store.list_all()
        assert [r.name for r in records] == ["取り込んだデータ"]
        assert records[0].shared_group_ids == frozenset({"sales"})

    def test_admin_deletes_by_id(self, aw, client):
        data = aw.world.custom_data("消す")
        _as(client, ADMIN_USER)
        assert (
            client.delete(f"/survey/persona-data/custom/{data.id}").status_code == 200
        )
        assert aw.world.custom_store.get(data.id) is None


# =============================================================================
# 画像のアップロードと配信（BR1.9、U5 のまま）
# =============================================================================


class TestSurveyImages:
    @patch("web.routers.survey.get_file_manager")
    def test_upload_passes_the_signed_in_user(self, mock_get_fm, client):
        manager = mock_get_fm.return_value
        manager.upload_survey_image.return_value = Mock(
            file_id="f1",
            file_path=_UPLOADED,
            mime_type="image/png",
            original_filename="a.png",
        )
        manager.check_preview_path.return_value = None

        resp = client.post(
            "/survey/templates/upload-image",
            files={"file": ("a.png", b"\x89PNG", "image/png")},
        )

        assert resp.status_code == 200
        principal, content, filename = manager.upload_survey_image.call_args.args
        assert principal.sub == "local:dev@example.com"
        assert (content, filename) == (b"\x89PNG", "a.png")
        assert resp.json()["preview_url"] == _preview_route(_UPLOADED)

    @pytest.mark.parametrize(
        "path",
        [
            "s3://bucket/datasets/secret.csv",
            "s3://bucket/uploads/local:other@example.com/survey_image/x_a.png",
            "../../etc/passwd",
        ],
    )
    def test_image_preview_of_any_other_path_is_not_found(self, client, path):
        """任意のパス・他人のパスは 404 で URL を作らない（BR1.9）"""
        from src.managers.file_manager import FileManager
        from src.models.errors import CodedError, ErrorCode

        s3 = Mock()
        s3.key_of.side_effect = lambda p: (
            p.removeprefix("s3://bucket/") if p.startswith("s3://bucket/") else None
        )
        store = Mock()
        store.get.return_value = None
        attachments = Mock()
        attachments.readable_file.side_effect = CodedError(
            "unreadable", code=ErrorCode.FILE_NOT_FOUND
        )
        manager = FileManager(s3_service=s3, file_store=store, attachments=attachments)
        with patch("web.routers.survey.get_file_manager", return_value=manager):
            resp = client.get("/survey/image-preview", params={"file_path": path})

        assert resp.status_code == 404
        s3.generate_presigned_url.assert_not_called()

    def test_edit_screen_signs_images_through_the_file_attachments(self, aw, client):
        from src.models.survey_template import TemplateImage

        image = TemplateImage(
            id="img1",
            name="商品",
            file_path=_UPLOADED,
            mime_type="image/png",
            original_filename="a.png",
        )
        template = aw.template(images=[image])
        html = client.get(f"/survey/templates/{template.id}/edit").text
        assert _preview_route(_UPLOADED) in html
        assert "amazonaws.com" not in html
        principal, path = aw.file_manager.check_preview_path.call_args.args
        assert principal.sub == ME.sub and path == _UPLOADED

    def test_edit_screen_leaves_unreadable_images_without_a_url(self, aw, client):
        from src.models.errors import CodedError, ErrorCode
        from src.models.survey_template import TemplateImage

        aw.file_manager.check_preview_path.side_effect = CodedError(
            "gone", code=ErrorCode.FILE_NOT_FOUND
        )
        image = TemplateImage(
            id="img1",
            name="商品",
            file_path=_UPLOADED,
            mime_type="image/png",
            original_filename="a.png",
        )
        template = aw.template(images=[image])
        html = client.get(f"/survey/templates/{template.id}/edit").text
        assert "/survey/image-preview/file" not in html

    def test_image_preview_json_points_at_the_app_route(self, aw, client):
        resp = client.get("/survey/image-preview", params={"file_path": _UPLOADED})
        assert resp.status_code == 200
        assert resp.json() == {"url": _preview_route(_UPLOADED)}

    @pytest.mark.parametrize("mode", ["presigned", "proxy"])
    @pytest.mark.parametrize(
        "path",
        [
            "s3://bucket/datasets/secret.csv",
            "s3://bucket/uploads/local:other@example.com/survey_image/x_a.png",
            "../../etc/passwd",
        ],
    )
    def test_preview_file_of_any_other_path_is_404_without_s3(
        self, client, monkeypatch, path, mode
    ):
        """閉域対応 U4 BR3.4、BR4.4: 任意のパス・他人のパスは開かず署名もしない"""
        from src.config import config
        from src.managers.file_manager import FileManager
        from src.models.errors import CodedError, ErrorCode

        monkeypatch.setattr(config, "ASSET_DELIVERY", mode)
        s3 = Mock()
        s3.key_of.side_effect = lambda p: (
            p.removeprefix("s3://bucket/") if p.startswith("s3://bucket/") else None
        )
        attachments = Mock()
        attachments.readable_file.side_effect = CodedError(
            "unreadable", code=ErrorCode.FILE_NOT_FOUND
        )
        manager = FileManager(s3_service=s3, file_store=Mock(), attachments=attachments)
        with patch("web.routers.survey.get_file_manager", return_value=manager):
            resp = client.get(
                "/survey/image-preview/file",
                params={"file_path": path},
                follow_redirects=False,
            )

        assert resp.status_code == 404
        s3.generate_presigned_url.assert_not_called()
        s3.open_stream.assert_not_called()

    def test_preview_file_redirects_in_presigned_mode(self, aw, client):
        from src.models.asset_delivery import DeliveryResult

        aw.file_manager.deliver_preview.return_value = DeliveryResult.redirect(
            "https://signed/a.png"
        )
        resp = client.get(
            "/survey/image-preview/file",
            params={"file_path": _UPLOADED},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://signed/a.png"
        principal, path = aw.file_manager.deliver_preview.call_args.args
        assert principal.sub == ME.sub and path == _UPLOADED

    def test_preview_file_streams_in_proxy_mode(self, aw, client):
        from src.models.asset_delivery import AssetStream, DeliveryResult

        close = Mock()
        aw.file_manager.deliver_preview.return_value = DeliveryResult.stream(
            AssetStream(chunks=iter([b"\x89PNG"]), content_length=4, close=close),
            "image/png",
            "商品.png",
        )
        resp = client.get(
            "/survey/image-preview/file",
            params={"file_path": _UPLOADED},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG"
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.headers["Content-Disposition"].startswith("inline;")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "private, no-store"
        close.assert_called()

    def test_path_signing_helper_is_gone(self):
        import web.routers.survey as survey_router

        assert not hasattr(survey_router, "_get_image_preview_url")


# =============================================================================
# CSRF（NFR4.14）
# =============================================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/survey/templates"),
        ("put", "/survey/templates/t1"),
        ("delete", "/survey/templates/t1"),
        ("post", "/survey/execute"),
        ("delete", "/survey/results/s1"),
        ("delete", "/survey/persona-data/custom/c1"),
        ("post", "/survey/persona-data/custom/c1/scope"),
        ("post", "/survey/persona-data/confirm-mapping"),
    ],
)
def test_changes_need_the_csrf_header(test_app, method, path):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as no_header:
        ME.login(no_header)
        response = getattr(no_header, method)(path)

    assert response.status_code == 403


def test_unrelated_user_sees_nothing_of_mine(aw, client):
    """AC4.4.2: 部署の違う人の一覧に、私のテンプレート・結果が出ない。"""
    aw.template(name="私のテンプレート")
    aw.survey(name="私の結果")
    _as(client, OTHER)
    assert "私のテンプレート" not in client.get("/survey/templates").text
    assert "私の結果" not in client.get("/survey/results").text


# =============================================================================
# 閉域対応 U3: Nemotron の入手先（BR2.2〜BR2.5）と DWH 抽出（BR3.2 の 4、BR3.7）
# =============================================================================

NEMOTRON_KEY = "persona-dataset/nemotron-personas-japan.parquet"
HTMX = {"HX-Request": "true"}


@pytest.fixture
def closed_source(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "NEMOTRON_SOURCE", "s3")


@pytest.fixture
def head_fails(aw, monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    def head(**_kwargs):
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    monkeypatch.setattr(aw.world.s3.s3_client, "head_object", head)


class TestNemotronSourceRoutes:
    def test_closed_download_is_forbidden_without_background_work(
        self, aw, client, closed_source
    ):
        from web.routers import survey as survey_router

        _as(client, ADMIN_USER)
        with patch("web.routers.survey.asyncio.create_task") as create_task:
            response = client.post(
                "/survey/persona-data/download-nemotron", headers=HTMX
            )
        assert response.status_code == 403
        assert "HX-Trigger" in response.headers
        create_task.assert_not_called()
        assert survey_router._nemotron_download_status["downloading"] is False
        aw.batch.download_nemotron_dataset.assert_not_called()

    @pytest.mark.parametrize(
        "path", ["/survey/persona-data", "/survey/persona-data/nemotron-status"]
    )
    def test_screens_render_when_the_status_head_fails(
        self, aw, client, closed_source, head_fails, path
    ):
        _as(client, ADMIN_USER)
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-testid="nemotron-download-button"' not in response.text

    def test_start_page_offers_nemotron_only_when_available(self, aw, client):
        aw.world.custom_data("営業向けデータ", "sales")
        html = client.get("/survey/start").text
        assert '<option value="nemotron">' not in html
        assert "営業向けデータ" in html
        aw.world.s3.upload_file(b"x" * 1024, NEMOTRON_KEY)
        html = client.get("/survey/start").text
        assert '<option value="nemotron">' in html
        assert "営業向けデータ" in html


class TestPersonaDataScreenByEnvironment:
    """BR4.1、BR3.5: 画面は Manager の状態だけで出し分ける（data-testid で確かめる）。"""

    def test_closed_missing_file_shows_the_placement_guide(
        self, aw, client, closed_source
    ):
        _as(client, ADMIN_USER)
        html = client.get("/survey/persona-data").text
        assert 'data-testid="nemotron-download-button"' not in html
        assert "huggingface.co" not in html
        assert 'data-testid="nemotron-placed-notice"' in html
        assert 'data-testid="nemotron-placed-missing"' in html
        assert 'data-testid="nemotron-placement-guide"' in html
        assert NEMOTRON_KEY in html

    def test_closed_member_sees_no_key(self, aw, client, closed_source):
        """The S3 key is a file path: only admins (who place the file) see it."""
        _as(client, ME)
        html = client.get("/survey/persona-data").text
        assert 'data-testid="nemotron-placed-notice"' in html
        assert 'data-testid="nemotron-placement-guide"' in html
        assert NEMOTRON_KEY not in html

    def test_closed_placed_file_shows_its_size(self, aw, client, closed_source):
        aw.world.s3.upload_file(b"x" * (3 * 1024 * 1024), NEMOTRON_KEY)
        _as(client, ADMIN_USER)
        html = client.get("/survey/persona-data/nemotron-status").text
        assert 'data-testid="nemotron-placed-available"' in html
        assert "3.0 MB" in html
        assert 'data-testid="nemotron-download-button"' not in html

    def test_public_screen_keeps_the_download(self, aw, client):
        _as(client, ADMIN_USER)
        html = client.get("/survey/persona-data").text
        assert 'data-testid="nemotron-download-button"' in html
        assert "huggingface.co" in html
        assert 'data-testid="nemotron-placed-notice"' not in html

    def test_dwh_tab_follows_allowed(self, aw, client, monkeypatch):
        from src.config import config

        _as(client, ADMIN_USER)
        html = client.get("/survey/persona-data").text
        assert 'data-testid="persona-data-dwh-tab"' in html
        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", False)
        html = client.get("/survey/persona-data").text
        assert 'data-testid="persona-data-dwh-tab"' not in html
        assert 'id="dwh-condition"' not in html


class TestDwhExtractNotAllowed:
    def test_returns_one_error_event_without_extracting(self, aw, client, monkeypatch):
        from src.config import config
        from src.managers.survey_dataset_manager import SurveyDatasetManager

        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", False)
        _as(client, ADMIN_USER)
        with patch.object(SurveyDatasetManager, "extract_segment_from_dwh") as run:
            response = client.post(
                "/survey/persona-data/dwh-extract", data={"condition": "20代女性"}
            )
        assert response.status_code == 200
        events = [
            line for line in response.text.splitlines() if line.startswith("event: ")
        ]
        assert events == ["event: error"]
        run.assert_not_called()
