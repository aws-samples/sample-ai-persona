"""
議論ルーター（discussion.py）のテスト

U6: Router は利用者を Manager に渡すだけで判定しない。判定は本物の Manager・
Component（tests/api/u6_world.py）で確かめる。BR2.1〜BR2.5、BR4.1、BR4.2、
BR5.1、BR7.5、BR9.1、NFR1.43〜NFR1.47、NFR1.53、NFR1.56、NFR4.13、AC2.1.2、
AC2.1.3、AC4.1.1〜AC4.1.3、AC4.1.5、AC4.4.1、AC4.4.2、AC5.1.1〜AC5.1.3。
"""

import json
import re
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

from src.models.authz import AccessTarget, ResourceType
from src.models.discussion import Discussion
from src.models.errors import CodedError, ErrorCode
from tests.api.u6_world import ME, PEER, STRANGER, ApiWorld, _messages
from tests.dialog_helpers import dialog_markup

#: A full-page request (the client sends ``HX-Request`` by default).
PAGE = {"HX-Request": ""}
#: The browser's own ``EventSource`` / fetch (NFR4.13).
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def aw(client):
    """ME signed in, with the real U6 Managers behind the routers."""
    ME.login(client)
    world = ApiWorld()
    with ExitStack() as stack:
        world.patch_routers(stack)
        yield world


def _sse(response) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# =============================================================================
# 設定画面・資料のアップロード（今のまま）
# =============================================================================


class TestDiscussionSetupPage:
    """議論設定ページのテスト"""

    def test_setup_page_loads(self, client):
        response = client.get("/discussion/setup")
        assert response.status_code == 200
        assert "議論" in response.text

    @patch("web.routers.discussion.list_selectable_models")
    def test_setup_page_passes_selectable_models_to_template(
        self, mock_list_selectable_models, client
    ):
        """list_selectable_models(config.ENABLE_ADDITIONAL_PERSONA_MODELS) の結果を渡す（#107 R8）"""
        from src.models.model_registry import DEFAULT_MODEL_ID, SUPPORTED_MODELS

        mock_list_selectable_models.return_value = [SUPPORTED_MODELS[DEFAULT_MODEL_ID]]

        response = client.get("/discussion/setup")

        assert response.status_code == 200
        mock_list_selectable_models.assert_called_once()

    @patch("web.routers.discussion.config")
    def test_setup_page_uses_enable_additional_persona_models_config(
        self, mock_config, client
    ):
        mock_config.ENABLE_ADDITIONAL_PERSONA_MODELS = True
        assert client.get("/discussion/setup").status_code == 200

    def test_start_indicator_is_busy_and_announced(self, client):
        html = client.get("/discussion/setup").text

        loading = re.search(r'<div id="discussion-loading"[^>]*>', html)
        assert loading and 'aria-busy="true"' in loading.group(0)
        assert re.search(r'id="discussion-loading-status"[^>]*role="status"', html)
        assert "議論を開始しています" in html


def _metadata(file_id: str, filename: str, mime_type: str):
    from datetime import datetime

    from src.managers.file_manager import FileMetadata

    return FileMetadata(
        file_id=file_id,
        original_filename=filename,
        saved_filename=f"{file_id}_{filename}",
        file_path=f"s3://b/uploads/x/discussion_document/{file_id}_{filename}",
        file_size=1024,
        file_hash="abc123",
        mime_type=mime_type,
        uploaded_at=datetime.now(),
    )


class TestDiscussionDocumentUploadEndpoint:
    """議論用ドキュメントのアップロード（U5 のまま）"""

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_passes_the_signed_in_user(self, mock_get_manager, client):
        manager = mock_get_manager.return_value
        manager.upload_discussion_document.return_value = _metadata(
            "doc-1", "a.pdf", "application/pdf"
        )

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("a.pdf", b"%PDF-1.4\n", "application/pdf")},
        )

        assert response.status_code == 200
        principal, content, filename = manager.upload_discussion_document.call_args.args
        assert principal.sub == "local:dev@example.com"
        assert (content, filename) == (b"%PDF-1.4\n", "a.pdf")
        assert set(response.json()) == {
            "file_id",
            "filename",
            "file_size",
            "mime_type",
            "uploaded_at",
        }

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_invalid_format(self, mock_get_manager, client):
        from src.managers.file_manager import FileUploadError

        mock_get_manager.return_value.upload_discussion_document.side_effect = (
            FileUploadError(
                "extension '.txt' not in discussion document formats",
                code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
                context={"allowed_formats": ".png, .jpg, .jpeg, .pdf"},
            )
        )

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("test.txt", b"text content", "text/plain")},
        )

        assert response.status_code == 400
        assert "許可されていないファイル形式" in response.text
        assert ".png, .jpg, .jpeg, .pdf" in response.text

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_does_not_expose_internal_detail(
        self, mock_get_manager, client
    ):
        """内部例外の詳細がレスポンスに出ないことを確認（#112）"""
        from src.managers.file_manager import FileUploadError

        mock_get_manager.return_value.upload_discussion_document.side_effect = (
            FileUploadError(
                "upload failed (ClientError): arn:aws:s3:::internal-bucket/secret",
                code=ErrorCode.FILE_OPERATION_FAILED,
            )
        )

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("document.pdf", b"%PDF-1.4\n", "application/pdf")},
        )

        assert response.status_code == 400
        assert "arn:aws:s3" not in response.text
        assert "ClientError" not in response.text


# =============================================================================
# 一覧（BR2.1〜BR2.3、NFR1.43、AC4.1.1）
# =============================================================================


class TestResultsList:
    def test_tabs_counts_and_only_readable_rows(self, aw, client):
        aw.discussion(topic="自分の議論について")
        aw.discussion(PEER, topic="共有された議論について", shared_with=("sales",))
        aw.discussion(STRANGER, topic="無関係な議論について")

        html = client.get("/discussion/results", headers=PAGE).text
        assert "自分の議論について" in html
        assert "共有された議論について" not in html
        assert "無関係な議論について" not in html
        assert 'role="tablist"' in html
        assert "自分のもの" in html and "共有されたもの" in html

        shared = client.get(
            "/discussion/results?tab=shared", headers={"HX-Target": "discussion-list"}
        ).text
        assert "共有された議論について" in shared
        assert "自分の議論について" not in shared
        assert "無関係な議論について" not in shared

    def test_member_asking_for_all_gets_mine(self, aw, client):
        aw.discussion(STRANGER, topic="無関係な議論について")
        html = client.get("/discussion/results?tab=all", headers=PAGE).text
        assert "無関係な議論について" not in html

    def test_zero_rows(self, aw, client):
        html = client.get("/discussion/results", headers=PAGE).text
        assert "議論がありません" in html

    def test_search_and_mode_inside_the_tab(self, aw, client):
        aw.discussion(topic="価格の見直しについて")
        aw.discussion(topic="広告の出し方について", mode="interview")

        found = client.get(
            "/discussion/results?search=価格", headers={"HX-Target": "discussion-list"}
        ).text
        assert "価格の見直しについて" in found
        assert "広告の出し方について" not in found

        interviews = client.get(
            "/discussion/results?mode=interview",
            headers={"HX-Target": "discussion-list"},
        ).text
        assert "広告の出し方について" in interviews
        assert "価格の見直しについて" not in interviews

    def test_rows_show_the_stored_names_and_counts(self, aw, client):
        aw.discussion(topic="自分の議論について")
        html = client.get("/discussion/results", headers=PAGE).text
        assert "佐藤" in html and "鈴木" in html
        assert "2件の発言" in html

    def test_only_manageable_rows_offer_delete_and_share(self, aw, client):
        aw.discussion(PEER, topic="共有された議論について", shared_with=("sales",))
        shared = client.get(
            "/discussion/results?tab=shared", headers={"HX-Target": "discussion-list"}
        ).text
        assert "hx-delete" not in shared
        assert "data-share-url" not in shared


# =============================================================================
# 詳細・部分表示・資料・エクスポート（BR2.4、BR5.1、BR7.3、BR9.1、NFR1.44）
# =============================================================================


class TestDetail:
    def test_owner_sees_the_operations(self, aw, client):
        discussion = aw.discussion()
        html = client.get(f"/discussion/{discussion.id}", headers=PAGE).text
        assert discussion.topic in html
        assert f'hx-delete="/discussion/{discussion.id}"' in html
        assert f'data-share-url="/share/discussion/{discussion.id}/dialog"' in html
        assert "regenerate-insights-dialog-open-button" in html
        assert f"/discussion/{discussion.id}/export" in html

    def test_shared_reader_sees_no_operations(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        html = client.get(f"/discussion/{discussion.id}", headers=PAGE).text
        assert discussion.topic in html
        assert "hx-delete" not in html
        assert "data-share-url" not in html
        assert "regenerate-insights-dialog-open-button" not in html
        assert "var reportManageable = false;" in html
        # 読める人はエクスポートできる
        assert f"/discussion/{discussion.id}/export" in html

    def test_unrelated_user_gets_not_found_everywhere(self, aw, client):
        discussion = aw.discussion(STRANGER)
        assert (
            client.get(f"/discussion/{discussion.id}", headers=PAGE).status_code == 404
        )
        for path in (
            f"/discussion/result-partial/{discussion.id}",
            f"/discussion/insights/{discussion.id}",
            f"/discussion/{discussion.id}/export",
        ):
            assert client.get(path).status_code == 404, path

    def test_participant_states_are_text_not_links(self, aw, client):
        world = aw.world
        discussion = Discussion.create_new(
            topic="参加者の状態について",
            participants=["p1", "p2", "p3"],
            mode="classic",
            participant_names={"p1": "佐藤", "p2": "鈴木", "p3": "田中"},
        )
        saved = world.records.save_new(ME.principal(), discussion, _messages(world))
        world.personas.targets["p2"] = AccessTarget(
            ResourceType.PERSONA, "p2", owner_sub="b", exists=False
        )

        html = client.get(f"/discussion/{saved.id}", headers=PAGE).text

        assert 'href="/persona/p1"' in html
        assert "〔削除済み〕" in html
        assert "〔参照権限なし〕" in html
        assert 'href="/persona/p2"' not in html
        assert 'href="/persona/p3"' not in html

    def test_models_are_shown_by_name(self, aw, client):
        world = aw.world
        discussion = Discussion.create_new(
            topic="モデル選択のテストです",
            participants=["p1", "p2"],
            mode="agent",
            participant_names={"p1": "佐藤", "p2": "鈴木"},
            agent_config={
                "rounds": 3,
                "persona_models": {"p1": "openai.gpt-5.6-terra"},
                "facilitator_model": "global.anthropic.claude-sonnet-5",
            },
        )
        saved = world.records.save_new(ME.principal(), discussion, _messages(world))

        html = client.get(f"/discussion/{saved.id}", headers=PAGE).text

        assert "GPT-5.6 Terra" in html
        assert "Claude Sonnet 5" in html

    def test_document_urls_come_through_file_attachments(self, aw, client):
        from src.models.discussion import DocumentRef

        discussion = aw.discussion()
        aw.world.store.bodies[discussion.id]["documents"] = [
            DocumentRef("f1", "a.png", "image/png", 1, "2026-10-15").to_dict()
        ]
        aw.world.files.readable_file.return_value = Mock(file_path="s3://b/k")

        html = client.get(f"/discussion/{discussion.id}", headers=PAGE).text

        assert "https://signed" in html
        principal, file_id = aw.world.files.readable_file.call_args.args
        assert (principal.sub, file_id) == (ME.sub, "f1")

    def test_result_partial_and_insights_for_readers(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        assert (
            client.get(f"/discussion/result-partial/{discussion.id}").status_code == 200
        )
        insights = client.get(f"/discussion/insights/{discussion.id}")
        assert insights.status_code == 200
        assert "regenerate-insights-dialog-open-button" not in insights.text


class TestExport:
    def test_readers_get_markdown_as_an_attachment(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.get(f"/discussion/{discussion.id}/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.headers["content-disposition"].startswith("attachment;")
        assert discussion.topic in response.text
        assert PEER.sub not in response.text


# =============================================================================
# 開始（BR2.8、BR7.5、NFR1.46、M12）
# =============================================================================


def _start(client, *persona_ids, **data):
    form = {
        "topic": "新商品のマーケティングについて",
        "persona_ids": list(persona_ids),
        "mode": "traditional",
    }
    form.update(data)
    return client.post("/discussion/start", data=form)


class TestStart:
    def test_classic_discussion_is_saved_as_mine(self, aw, client):
        response = _start(client, "p1", "p2")
        assert response.status_code == 200
        (body,) = aw.world.store.bodies.values()
        assert body["owner_sub"] == ME.sub
        assert body["topic"] in response.text

    def test_unreadable_persona_shows_the_stop_panel(self, aw, client):
        response = _start(client, "p1", "p3")
        assert response.status_code == 404
        assert response.headers.get("X-Render-Response") == "true"
        assert 'role="alert"' in response.text
        assert "ページが見つかりません" in response.text
        aw.ai.facilitate_discussion.assert_not_called()
        assert aw.world.store.bodies == {}

    def test_too_few_personas(self, aw, client):
        response = _start(client, "p1")
        assert response.status_code == 400

    def test_interview_mode_is_refused(self, aw, client):
        response = _start(client, "p1", "p2", mode="interview")
        assert response.status_code == 400

    def test_agent_mode_passes_models(self, aw, client):
        from src.config import config

        run = Mock(wraps=aw.agent_discussions.run_agent_discussion_full)
        with (
            patch.object(aw.agent_discussions, "run_agent_discussion_full", run),
            patch.object(config, "ENABLE_ADDITIONAL_PERSONA_MODELS", True),
        ):
            response = _start(
                client,
                "p1",
                "p2",
                mode="agent",
                rounds=1,
                **{
                    "persona_models[p1]": "openai.gpt-5.6-terra",
                    "facilitator_model": "global.anthropic.claude-sonnet-5",
                },
            )
        assert response.status_code == 200, response.text
        principal, personas, topic = run.call_args.args
        assert principal.sub == ME.sub
        assert [p.id for p in personas] == ["p1", "p2"]
        assert run.call_args.kwargs["persona_models"] == {"p1": "openai.gpt-5.6-terra"}
        assert (
            run.call_args.kwargs["facilitator_model"]
            == "global.anthropic.claude-sonnet-5"
        )

    def test_someone_elses_document_is_not_found(self, aw, client):
        from src.models.errors import CodedError

        aw.world.files.attachable_file.side_effect = CodedError(
            "no", code=ErrorCode.FILE_NOT_FOUND
        )
        response = _start(client, "p1", "p2", document_ids=["someone-elses"])
        assert response.status_code == 404
        assert "showToast" in response.headers["HX-Trigger"]
        aw.ai.facilitate_discussion.assert_not_called()


class TestStream:
    def _params(self, **extra):
        params = {
            "topic": "新商品のマーケティングについて",
            "persona_ids": "p1,p2",
        }
        params.update(extra)
        return params

    @pytest.mark.parametrize("site", [None, "cross-site", "same-site", "none"])
    def test_other_sites_are_refused(self, aw, client, site):
        headers = {"Sec-Fetch-Site": site} if site else {}
        response = client.get(
            "/discussion/stream", params=self._params(), headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == ErrorCode.CSRF_FAILED.value
        aw.ai.facilitate_discussion_streaming.assert_not_called()

    def test_same_origin_runs_and_saves(self, aw, client):
        response = client.get(
            "/discussion/stream", params=self._params(), headers=SAME_ORIGIN
        )
        assert response.status_code == 200
        events = _sse(response)
        assert events[-1]["type"] == "complete"
        assert events[-1]["discussion_id"] in aw.world.store.bodies

    def test_unreadable_persona_stops(self, aw, client):
        response = client.get(
            "/discussion/stream",
            params=self._params(persona_ids="p1,p3"),
            headers=SAME_ORIGIN,
        )
        assert [e["type"] for e in _sse(response)] == ["stopped"]
        aw.ai.facilitate_discussion_streaming.assert_not_called()

    def test_agent_mode_passes_persona_models_json(self, aw, client):
        run = Mock(return_value=iter([]))
        with patch.object(aw.agent_discussions, "run_agent_discussion_streaming", run):
            response = client.get(
                "/discussion/stream",
                params=self._params(
                    mode="agent",
                    persona_models_json=json.dumps({"p1": "openai.gpt-5.6-terra"}),
                    facilitator_model="global.anthropic.claude-sonnet-5",
                ),
                headers=SAME_ORIGIN,
            )
        assert response.status_code == 200
        assert run.call_args.kwargs["persona_models"] == {"p1": "openai.gpt-5.6-terra"}
        assert (
            run.call_args.kwargs["facilitator_model"]
            == "global.anthropic.claude-sonnet-5"
        )


# =============================================================================
# 管理できる人だけの操作（BR2.5、BR4.2、NFR1.45）
# =============================================================================


class TestManageOnly:
    def test_shared_reader_cannot_delete(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.delete(f"/discussion/{discussion.id}")
        assert response.status_code == 403
        assert discussion.id in aw.world.store.bodies

    def test_owner_deletes(self, aw, client):
        discussion = aw.discussion()
        assert client.delete(f"/discussion/{discussion.id}").status_code == 200
        assert discussion.id not in aw.world.store.bodies

    def test_unrelated_delete_is_not_found(self, aw, client):
        discussion = aw.discussion(STRANGER)
        assert client.delete(f"/discussion/{discussion.id}").status_code == 404

    def test_shared_reader_cannot_regenerate(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.post(f"/discussion/{discussion.id}/regenerate-insights")
        assert response.status_code == 403
        aw.ai.extract_insights.reset_mock()
        aw.ai.extract_insights.assert_not_called()

    def test_regenerate_is_a_dialog_that_closes_on_success(self, aw, client):
        discussion = aw.discussion()
        html = client.post(
            f"/discussion/{discussion.id}/regenerate-insights",
            data={
                "categories[0][name]": "顧客ニーズ",
                "categories[0][description]": "ニーズ",
            },
        ).text

        outer = dialog_markup(html, "regenerate-insights-dialog")
        assert "data-dialog-close-on-success" in outer
        assert "data-dialog-backdrop-close" not in outer
        assert 'data-dialog-open="regenerate-insights-dialog"' in html
        assert f'hx-post="/discussion/{discussion.id}/regenerate-insights"' in html

    def test_shared_reader_cannot_save_edit_or_delete_reports(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        report = aw.world.records.save_report(
            PEER, discussion.id, "summary", "内容", None
        )
        base = f"/discussion/{discussion.id}/report"
        assert (
            client.post(
                f"{base}/save", data={"template_type": "summary", "content": "x"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{base}/{report.report_id}/update-content", data={"content": "x"}
            ).status_code
            == 403
        )
        assert client.delete(f"{base}/{report.report_id}").status_code == 403
        assert aw.world.store.bodies[discussion.id]["report_count"] == 1

    @pytest.mark.parametrize("operation", ["update", "delete"])
    def test_a_failed_report_change_is_a_toast(
        self, aw, client, monkeypatch, operation
    ):
        """再試行で直る失敗は、画面を書き換えずにトーストで知らせる（error-catalog.md）。"""
        discussion = aw.discussion()
        report = aw.world.records.save_report(
            ME.principal(), discussion.id, "summary", "内容", None
        )

        def failed(*_a, **_k):
            raise CodedError("store down", code=ErrorCode.REPORT_OPERATION_FAILED)

        monkeypatch.setattr(aw.world.records, f"{operation}_report", failed)
        url = f"/discussion/{discussion.id}/report/{report.report_id}"
        if operation == "update":
            response = client.post(f"{url}/update-content", data={"content": "x"})
        else:
            response = client.delete(url)

        assert response.status_code == 400
        assert "showToast" in response.headers["HX-Trigger"]
        assert "store down" not in response.text

    @pytest.mark.parametrize("operation", ["update", "delete"])
    def test_a_correctable_report_error_reaches_the_screen(
        self, aw, client, monkeypatch, operation
    ):
        """直せる失敗の 400 の本文は mark_renderable の印付き（htmx が差し替える）。"""
        discussion = aw.discussion()
        report = aw.world.records.save_report(
            ME.principal(), discussion.id, "summary", "内容", None
        )

        def too_big(*_a, **_k):
            raise CodedError("too big", code=ErrorCode.REPORT_CAPACITY_EXCEEDED)

        monkeypatch.setattr(aw.world.records, f"{operation}_report", too_big)
        url = f"/discussion/{discussion.id}/report/{report.report_id}"
        if operation == "update":
            response = client.post(f"{url}/update-content", data={"content": "x"})
        else:
            response = client.delete(url)

        assert response.status_code == 400
        assert response.headers.get("X-Render-Response") == "true"
        assert "too big" not in response.text

    def test_report_ids_are_assigned_by_the_server(self, aw, client):
        discussion = aw.discussion()
        response = client.post(
            f"/discussion/{discussion.id}/report/save",
            data={
                "report_id": "chosen-by-the-client",
                "template_type": "summary",
                "content": "レポートの内容",
            },
        )
        assert response.status_code == 200
        (report_id,) = aw.world.store.reports[discussion.id]
        assert report_id != "chosen-by-the-client"
        assert "chosen-by-the-client" not in response.text

    def test_fourth_report_is_refused_with_the_preview(self, aw, client):
        discussion = aw.discussion()
        for i in range(3):
            aw.world.records.save_report(
                ME.principal(), discussion.id, "summary", f"r{i}", None
            )
        response = client.post(
            f"/discussion/{discussion.id}/report/save",
            data={"template_type": "summary", "content": "4件目"},
        )
        # 入力したレポートは消さず、プレビューのまま保存できなかったことを示す
        assert response.status_code == 200
        assert "4件目" in response.text
        assert "data-report-save-error" in response.text
        assert aw.world.store.bodies[discussion.id]["report_count"] == 3


# =============================================================================
# レポートの参照・生成（BR4.1、BR4.3）
# =============================================================================


class TestReports:
    def test_readers_open_and_export_reports(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        report = aw.world.records.save_report(
            PEER, discussion.id, "summary", "# 見出し\n本文", None
        )
        base = f"/discussion/{discussion.id}/report/{report.report_id}"

        opened = client.get(base)
        assert opened.status_code == 200
        assert "saved-update-btn" not in opened.text

        exported = client.get(f"{base}/export?format=txt")
        assert exported.status_code == 200
        assert exported.text == "見出し\n本文"
        assert exported.headers["content-disposition"].startswith("attachment;")

    def test_missing_report_is_not_found(self, aw, client):
        discussion = aw.discussion()
        response = client.get(f"/discussion/{discussion.id}/report/nonexistent")
        assert response.status_code == 404

    def test_generate_needs_same_origin(self, aw, client):
        discussion = aw.discussion()
        url = f"/discussion/{discussion.id}/report/generate"
        assert client.get(url).status_code == 403
        response = client.get(url, headers=SAME_ORIGIN)
        assert [e["type"] for e in _sse(response)][-1] == "done"

    def test_shared_reader_may_generate(self, aw, client):
        discussion = aw.discussion(PEER, shared_with=("sales",))
        response = client.get(
            f"/discussion/{discussion.id}/report/generate", headers=SAME_ORIGIN
        )
        events = _sse(response)
        assert [e["content"] for e in events if e["type"] == "chunk"] == [
            "レポート",
            "本文",
        ]

    def test_unrelated_user_cannot_generate(self, aw, client):
        discussion = aw.discussion(STRANGER)
        response = client.get(
            f"/discussion/{discussion.id}/report/generate", headers=SAME_ORIGIN
        )
        assert _sse(response)[-1]["type"] == "error"
        aw.ai.generate_standard_report_streaming.assert_not_called()

    def test_invalid_template_type(self, aw, client):
        discussion = aw.discussion()
        response = client.get(
            f"/discussion/{discussion.id}/report/generate?template_type=poem",
            headers=SAME_ORIGIN,
        )
        assert _sse(response)[-1]["type"] == "error"

    def test_followup_uses_the_users_own_session(self, aw, client):
        from src.managers.report_manager import report_session_id

        discussion = aw.discussion()
        with patch("src.managers.report_manager.service_factory") as factory:
            factory.check_stm_session_exists.return_value = False
            response = client.post(
                f"/discussion/{discussion.id}/report/generate-followup",
                data={
                    "followup_prompt": "追加で分析して",
                    "previous_report": "前回",
                    "session_id": "someone-elses-session",
                },
            )
        assert response.status_code == 200
        kwargs = aw.agent_service.run_report_agent_streaming.call_args.kwargs
        assert kwargs["session_id"] == report_session_id(discussion.id, ME.sub)
