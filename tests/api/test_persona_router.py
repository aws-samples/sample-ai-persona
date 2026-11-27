"""
Persona router (persona.py) tests.

Covers file upload and the persona generate, save, edit and delete endpoints.
"""

import html
import json
import threading
from dataclasses import replace
from unittest.mock import Mock, patch
from io import BytesIO

import pytest

from src.managers.file_manager import FileUploadError
from src.managers.persona_manager import PersonaManagerError
from src.models.errors import ErrorCode
from web.error_messages import user_message_for_code
from tests.dialog_helpers import dialog_ids, dialog_markup


def _listing(personas, tab="mine"):
    """A TabbedList the mocked PersonaManager returns."""
    from src.models.sharing import ListTab, TabbedList

    return TabbedList(
        tab=ListTab(tab),
        items=list(personas),
        counts={"mine": len(personas), "shared": 0},
        manageable=frozenset(p.id for p in personas),
    )


def _detail(persona, manageable=True):
    from src.managers.persona_manager import PersonaDetail

    return PersonaDetail(persona, manageable=manageable, owner_label="自分")


def _not_found():
    from src.models.errors import CodedError

    return CodedError("not readable", code=ErrorCode.PERSONA_NOT_FOUND)


class TestPersonaGenerationPage:
    """Persona generation page tests"""

    def test_generation_page_loads(self, client):
        """Verify the persona generation page loads"""
        response = client.get("/persona/generation")

        assert response.status_code == 200
        assert "AIペルソナ生成" in response.text or "ペルソナ" in response.text

    def test_dwh_choice_is_hidden_when_not_allowed(self, client, monkeypatch):
        """allowed=false hides 「データ分析エージェント連携」 (closed network)"""
        from src.config import config

        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", False)
        html = client.get("/persona/generation").text
        assert 'data-testid="generation-data-type-dwh"' not in html
        assert 'value="dwh"' not in html

    def test_dwh_choice_shows_by_default_even_without_an_arn(self, client, monkeypatch):
        """By default (public) it still shows even when the ARN is unset"""
        from src.config import config

        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", True)
        monkeypatch.setattr(config, "ENABLE_DATA_AGENT", False)
        monkeypatch.setattr(config, "DATA_AGENT_RUNTIME_ARN", None)
        html = client.get("/persona/generation").text
        assert 'data-testid="generation-data-type-dwh"' in html


def _advanced_available(available: bool):
    """Patch the tab strip's availability check."""
    manager = Mock()
    manager.is_available.return_value = available
    return patch(
        "web.routers.persona_advanced.get_advanced_generation_manager",
        return_value=manager,
    )


class TestGenerationTabs:
    """Tab strip on the regular generation page (advanced generation)."""

    def test_available_user_sees_both_tabs_with_the_current_one_marked(self, client):
        import re

        with _advanced_available(True):
            html = client.get("/persona/generation").text

        assert 'data-testid="generation-tabs"' in html
        standard = re.search(
            r'<a\b[^>]*data-testid="generation-tab-standard"[^>]*>', html
        )
        advanced = re.search(
            r'<a\b[^>]*data-testid="generation-tab-advanced"[^>]*>', html
        )
        assert standard and 'aria-current="page"' in standard.group(0)
        assert advanced and "aria-current" not in advanced.group(0)
        assert "ベータ" in html
        # The regular generation form is unchanged
        assert 'id="generate-form"' in html

    def test_unavailable_user_sees_no_tab_strip(self, client):
        with _advanced_available(False):
            html = client.get("/persona/generation").text

        assert 'data-testid="generation-tabs"' not in html
        assert "/persona/generation/advanced" not in html
        assert 'id="generate-form"' in html

    def test_back_from_the_advanced_page_shows_the_notice_toast(self, client):
        with _advanced_available(False):
            html = client.get("/persona/generation?notice=advanced_unavailable").text

        message = user_message_for_code(ErrorCode.ADVANCED_GENERATION_UNAVAILABLE.value)
        assert "showFlashMessage" in html
        assert json.dumps(message) in html

    def test_unknown_notice_shows_nothing(self, client):
        with _advanced_available(False):
            html = client.get("/persona/generation?notice=<b>x</b>").text

        assert "showFlashMessage(" not in html
        assert "<b>x</b>" not in html

    def test_availability_failure_keeps_the_ordinary_page(self, client):
        manager = Mock()
        manager.is_available.side_effect = RuntimeError("settings down")
        with patch(
            "web.routers.persona_advanced.get_advanced_generation_manager",
            return_value=manager,
        ):
            response = client.get("/persona/generation")

        assert response.status_code == 200
        assert 'data-testid="generation-tabs"' not in response.text
        assert "settings down" not in response.text


class TestAdvancedPersonaDetail:
    """Badge and analysis summary on the detail page of a persona made by advanced generation."""

    def _persona(self, sample_persona, segment_id="S1", unverified=()):
        from tests.advanced_screen_samples import advanced_context

        return replace(
            sample_persona,
            generation_context=advanced_context(segment_id, unverified=unverified),
        )

    @patch("web.routers.persona.get_persona_manager")
    def test_badge_summary_and_record_are_shown(
        self, mock_get_manager, client, sample_persona
    ):
        from tests.advanced_screen_samples import SCRIPT_TEXT, record

        persona = self._persona(sample_persona)
        manager = mock_get_manager.return_value
        manager.get_persona_detail.return_value = _detail(persona)
        manager.get_analysis_record.return_value = (record(), ())

        response = client.get(f"/persona/{persona.id}")

        assert response.status_code == 200
        html_text = response.text
        assert 'data-testid="persona-advanced-badge"' in html_text
        assert "高度な生成（ベータ）で作成" in html_text
        assert "クラスタ分析" in html_text
        assert "全体の 50%" in html_text
        # the segment is named, never shown by its internal ID
        summary = html_text.split('data-testid="persona-advanced-summary"')[1][:900]
        assert "若い学生" in summary and "S1 " not in summary
        assert "分析の詳細" in html_text and "分析の記録" not in html_text
        # the button stands among the header's actions, like 生成ログ・評価
        assert 'data-testid="persona-analysis-detail-button"' in html_text
        actions = html_text.split('data-testid="persona-detail-actions"')[1]
        assert actions.index("persona-analysis-detail-button") < actions.index(
            "persona-export-button"
        )
        assert 'data-testid="advanced-record"' in html_text
        assert SCRIPT_TEXT not in html_text
        assert manager.get_analysis_record.call_args.args[1] == persona.id

    @pytest.mark.parametrize(
        ("state", "shown", "hidden"),
        [
            ("deleted", "元のファイルは消えています", "元のファイルの記録はありません"),
            ("unknown", "元のファイルの記録はありません", "元のファイルは消えています"),
            ("available", "people.csv", "元のファイルは消えています"),
        ],
    )
    @patch("web.routers.persona.get_persona_manager")
    def test_record_says_whether_the_source_file_is_still_there(
        self, mock_get_manager, client, sample_persona, state, shown, hidden
    ):
        """The plan stays, the file's fate is told in words."""
        from src.models.source_file import (
            SourceAvailability,
            SourceAvailabilityState,
        )
        from tests.advanced_screen_samples import record

        persona = self._persona(sample_persona)
        manager = mock_get_manager.return_value
        manager.get_persona_detail.return_value = _detail(persona)
        known = state != "unknown"
        found = record().update(
            source_file_ids=("file-1",) if known else (),
            source_display_names=("people.csv",) if known else (),
        )
        manager.get_analysis_record.return_value = (
            found,
            (
                SourceAvailability(
                    "file-1" if known else "",
                    "people.csv" if known else "",
                    SourceAvailabilityState(state),
                ),
            ),
        )

        html_text = client.get(f"/persona/{persona.id}").text

        assert shown in html_text
        assert hidden not in html_text
        assert "クラスタ分析" in html_text  # the plan is shown either way

    @patch("web.routers.persona.get_persona_manager")
    def test_unverified_segment_share_is_not_shown(
        self, mock_get_manager, client, sample_persona
    ):
        persona = self._persona(sample_persona, segment_id="S2", unverified=("S2",))
        manager = mock_get_manager.return_value
        manager.get_persona_detail.return_value = _detail(persona)
        manager.get_analysis_record.return_value = None

        html_text = client.get(f"/persona/{persona.id}").text

        assert "全体の 50%" not in html_text
        assert "分析の詳細は残っていません" in html_text

    @patch("web.routers.persona.get_persona_manager")
    def test_unreadable_record_still_shows_the_detail(
        self, mock_get_manager, client, sample_persona
    ):
        persona = self._persona(sample_persona)
        manager = mock_get_manager.return_value
        manager.get_persona_detail.return_value = _detail(persona)
        manager.get_analysis_record.side_effect = PersonaManagerError(
            "s3 read failed", code=ErrorCode.GENERATION_OPERATION_FAILED
        )

        response = client.get(f"/persona/{persona.id}")

        assert response.status_code == 200
        assert 'data-testid="persona-advanced-badge"' in response.text
        assert "分析の詳細は残っていません" in response.text
        assert "s3 read failed" not in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_ordinary_persona_has_no_badge_and_no_record_read(
        self, mock_get_manager, client, sample_persona
    ):
        manager = mock_get_manager.return_value
        manager.get_persona_detail.return_value = _detail(sample_persona)

        html_text = client.get(f"/persona/{sample_persona.id}").text

        assert 'data-testid="persona-advanced-badge"' not in html_text
        manager.get_analysis_record.assert_not_called()
        assert "persona-analysis-detail-button" not in html_text


class TestPersonaManagementPage:
    """Persona management page tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_management_page_with_personas(
        self, mock_get_manager, client, sample_persona
    ):
        """The first page is rendered on the server together with the tabs"""
        mock_manager = Mock()
        mock_manager.list_personas.return_value = _listing([sample_persona])
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/management")

        assert response.status_code == 200
        assert "田中花子" in response.text
        assert 'data-testid="list-tabs"' in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_list_failure_keeps_the_page_and_shows_dash_counts(
        self, mock_get_manager, client
    ):
        mock_manager = Mock()
        mock_manager.list_personas.side_effect = PersonaManagerError(
            "down", code=ErrorCode.PERSONA_OPERATION_FAILED
        )
        mock_get_manager.return_value = mock_manager

        page = client.get("/persona/management")
        assert page.status_code == 200
        assert 'data-count-label="自分のもの -"' in page.text
        panel = client.get("/persona/management", headers={"HX-Target": "persona-list"})
        assert panel.status_code == 503
        assert panel.headers["X-Render-Response"] == "true"


class TestPersonaGenerateEndpoint:
    """Persona generation endpoint tests"""

    @pytest.fixture(autouse=True)
    def mock_file_manager(self):
        """generate_persona builds a FileManager via get_file_manager(), and its
        constructor tries a real DynamoDB connection. That fails on CI without
        credentials, so FileManager is replaced with a mock. The default
        validate_persona_source_file returns None (validation passes), so the happy
        path works as is. Rejection tests set side_effect on the returned mock."""
        with patch("web.routers.persona.get_file_manager") as mock_get_fm:
            mock_fm = Mock()
            mock_fm.validate_persona_source_file.return_value = None
            mock_get_fm.return_value = mock_fm
            yield mock_fm

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_success(self, mock_get_gen_manager, client, sample_persona):
        """Verify persona generation succeeds (SSE)"""
        from src.managers.persona_generation_manager import GenerationResult

        mock_manager = Mock()
        mock_manager.generate.return_value = GenerationResult([sample_persona], [])
        mock_get_gen_manager.return_value = mock_manager

        file_content = (
            "十分な長さのインタビューテキスト。これはテスト用のテキストです。" * 5
        )
        files = [
            (
                "files",
                ("interview.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert "event: result" in response.text
        # Make the generating user the owner of the candidate
        (principal,) = mock_manager.generate.call_args.args
        assert principal.sub == "local:dev@example.com"
        # The candidate ID shown on screen is candidate_id (sent back on save)
        assert f'value="{sample_persona.id}"' in response.text
        # A generation that finishes quickly returns its result without waiting for keepalive
        assert "event: keepalive" not in response.text

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_sends_keepalives_while_running(
        self, mock_get_gen_manager, client, sample_persona, monkeypatch
    ):
        """Send keepalive until generation finishes, then return the result."""
        import time

        import web.routers.persona as persona_router
        from src.managers.persona_generation_manager import GenerationResult

        monkeypatch.setattr(persona_router, "_KEEPALIVE_SECONDS", 0.01)

        def slow_generate(*args, **kwargs):
            time.sleep(0.2)
            return GenerationResult([sample_persona], [])

        mock_manager = Mock()
        mock_manager.generate.side_effect = slow_generate
        mock_get_gen_manager.return_value = mock_manager

        content = "十分な長さのインタビューテキスト。これはテスト用のテキストです。" * 5
        response = client.post(
            "/persona/generate",
            files=[
                ("files", ("i.txt", BytesIO(content.encode("utf-8")), "text/plain"))
            ],
            data={"data_type": "interview", "persona_count": 1},
        )

        assert response.status_code == 200
        text = response.text
        assert "event: keepalive" in text
        assert text.index("event: keepalive") < text.index("event: result")

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_rejects_unsupported_extension(
        self, mock_get_gen_manager, client, mock_file_manager
    ):
        """An unsupported extension (.exe) is rejected before extraction and the generation manager is not called (SSE)"""
        mock_manager = Mock()
        mock_get_gen_manager.return_value = mock_manager
        # Make the router's validation raise FileUploadError to reproduce the rejection path
        mock_file_manager.validate_persona_source_file.side_effect = FileUploadError(
            "extension not allowed", code=ErrorCode.FILE_FORMAT_NOT_ALLOWED
        )

        files = [
            ("files", ("malware.exe", BytesIO(b"anything at all here"), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text
        mock_manager.generate.assert_not_called()

    @patch("web.routers.persona.get_persona_manager")
    def test_generate_empty_text(self, mock_get_manager, client):
        """Verify an empty file returns an error (SSE)"""
        files = [
            ("files", ("empty.txt", BytesIO(b""), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_generate_invalid_count(self, mock_get_manager, client):
        """Verify an invalid persona count returns an error (SSE)"""
        file_content = "テスト用テキスト。" * 10
        files = [
            (
                "files",
                ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 0,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_multiple_success(
        self, mock_get_gen_manager, client, sample_persona, sample_persona_2
    ):
        """Verify multi-persona generation succeeds (SSE)"""
        from src.managers.persona_generation_manager import GenerationResult

        mock_manager = Mock()
        mock_manager.generate.return_value = GenerationResult(
            [sample_persona, sample_persona_2],
            [{"type": "thinking", "content": "分析中..."}],
        )
        mock_get_gen_manager.return_value = mock_manager

        file_content = "これは市場調査レポートです。" * 50
        files = [
            (
                "files",
                ("report.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "market_report",
                "persona_count": 2,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: result" in response.text

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_failed_generation_reads_the_awaited_exception(
        self, mock_get_gen_manager, client, monkeypatch
    ):
        """The failure is read from the awaited future.

        An unread one makes asyncio log 'Future exception was never retrieved'
        with the exception text when it is collected.
        """
        import asyncio

        import web.routers.persona as persona_router
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        wrapped: list[asyncio.Future] = []
        original_wrap_future = asyncio.wrap_future

        def recording_wrap_future(future, *args, **kwargs):
            waiting = original_wrap_future(future, *args, **kwargs)
            wrapped.append(waiting)
            return waiting

        monkeypatch.setattr(
            persona_router.asyncio, "wrap_future", recording_wrap_future
        )
        mock_manager = Mock()
        mock_manager.generate.side_effect = PersonaGenerationManagerError("secret")
        mock_get_gen_manager.return_value = mock_manager

        content = "十分な長さのインタビューテキスト。これはテスト用のテキストです。" * 5
        response = client.post(
            "/persona/generate",
            files=[
                ("files", ("i.txt", BytesIO(content.encode("utf-8")), "text/plain"))
            ],
            data={"data_type": "interview", "persona_count": 1},
        )

        assert "event: error" in response.text
        assert len(wrapped) == 1
        # asyncio clears _log_traceback once the exception has been read
        assert wrapped[0]._log_traceback is False

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_manager_error(self, mock_get_gen_manager, client):
        """Verify PersonaGenerationManagerError is handled properly (SSE)"""
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        mock_manager = Mock()
        mock_manager.generate.side_effect = PersonaGenerationManagerError("生成エラー")
        mock_get_gen_manager.return_value = mock_manager

        file_content = "テスト用テキスト。" * 10
        files = [
            (
                "files",
                ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_dwh_streams_agent_events_unbuffered(
        self, mock_get_gen_manager, client, sample_persona
    ):
        """DWH generation streams the agent's progress and keeps the proxy from buffering (SSE)"""
        from src.managers.persona_generation_manager import GenerationResult

        def fake_generate(principal, **kwargs):
            kwargs["event_queue"].put(
                {"type": "tool_call", "content": "query_dwh", "detail": "SELECT 1"}
            )
            return GenerationResult([sample_persona], [])

        mock_manager = Mock()
        mock_manager.generate.side_effect = fake_generate
        mock_get_gen_manager.return_value = mock_manager

        response = client.post(
            "/persona/generate",
            data={
                "data_type": "dwh",
                "persona_count": 1,
                "analysis_angle": "購買頻度",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        # The middleware in web/main.py normalizes Cache-Control to no-store
        assert response.headers["X-Accel-Buffering"] == "no"
        assert "query_dwh" in response.text
        assert "event: result" in response.text

    def test_dwh_generation_not_allowed_returns_one_error_event(
        self, client, monkeypatch
    ):
        """Reject before streaming starts and do not call generation (closed network)"""
        from src.config import config
        from src.managers.persona_generation_manager import PersonaGenerationManager

        monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", False)
        real = PersonaGenerationManager(
            agent_service=Mock(),
            dataset_analysis_service=Mock(),
            candidate_store=Mock(),
            s3_service=None,
        )
        with (
            patch(
                "web.routers.persona.get_persona_generation_manager",
                return_value=real,
            ),
            patch.object(real, "generate") as generate,
        ):
            response = client.post(
                "/persona/generate",
                data={
                    "data_type": "dwh",
                    "persona_count": 1,
                    "analysis_angle": "購買頻度",
                    "custom_prompt": "",
                },
            )

        assert response.status_code == 200
        events = [
            line for line in response.text.splitlines() if line.startswith("event: ")
        ]
        assert events == ["event: error"]
        generate.assert_not_called()


class TestPersonaSaveEndpoint:
    """Persona save endpoint tests"""

    _FORM = {
        "persona_id": "cand-1",
        "name": "テストペルソナ",
        "age": 30,
        "occupation": "エンジニア",
        "background": "テスト背景",
        "values": "価値観1\n価値観2",
        "pain_points": "課題1\n課題2",
        "goals": "目標1\n目標2",
    }

    @staticmethod
    def _managers(mock_get_manager, mock_get_gen_manager, result=None):
        from src.models.persona_candidate import CandidateSaveResult

        calls = []
        gen = Mock()
        gen.claim_candidates.side_effect = lambda *a, **k: calls.append("claim") or (
            "the-claim"
        )
        gen.finish_candidates.side_effect = lambda *a, **k: calls.append("finish")
        manager = Mock()
        saved = result or CandidateSaveResult(
            saved_ids=("cand-1",), saved_persona_ids=("new-1",)
        )
        manager.save_generated_personas.side_effect = lambda *a, **k: (
            calls.append("save") or saved
        )
        mock_get_gen_manager.return_value = gen
        mock_get_manager.return_value = manager
        return gen, manager, calls

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_claims_saves_then_finishes(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """Just calls hold, save and cleanup in that order"""
        gen, manager, calls = self._managers(mock_get_manager, mock_get_gen_manager)

        response = client.post(
            "/persona/save",
            data={**self._FORM, "selected_behavior_datasets": "t1, t2"},
        )

        assert response.status_code == 200
        assert "保存しました" in response.text
        assert calls == ["claim", "save", "finish"]
        principal, ids = gen.claim_candidates.call_args.args
        assert principal.sub == "local:dev@example.com" and ids == ["cand-1"]
        kwargs = manager.save_generated_personas.call_args.kwargs
        assert kwargs["edited"].name == "テストペルソナ"
        assert set(kwargs["selected_temp_ids"]) == {"t1", "t2"}
        claim, result = gen.finish_candidates.call_args.args
        assert claim == "the-claim" and result.saved_ids == ("cand-1",)

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_failed_save_still_finishes(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """Cleanup is always called even if save fails (no result = release the hold)"""
        gen, manager, calls = self._managers(mock_get_manager, mock_get_gen_manager)
        manager.save_generated_personas.side_effect = PersonaManagerError(
            "db down", code=ErrorCode.PERSONA_OPERATION_FAILED
        )

        response = client.post("/persona/save", data=self._FORM)

        assert response.status_code == 503
        assert response.text == ""
        gen.finish_candidates.assert_called_once_with("the-claim", None)

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_unavailable_candidate_is_the_expired_toast(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """If the hold cannot be taken, nothing is saved and an 'expired' toast is shown"""
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        gen, manager, _calls = self._managers(mock_get_manager, mock_get_gen_manager)
        gen.claim_candidates.side_effect = PersonaGenerationManagerError(
            "unavailable", code=ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        )

        response = client.post("/persona/save", data=self._FORM)

        assert response.status_code == 404
        trigger = json.loads(response.headers["HX-Trigger"])
        assert "期限切れ" in trigger["showToast"]["message"]
        manager.save_generated_personas.assert_not_called()
        gen.finish_candidates.assert_not_called()

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_error(self, mock_get_manager, mock_get_gen_manager, client):
        """Verify a save error is shown as a toast and the generated result is not lost.

        Errors a retry can resolve (TRANSIENT) return no body and notify via
        HX-Trigger. Returning the error partial as the body would remove the generated
        persona from the screen and prevent saving again (Issue #117).
        """
        _gen, manager, _calls = self._managers(mock_get_manager, mock_get_gen_manager)
        manager.save_generated_personas.side_effect = Exception("Database error")

        response = client.post("/persona/save", data=self._FORM)

        assert response.status_code == 500
        # The body is empty because the page is not rewritten
        assert response.text == ""
        trigger = json.loads(response.headers["HX-Trigger"])
        assert (
            "ペルソナの保存中にエラーが発生しました" in trigger["showToast"]["message"]
        )
        # The exception details must not leak to the user
        assert "Database error" not in response.headers["HX-Trigger"]


class TestPersonaDetailEndpoint:
    """Persona detail endpoint tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_get_detail_success(self, mock_get_manager, client, sample_persona):
        """Verify the persona detail page renders"""
        mock_manager = Mock()
        mock_manager.get_persona_detail.return_value = _detail(sample_persona)
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/persona/{sample_persona.id}")

        assert response.status_code == 200
        assert "田中花子" in response.text


class TestPersonaUpdateEndpoint:
    """Persona update endpoint tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_response_keeps_detail_header(
        self, mock_get_manager, client, sample_persona
    ):
        """The header swapped in after save matches the detail page.

        detail_swap.html used to have its own stale header, so the icon image, the
        icon edit UI and the generation log button disappeared right after saving.
        """
        updated_persona = sample_persona.update(name="更新された名前").with_avatar(
            "s3://bucket/persona_avatars/p1/abcd-1234.webp"
        )
        updated_persona = replace(
            updated_persona,
            generation_context={
                "data_type": "interview",
                "data_description": "",
                "custom_prompt": "",
                "persona_count": 1,
                "source_files": ["interview.txt"],
                "generated_at": "2026-09-30T12:00:00",
            },
        )
        mock_manager = Mock()
        mock_manager.update_persona.return_value = updated_persona
        mock_manager.describe_persona.side_effect = lambda _p, persona: _detail(persona)
        mock_manager.avatar_upload_enabled.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "更新された名前",
                "age": 36,
                "occupation": "シニアマーケター",
                "background": "更新された背景",
                "values": "新しい価値観",
                "pain_points": "新しい課題",
                "goals": "新しい目標",
            },
        )

        assert response.status_code == 200
        assert "persona-avatar-editor-slot" in response.text
        assert f"/persona/{sample_persona.id}/avatar?v=abcd-1234" in response.text
        assert "自動アバターに戻す" in response.text
        assert "生成ログ・評価" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_update_not_found(self, mock_get_manager, client):
        """Verify updating an unreadable or nonexistent persona returns 404"""
        mock_manager = Mock()
        mock_manager.update_persona.side_effect = _not_found()
        mock_get_manager.return_value = mock_manager

        response = client.put(
            "/persona/non-existent-id",
            data={
                "name": "テスト",
                "age": 30,
                "occupation": "テスト",
                "background": "テスト",
                "values": "テスト",
                "pain_points": "テスト",
                "goals": "テスト",
            },
        )

        assert response.status_code == 404


class TestPersonaDeleteEndpoint:
    """Persona delete endpoint tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_delete_success(self, mock_get_manager, client):
        """Verify persona deletion succeeds"""
        mock_manager = Mock()
        mock_manager.delete_persona.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete("/persona/test-id")

        assert response.status_code == 200
        assert "削除しました" in response.text
        assert "HX-Redirect" not in response.headers

    @patch("web.routers.persona.get_persona_manager")
    def test_delete_from_detail_redirects_to_the_list(self, mock_get_manager, client):
        """Deleting from the detail screen navigates to the list"""
        mock_manager = Mock()
        mock_manager.delete_persona.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete(
            "/persona/test-id",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/persona/test-id",
            },
        )

        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/persona/management"


class TestPersonaListPartialEndpoint:
    """Persona list partial endpoint tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_list_partial_success(self, mock_get_manager, client, sample_persona):
        """Verify the persona list partial is returned"""
        mock_manager = Mock()
        mock_manager.list_personas.return_value = _listing([sample_persona])
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial")

        assert response.status_code == 200
        assert "田中花子" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_list_partial_with_search(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """Search passes the tab and query to the Manager (narrows within the tab)"""
        mock_manager = Mock()
        mock_manager.search_personas.return_value = _listing([sample_persona])
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial?search=田中&tab=shared")

        assert response.status_code == 200
        _principal, tab, query = mock_manager.search_personas.call_args.args
        assert (tab, query) == ("shared", "田中")

    @patch("web.routers.persona.get_persona_manager")
    def test_list_partial_failure_is_a_banner(self, mock_get_manager, client):
        mock_manager = Mock()
        mock_manager.list_personas.side_effect = RuntimeError("down")
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial")

        assert response.status_code == 500
        assert response.headers["X-Render-Response"] == "true"
        assert "down" not in response.text


class TestSaveSelectedPersonasEndpoint:
    """Selected-persona save endpoint tests"""

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_claims_all_then_reports_counts(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """Bulk save shows the number saved and the names of candidates that failed"""
        from src.models.persona_candidate import CandidateSaveResult

        gen = Mock()
        gen.claim_candidates.return_value = "the-claim"
        mock_get_gen_manager.return_value = gen
        manager = Mock()
        result = CandidateSaveResult(
            saved_ids=("id1",),
            failed_ids=("id2",),
            saved_persona_ids=("new-1",),
            failed_names=("鈴木 一郎",),
        )
        manager.save_generated_personas.return_value = result
        mock_get_manager.return_value = manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": "id1, id2"}
        )

        assert response.status_code == 200
        assert "1人のペルソナを保存しました" in response.text
        assert "鈴木 一郎" in response.text
        principal, ids = gen.claim_candidates.call_args.args
        assert ids == ["id1", "id2"] and principal.sub == "local:dev@example.com"
        args, kwargs = manager.save_generated_personas.call_args
        assert args == (principal, "the-claim") and "edited" not in kwargs
        gen.finish_candidates.assert_called_once_with("the-claim", result)

    def test_save_selected_empty_ids(self, client):
        """Verify an empty ID list returns an error"""
        response = client.post("/persona/save-selected", data={"persona_ids": ""})

        # FastAPI may reject an empty string with 422
        assert response.status_code in [400, 422]

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_unavailable_is_the_expired_toast(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """If any candidate cannot be held, nothing is saved"""
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        gen = Mock()
        gen.claim_candidates.side_effect = PersonaGenerationManagerError(
            "unavailable", code=ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        )
        mock_get_gen_manager.return_value = gen
        manager = Mock()
        mock_get_manager.return_value = manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": "non-existent-id"}
        )

        assert response.status_code == 404
        trigger = json.loads(response.headers["HX-Trigger"])
        assert "一時データが期限切れ" in trigger["showToast"]["message"]
        manager.save_generated_personas.assert_not_called()


class TestPersonaEditForm:
    """Persona edit form tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_loads(self, mock_get_mgr, client, sample_persona):
        mock_mgr = Mock()
        mock_mgr.get_persona_for_edit.return_value = sample_persona
        mock_get_mgr.return_value = mock_mgr

        response = client.get(f"/persona/{sample_persona.id}/edit")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_not_found(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_persona_for_edit.side_effect = _not_found()
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/nonexistent/edit")
        assert response.status_code == 404

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_retryable_error(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_persona_for_edit.side_effect = PersonaManagerError(
            "down", code=ErrorCode.PERSONA_OPERATION_FAILED
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/test-id/edit")
        assert response.status_code == 500
        assert response.headers["X-Render-Response"] == "true"

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_error(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_persona_for_edit.side_effect = Exception("DB error")
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/test-id/edit")
        assert response.status_code == 500


class TestPersonaUpdateValidationPreservesInput:
    """Submitted values are not lost when validation fails (Issue #117).

    The edit form's hx-target is the detail body (#persona-detail-container), so
    returning the generic error partial removed the whole form and the user had to
    re-enter all 11 fields. Returning the edit form with the submitted values
    filled back in fixes this.
    """

    #: User-entered value; checks that it stays in the error response as is
    _SUBMITTED = {
        "name": "編集した名前",
        "age": "41",
        "occupation": "編集した職業",
        "background": "編集した背景テキスト",
        "values": "価値観A\n価値観B",
        "pain_points": "課題A\n課題B",
        "goals": "目標A\n目標B",
        "gender": "female",
        "country": "JP",
        "city": "編集した都市",
        "tags": "タグA\nタグB",
    }

    @staticmethod
    def _manager(sample_persona, exc):
        mgr = Mock()
        mgr.update_persona.side_effect = exc
        # Source data for the re-rendered header (name, back link); read as someone who can manage
        mgr.get_persona_for_edit.return_value = sample_persona
        return mgr

    @patch("web.routers.persona.get_persona_manager")
    def test_submitted_values_survive_validation_error(
        self, mock_get_mgr, client, sample_persona
    ):
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "occupation exceeds max length",
                code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                context={"field": "occupation", "max_length": 100},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.status_code == 400
        # The marker that lets htmx apply the body to the DOM is present
        assert response.headers.get("X-Render-Response") == "true"
        # Every entered field is kept (a toast or generic partial would drop them)
        for field, value in self._SUBMITTED.items():
            first_line = value.split("\n")[0]
            assert first_line in response.text, f"{field} の入力が失われた"
        # The message for the field is shown
        assert "職業は100文字以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_capacity_error_also_preserves_input(
        self, mock_get_mgr, client, sample_persona
    ):
        """CAPACITY (over a limit) is fixed by editing the input, so it is handled the same way."""
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "too many tags",
                code=ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS,
                context={"field": "tags", "max_items": 20},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.status_code == 400
        assert "編集した背景テキスト" in response.text
        assert "タグは20項目以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_transient_error_still_uses_toast(
        self, mock_get_mgr, client, sample_persona
    ):
        """Errors that a retry resolves show a toast without re-rendering (the input stays in the DOM)."""
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "dynamodb unavailable",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.text == ""
        assert "HX-Trigger" in response.headers

    @patch("web.routers.persona.get_persona_manager")
    def test_falls_back_to_toast_when_persona_cannot_be_refetched(
        self, mock_get_mgr, client, sample_persona
    ):
        """Do not destroy the input even when the persona for re-rendering cannot be fetched."""
        mgr = Mock()
        mgr.update_persona.side_effect = PersonaManagerError(
            "name is blank",
            code=ErrorCode.PERSONA_FIELD_REQUIRED,
            context={"field": "name"},
        )
        mgr.get_persona_for_edit.side_effect = _not_found()  # Cannot be fetched
        mock_get_mgr.return_value = mgr

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        # The form cannot be rendered, so fall back to a toast (the page is not rewritten)
        assert response.text == ""
        assert "HX-Trigger" in response.headers

    @patch("web.routers.persona.get_persona_manager")
    def test_error_response_does_not_leak_exception_message(
        self, mock_get_mgr, client, sample_persona
    ):
        secret = "arn:aws:dynamodb:ap-northeast-1:123456789012:table/personas"
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                secret,
                code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                context={"field": "name", "max_length": 50},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert secret not in response.text


class TestPersonaUpdate:
    """Persona update tests"""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_clears_demographics_with_empty_form_values(
        self, mock_get_mgr, client, sample_persona
    ):
        """Empty gender/country/city form values are passed to update_persona as empty strings and cleared"""
        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_mgr.update_persona.return_value = sample_persona
        mock_mgr.describe_persona.side_effect = lambda _p, persona: _detail(persona)
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "新名前",
                "age": "35",
                "occupation": "新職業",
                "background": "新背景",
                "values": "価値1",
                "pain_points": "課題1",
                "goals": "目標1",
                "gender": "",
                "country": "",
                "city": "",
            },
        )
        assert response.status_code == 200
        # Pass the empty string as is (not None) so update() can clear it
        call_kwargs = mock_mgr.update_persona.call_args[1]
        assert call_kwargs["gender"] == ""
        assert call_kwargs["country"] == ""
        assert call_kwargs["city"] == ""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_validation_error(self, mock_get_mgr, client, sample_persona):
        from src.managers.persona_manager import PersonaManagerError

        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_mgr.update_persona.side_effect = PersonaManagerError("名前が空です")
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "valid",
                "age": "30",
                "occupation": "Y",
                "background": "Z",
                "values": "v",
                "pain_points": "p",
                "goals": "g",
            },
        )
        assert response.status_code == 400


class TestPersonaMemories:
    """Persona memory management tests"""

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_memories_list_error(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.get_memories.side_effect = PersonaMemoryManagerError(
            "agentcore said s3://secret", code=ErrorCode.MEMORY_OPERATION_FAILED
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/p1/memories")

        # Show the catalog message in the list area, not the exception text
        assert response.status_code == 200
        assert (
            html.escape(
                user_message_for_code(ErrorCode.MEMORY_OPERATION_FAILED.value, None)
            )
            in response.text
        )
        assert "s3://secret" not in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_memory_config_error_replaces_item(self, mock_get_mgr, client):
        """Feature disabled (CONFIG) is not fixed by editing input, so the item
        may be replaced with an error card (the memory cannot be deleted at all)."""
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_memory.side_effect = PersonaMemoryManagerError(
            "memory feature disabled", code=ErrorCode.MEMORY_FEATURE_DISABLED
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories/m1")

        assert response.status_code == 400
        assert response.headers.get("X-Render-Response") == "true"
        assert 'id="memory-item-m1"' in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_memory_transient_uses_toast(self, mock_get_mgr, client):
        """Keep the memory item on errors a retry can resolve (#117).

        The delete hx-swap is outerHTML. On a service failure or timeout the memory
        still exists, so replacing the item with an error card would make the list
        disagree with the data and prevent a retry. Notify with a toast.
        """
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_memory.side_effect = PersonaMemoryManagerError(
            "memory service timeout", code=ErrorCode.MEMORY_SERVICE_UNAVAILABLE
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories/m1")

        # Does not replace the memory item (no body)
        assert response.text == ""
        assert "HX-Trigger" in response.headers
        # No id is emitted for swapping the memory item
        assert "memory-item-m1" not in response.headers.get("HX-Trigger", "")

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_all_memories_disabled(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_all_memories.side_effect = PersonaMemoryManagerError(
            "長期記憶機能が無効です"
        )
        mock_mgr.safe_get_memories.return_value = []
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_adding_knowledge_does_not_log_its_title(
        self, mock_get_mgr, client, caplog
    ):
        """The knowledge title is memory content and stays out of the logs."""
        mark = "TOPIC-TITLE-SENTINEL-3b9a"
        mock_mgr = Mock()
        mock_mgr.get_memories.return_value = ([], 1, 1)
        mock_get_mgr.return_value = mock_mgr

        with caplog.at_level("DEBUG"):
            response = client.post(
                "/persona/p1/memories",
                data={"topic_name": f"題名{mark}", "topic_content": "本文"},
            )

        assert response.status_code == 200
        assert not [r for r in caplog.records if mark in r.getMessage()]


class TestMemoryLookupUnavailableIs503:
    """A failed decision read on the memory screen is the common 503.

    ``LookupUnavailableError`` is not a ``PersonaMemoryManagerError``;
    it must reach the common exception handler instead of the generic
    "unexpected error" branches (error-catalog.md).
    """

    @staticmethod
    def _unavailable():
        from src.models.errors import LookupUnavailableError

        return LookupUnavailableError("personas table unreachable")

    @pytest.mark.parametrize(
        ("method", "path", "manager_method", "kwargs"),
        [
            ("get", "/persona/p1/memories", "get_memories", {}),
            ("delete", "/persona/p1/memories/m1", "delete_memory", {}),
            ("delete", "/persona/p1/memories", "delete_all_memories", {}),
            (
                "post",
                "/persona/p1/memories",
                "add_knowledge",
                {"data": {"topic_name": "t", "topic_content": "c"}},
            ),
            (
                "post",
                "/persona/p1/memories/upload-preview",
                "ensure_memory_manageable",
                {"files": {"file": ("k.txt", b"knowledge", "text/plain")}},
            ),
        ],
    )
    @patch("web.routers.persona.get_persona_memory_manager")
    def test_lookup_failure_is_503(
        self, mock_get_mgr, client, method, path, manager_method, kwargs
    ):
        mock_mgr = Mock()
        getattr(mock_mgr, manager_method).side_effect = self._unavailable()
        mock_mgr.safe_get_memories.return_value = []
        mock_get_mgr.return_value = mock_mgr

        response = getattr(client, method)(path, **kwargs)

        assert response.status_code == 503
        # the common handler answers; no memory partial is rendered
        assert "memory-list" not in response.text


class TestAddMemoryValidationPreservesInput:
    """Input is not lost when adding knowledge fails validation (#117).

    The memory list (#memory-list-container) collapses its input area with Alpine's
    `showForm: false`. Replacing it on error would close the input area and lose
    the input, so HX-Retarget swaps only the dedicated area inside the form.
    """

    _FORM = {
        "topic_name": "好きな食べ物",
        "topic_content": "ラーメンが好き",
        "strategy_type": "semantic",
    }

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_validation_error_retargets_to_form_error_area(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "topic name is blank",
            code=ErrorCode.MEMORY_TOPIC_NAME_REQUIRED,
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.status_code == 400
        # The marker that reaches the screen is present
        assert response.headers.get("X-Render-Response") == "true"
        # Swaps the dedicated area inside the source form, not the list.
        # Use a find-relative selector so it is not confused with another form that has the same area
        assert response.headers.get("HX-Retarget") == "find .memory-form-error"
        assert response.headers.get("HX-Reswap") == "innerHTML"
        assert "トピック名を入力してください" in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_capacity_error_also_retargets(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "content too long",
            code=ErrorCode.MEMORY_CONTENT_TOO_LONG,
            context={"max_length": 10000},
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.status_code == 400
        assert response.headers.get("HX-Retarget") == "find .memory-form-error"
        assert "内容は10000文字以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_transient_error_uses_toast_not_retarget(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "agentcore unavailable",
            code=ErrorCode.MEMORY_OPERATION_FAILED,
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.text == ""
        assert "HX-Trigger" in response.headers
        assert "HX-Retarget" not in response.headers


class TestFormErrorTargetExists:
    """The HX-Retarget target exists in the template (#117).

    Even if the server returns `HX-Retarget: find .memory-form-error`, htmx cannot
    find the target and no message appears unless that element is inside the source
    form. Because the selector is find-relative, it must be **inside each form**.
    The test pins the server/template correspondence.
    """

    def test_forms_posting_memories_contain_the_error_area(self):
        import re
        from pathlib import Path

        templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
        offenders = []
        for path in templates_dir.rglob("*.html"):
            html = path.read_text(encoding="utf-8")
            # Extract the <form ...> ... </form> that POSTs to /memories
            for form in re.findall(r"<form\b.*?</form>", html, re.DOTALL):
                if 'hx-post="/persona/' not in form or "/memories" not in form:
                    continue
                # upload-preview (preview generation) does not add memory, so it is excluded
                if "/memories/upload-preview" in form:
                    continue
                if "memory-form-error" not in form:
                    offenders.append(path.name)
        assert offenders == [], (
            "記憶追加フォーム内に .memory-form-error が無い"
            f"（find 相対セレクタが解決できない）: {offenders}"
        )

    def test_avatar_upload_form_contains_the_error_area(self):
        import re
        from pathlib import Path

        templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
        forms = [
            form
            for path in templates_dir.rglob("*.html")
            for form in re.findall(
                r"<form\b.*?</form>", path.read_text(encoding="utf-8"), re.DOTALL
            )
            if "/avatar" in form and "hx-post=" in form
        ]
        assert forms, "アイコン画像アップロードフォームが見つからない"
        assert all("avatar-form-error" in form for form in forms), (
            "アイコン画像フォーム内に .avatar-form-error が無い"
        )


class TestPersonaAvatarEndpoints:
    """Icon image upload, delete and redirect"""

    AVATAR_PATH = "s3://bucket/persona_avatars/p1/abcd-1234.webp"

    @patch("web.routers.persona.get_persona_manager")
    def test_upload_success_returns_editor_with_toast(
        self, mock_get_manager, client, sample_persona
    ):
        updated = sample_persona.with_avatar(self.AVATAR_PATH)
        mock_manager = Mock()
        mock_manager.set_avatar.return_value = updated
        mock_manager.avatar_upload_enabled.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.post(
            f"/persona/{sample_persona.id}/avatar",
            files={"file": ("a.png", BytesIO(b"png-bytes"), "image/png")},
        )

        assert response.status_code == 200
        principal, persona_id, content = mock_manager.set_avatar.call_args.args
        assert principal.sub == "local:dev@example.com"
        assert (persona_id, content) == (sample_persona.id, b"png-bytes")
        assert f"/persona/{sample_persona.id}/avatar?v=abcd-1234" in response.text
        assert "自動アバターに戻す" in response.text
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "success"

    @patch("web.routers.persona.get_persona_manager")
    def test_upload_invalid_image_retargets_form_error(
        self, mock_get_manager, client, sample_persona
    ):
        mock_manager = Mock()
        mock_manager.set_avatar.side_effect = PersonaManagerError(
            "bad image", code=ErrorCode.PERSONA_AVATAR_INVALID_IMAGE
        )
        mock_get_manager.return_value = mock_manager

        response = client.post(
            f"/persona/{sample_persona.id}/avatar",
            files={"file": ("a.png", BytesIO(b"x"), "image/png")},
        )

        assert response.status_code == 400
        assert response.headers["HX-Retarget"] == "find .avatar-form-error"
        assert response.headers["X-Render-Response"] == "true"

    @patch("web.routers.persona.get_persona_manager")
    def test_upload_transient_failure_is_toast(
        self, mock_get_manager, client, sample_persona
    ):
        mock_manager = Mock()
        mock_manager.set_avatar.side_effect = PersonaManagerError(
            "s3 down", code=ErrorCode.PERSONA_OPERATION_FAILED
        )
        mock_get_manager.return_value = mock_manager

        response = client.post(
            f"/persona/{sample_persona.id}/avatar",
            files={"file": ("a.png", BytesIO(b"x"), "image/png")},
        )

        assert response.text == ""
        assert "showToast" in response.headers["HX-Trigger"]

    @patch("web.routers.persona.get_persona_manager")
    def test_delete_reverts_to_generated_avatar(
        self, mock_get_manager, client, sample_persona
    ):
        mock_manager = Mock()
        mock_manager.delete_avatar.return_value = sample_persona
        mock_manager.avatar_upload_enabled.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete(f"/persona/{sample_persona.id}/avatar")

        assert response.status_code == 200
        principal, persona_id = mock_manager.delete_avatar.call_args.args
        assert principal.sub == "local:dev@example.com"
        assert persona_id == sample_persona.id
        assert "自動アバターに戻す" not in response.text
        assert "画像を変更" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_get_redirects_to_presigned_url(self, mock_get_manager, client):
        from src.models.asset_delivery import DeliveryResult

        mock_manager = Mock()
        mock_manager.deliver_avatar.return_value = DeliveryResult.redirect(
            "https://signed.example/a.webp"
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/p1/avatar", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"] == "https://signed.example/a.webp"
        assert response.headers["Cache-Control"] == "private, max-age=300"

    @patch("web.routers.persona.get_persona_manager")
    def test_get_streams_the_avatar_in_proxy_mode(self, mock_get_manager, client):
        """Closed network (proxy): the app streams it. Cache headers are the same"""
        from src.models.asset_delivery import AssetStream, DeliveryResult

        close = Mock()
        asset = AssetStream(
            chunks=iter([b"RIFF", b"WEBP"]), content_length=8, close=close
        )
        mock_manager = Mock()
        mock_manager.deliver_avatar.return_value = DeliveryResult.stream(
            asset, "image/webp", "p1.webp"
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/p1/avatar", follow_redirects=False)

        assert response.status_code == 200
        assert response.content == b"RIFFWEBP"
        assert response.headers["Content-Type"] == "image/webp"
        assert response.headers["Content-Length"] == "8"
        assert response.headers["Content-Disposition"].startswith("inline;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "private, max-age=300"
        close.assert_called()

    @patch("web.routers.persona.get_persona_manager")
    def test_get_failure_is_500(self, mock_get_manager, client):
        mock_manager = Mock()
        mock_manager.deliver_avatar.side_effect = PersonaManagerError(
            "s3", code=ErrorCode.PERSONA_OPERATION_FAILED
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/p1/avatar", follow_redirects=False)

        assert response.status_code == 500

    @patch("web.routers.persona.get_persona_manager")
    def test_get_without_avatar_is_404(self, mock_get_manager, client):
        mock_manager = Mock()
        mock_manager.deliver_avatar.side_effect = PersonaManagerError(
            "no avatar", code=ErrorCode.PERSONA_AVATAR_NOT_FOUND
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/p1/avatar", follow_redirects=False)

        assert response.status_code == 404

    @patch("web.routers.persona.get_persona_manager")
    def test_detail_hides_editor_without_storage(
        self, mock_get_manager, client, sample_persona
    ):
        mock_manager = Mock()
        mock_manager.get_persona_detail.return_value = _detail(sample_persona)
        mock_manager.avatar_upload_enabled.return_value = False
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/persona/{sample_persona.id}")

        assert response.status_code == 200
        assert "persona-avatar-editor-slot" in response.text
        assert "画像を変更" not in response.text


class TestPersonaModalsUseSharedDialog:
    """The persona modals are rendered by components/dialog.html."""

    @patch("web.routers.persona.get_persona_manager")
    def test_kb_binding_dialogs_have_unique_ids_and_close_on_success(
        self, mock_get_manager, client
    ):
        from src.managers.persona_manager import KbBindingView
        from src.models.knowledge_base import KnowledgeBase

        kbs = [
            replace(KnowledgeBase.create_new("ABC", f"KB {kb_id}", ""), id=kb_id)
            for kb_id in ("kb1", "kb2")
        ]
        mock_get_manager.return_value.get_kb_binding.return_value = KbBindingView(
            persona_id="p1", manageable=True, binding=None, choices=kbs
        )

        html = client.get("/persona/p1/kb-binding").text

        assert dialog_ids(html) == ["kb-binding-kb1", "kb-binding-kb2"]
        for kb_id in ("kb1", "kb2"):
            outer = dialog_markup(html, f"kb-binding-{kb_id}")
            assert "data-dialog-close-on-success" in outer
            assert "data-dialog-backdrop-close" not in outer
            assert f'data-dialog-open="kb-binding-{kb_id}"' in html
        assert html.count('hx-post="/persona/p1/kb-binding"') == 2
        assert "showModal" not in html

    @patch("web.routers.persona.get_persona_manager")
    def test_dataset_binding_and_preview_dialogs(self, mock_get_manager, client):
        from src.managers.persona_manager import BindingRow, DatasetBindingsView
        from src.models.dataset import Dataset, DatasetColumn

        ds1 = replace(
            Dataset.create_new(
                name="DS ds1",
                description="",
                s3_path="s3://b/k.csv",
                columns=[DatasetColumn(name="user_id", data_type="string")],
                row_count=3,
            ),
            id="ds1",
        )
        view = DatasetBindingsView(
            persona_id="p1",
            manageable=True,
            bindings=[
                BindingRow(
                    target_id="ds2",
                    name="DS ds2",
                    usable=True,
                    binding_keys={"user_id": "u1"},
                )
            ],
            choices=[ds1],
        )
        mock_get_manager.return_value.get_dataset_bindings.return_value = view

        html = client.get("/persona/p1/dataset-bindings").text

        # Links first, then the additional choices
        assert dialog_ids(html) == ["dataset-preview-ds2", "dataset-binding-ds1"]
        binding = dialog_markup(html, "dataset-binding-ds1")
        assert "data-dialog-backdrop-close" not in binding
        preview = dialog_markup(html, "dataset-preview-ds2")
        assert "data-dialog-backdrop-close" in preview
        # Preview and delete URLs use the dataset ID (not the link ID)
        assert 'hx-get="/persona/p1/dataset-bindings/ds2/preview"' in html
        assert 'hx-delete="/persona/p1/dataset-bindings/ds2"' in html
        assert 'hx-post="/persona/p1/dataset-bindings"' in html
        assert "showModal" not in html and "showPreview" not in html

    @patch("web.routers.persona.get_persona_manager")
    def test_generation_log_is_a_viewing_dialog(
        self, mock_get_manager, client, sample_persona
    ):
        persona = replace(
            sample_persona, generation_log=[{"type": "thinking", "content": "分析"}]
        )
        mock_get_manager.return_value.get_persona_detail.return_value = _detail(persona)

        html = client.get(f"/persona/{persona.id}").text

        outer = dialog_markup(html, "generation-log-dialog")
        assert "data-dialog-backdrop-close" in outer
        assert 'data-dialog-open="generation-log-dialog"' in html
        assert "showGenLog" not in html


# =============================================================================
# Tabbed list, search, detail, edit and delete decisions
# PersonaManager and ResourceSharing are real; Stores are in-memory fakes.
# =============================================================================

A_SUB = "local:a@example.com"  # member (sales)
B_SUB = "local:b@example.com"  # member (sales, plan)
D_SUB = "local:d@example.com"  # member (hr); unrelated


@pytest.fixture
def world():
    from tests.unit.test_persona_manager_sharing import World

    return World()


@pytest.fixture
def real_manager(world):
    from src.managers.components.resource_sharing import ResourceSharing
    from src.managers.persona_manager import PersonaManager
    from tests.unit.test_resource_sharing import DEPARTMENTS, TABLE

    departments = Mock()
    departments.list_all.return_value = list(DEPARTMENTS)
    departments.get_many.side_effect = lambda ids: {
        d.group_id: d for d in DEPARTMENTS if d.group_id in ids
    }
    owners = Mock()
    owners.labels.side_effect = lambda subs: {s: f"氏名-{s}" for s in subs}
    sharing = ResourceSharing(world.store, departments, sleep=lambda _s: None)
    sharing.register(TABLE)
    from tests.unit.persona_u5_world import FakeBindingStore

    # Links and long-term memory are fakes/none (cleanup on delete must not touch real AWS)
    manager = PersonaManager(
        persona_store=world.persona_store,
        s3_service=Mock(),
        resource_sharing=sharing,
        owners=owners,
        binding_store=FakeBindingStore(),
        memory_service=None,
    )
    with (
        patch("web.routers.persona.get_persona_manager", return_value=manager),
        patch("web.routers.api.get_persona_manager", return_value=manager),
    ):
        yield manager


def _login(client, who: str):
    from tests.conftest import login_as

    users = {
        "a": ("a@example.com", ("member",), ("sales",)),
        "b": ("b@example.com", ("member",), ("sales", "plan")),
        "c": ("c@example.com", ("member",), ()),
        "d": ("d@example.com", ("member",), ("hr",)),
        "admin": ("admin@example.com", ("admin",), ()),
    }
    email, roles, groups = users[who]
    login_as(client, email=email, roles=roles, groups=groups)
    return client


def _panel(client, tab: str, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"tab": tab, **params}.items())
    return client.get(f"/persona/list/partial?{query}").text


@pytest.mark.usefixtures("real_manager")
class TestPersonaTabs:
    def test_member_sees_two_tabs_with_counts(self, client, world):
        world.add("p1", A_SUB)
        world.add("p2", B_SUB, {"sales"})
        html = _login(client, "a").get("/persona/management").text

        assert 'data-testid="list-tab-mine"' in html
        assert 'data-testid="list-tab-shared"' in html
        assert 'data-testid="list-tab-all"' not in html
        assert 'data-count-label="自分のもの 1 件"' in html
        assert 'data-count-label="共有されたもの 1 件"' in html
        assert 'data-reload-url="/persona/management?tab=mine"' in html

    def test_admin_sees_the_all_tab(self, client, world):
        world.add("p1", A_SUB)
        html = _login(client, "admin").get("/persona/management?tab=all").text
        assert 'data-count-label="すべて 1 件"' in html
        assert 'id="persona-list-tab-all"' in html and 'aria-selected="true"' in html

    def test_count_that_cannot_be_read_is_a_dash(self, client, world):
        from src.services.stores.resource_share_store import ResourceShareStoreError

        world.add("p1", A_SUB)
        world.store.count_owner = Mock(side_effect=ResourceShareStoreError("down"))
        html = _login(client, "a").get("/persona/management").text
        assert 'data-count-label="自分のもの -"' in html

    def test_member_asking_for_all_gets_mine(self, client, world):
        world.add("p1", A_SUB)
        world.add("p2", D_SUB)
        html = _login(client, "a").get("/persona/management?tab=all").text
        assert 'data-reload-url="/persona/management?tab=mine"' in html
        assert "名前-p1" in html and "名前-p2" not in html

    def test_tab_request_from_the_list_returns_the_panel_only(self, client, world):
        world.add("p2", B_SUB, {"sales"})
        response = _login(client, "a").get(
            "/persona/management?tab=shared", headers={"HX-Target": "persona-list"}
        )
        assert "<html" not in response.text and "名前-p2" in response.text

    def test_shared_rows_have_owner_and_source(self, client, world):
        world.add("p2", B_SUB, {"sales"})
        html = _panel(_login(client, "a"), "shared")
        assert f"所有者: 氏名-{B_SUB}" in html
        assert "共有元: 営業部" in html

    def test_shared_tab_has_no_selection_or_share_button(self, client, world):
        world.add("p2", B_SUB, {"sales"})
        html = _panel(_login(client, "a"), "shared")
        assert 'data-testid="bulk-select-bar"' not in html
        assert 'data-testid="bulk-select-row"' not in html
        assert "data-share-url" not in html
        assert 'hx-delete="/persona/p2"' not in html

    def test_own_rows_have_the_share_column_and_actions(self, client, world):
        world.add("p1", A_SUB, {"plan"})
        world.add("p3", A_SUB)
        html = _panel(_login(client, "a"), "mine")
        assert "共有: 企画部" in html and "〔共有なし〕" in html
        assert 'data-testid="bulk-select-bar"' in html
        assert 'data-share-url="/share/persona/p1/dialog"' in html
        assert 'data-dialog-open="share-dialog"' in html

    def test_card_share_button_says_share_and_names_the_persona(self, client, world):
        """The card button reads 「共有」; its accessible name adds the persona name."""
        import re

        world.add("p1", A_SUB)
        html = _panel(_login(client, "a"), "mine")
        buttons = re.findall(
            r'<button[^>]*data-testid="persona-share-button"[^>]*>\s*(.*?)\s*</button>',
            html,
            re.S,
        )
        assert buttons == ["共有"]
        labels = re.findall(
            r'<button[^>]*aria-label="([^"]*)"[^>]*data-testid="persona-share-button"',
            html,
        )
        assert len(labels) == 1 and labels[0].endswith(" を共有")

    def test_admin_all_marks_unknown_owner(self, client, world):
        world.add("legacy", None)
        html = _panel(_login(client, "admin"), "all")
        assert "〔所有者不明〕" in html
        assert 'data-testid="bulk-select-bar"' in html

    @pytest.mark.parametrize(
        ("who", "tab", "text"),
        [
            ("a", "mine", "まだペルソナがありません"),
            (
                "a",
                "shared",
                "共有されたペルソナはありません。所属グループに共有されると、ここに表示されます",
            ),
            ("c", "shared", "所属グループがないため、共有されたペルソナはありません"),
        ],
    )
    def test_empty_states(self, client, who, tab, text):
        assert text in _panel(_login(client, who), tab)

    def test_load_more_follows_the_next_cursor(self, client, world):
        for i in range(21):
            world.add(f"p{i:02d}", A_SUB, minutes=i)
        html = _panel(_login(client, "a"), "mine")
        assert 'id="load-more-persona"' in html and "tab=mine" in html
        world.store.bodies.pop("p00")
        assert 'id="load-more-persona"' not in _panel(client, "mine")

    def test_search_stays_inside_the_tab(self, client, world):
        world.add("p1", A_SUB, name="田中")
        world.add("p2", D_SUB, name="田中")
        html = _panel(_login(client, "a"), "mine", search="田中")
        assert "persona-card-p1" in html and "persona-card-p2" not in html

    def test_search_over_500_says_so(self, client, world):
        for i in range(501):
            world.add(f"p{i:03d}", A_SUB, minutes=i)
        html = _panel(_login(client, "a"), "mine", search="名前")
        assert "新しい順に 500 件までを検索しました" in html

    def test_selection_list_is_tab_scoped(self, client, world):
        world.add("p1", A_SUB)
        world.add("p2", D_SUB)
        world.add("p3", B_SUB, {"sales"})
        html = _panel(_login(client, "a"), "mine", selectable="1")
        assert 'data-persona-id="p1"' in html and 'data-persona-id="p2"' not in html
        assert 'data-testid="persona-select-tab-shared"' in html
        shared = _panel(client, "shared", selectable="1")
        assert 'data-persona-id="p3"' in shared


@pytest.mark.usefixtures("real_manager")
class TestPersonaDetailAuthz:
    def test_unshared_member_gets_not_found(self, client, world):
        world.add("p1", A_SUB)
        response = _login(client, "d").get(
            "/persona/p1", headers={"HX-Request": "", "Accept": "text/html"}
        )
        assert response.status_code == 404

    def test_shared_member_sees_no_change_operations(self, client, world):
        world.add("p1", A_SUB, {"sales"})
        html = _login(client, "b").get("/persona/p1").text
        assert f"所有者: 氏名-{A_SUB}" in html
        assert 'hx-get="/persona/p1/edit"' not in html
        assert 'hx-delete="/persona/p1"' not in html
        assert "data-share-url" not in html
        assert "共有されたペルソナです" in html

    def test_owner_sees_edit_share_and_delete(self, client, world):
        world.add("p1", A_SUB)
        html = _login(client, "a").get("/persona/p1").text
        assert 'hx-get="/persona/p1/edit"' in html
        assert 'hx-delete="/persona/p1"' in html
        assert 'data-share-url="/share/persona/p1/dialog"' in html
        assert 'id="share-dialog"' in html

    def test_detail_actions_say_share_and_do_not_wrap(self, client, world):
        """The detail actions read 「共有」 and never break mid-word."""
        import re

        world.add("p1", A_SUB)
        html = _login(client, "a").get("/persona/p1").text
        share = re.search(
            r'<button[^>]*data-testid="persona-share-button"[^>]*>\s*(.*?)\s*</button>',
            html,
            re.S,
        )
        assert share and share.group(1) == "共有"
        actions = re.search(
            r'<div[^>]*data-testid="persona-detail-actions"[^>]*>', html
        )
        assert actions and "flex-wrap" in actions.group(0)
        for button in re.findall(
            r'<button[^>]*(?:hx-get="/persona/p1/edit"|data-testid="persona-share-button"'
            r'|hx-delete="/persona/p1"|data-dialog-open="generation-log-dialog")[^>]*>',
            html,
        ):
            assert "whitespace-nowrap" in button

    def test_detail_header_is_a_reloadable_region(self, client, world):
        """The share save reloads #persona-detail-header from its own URL."""
        world.add("p1", A_SUB)
        html = _login(client, "a").get("/persona/p1").text
        assert 'id="persona-detail-header"' in html
        assert 'data-reload-url="/persona/p1/header"' in html
        assert html.count('id="persona-detail-header"') == 1

    def test_header_partial_shows_the_current_shares(self, client, world):
        world.add("p1", A_SUB, {"plan"})
        response = _login(client, "a").get(
            "/persona/p1/header", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        assert "共有: 企画部" in response.text
        assert 'data-testid="persona-owner-line"' in response.text
        # the region's content only, not the region itself (innerHTML swap)
        assert 'id="persona-detail-header"' not in response.text

    def test_detail_header_and_update_show_the_same_owner_line(self, client, world):
        """The page, the header reload and the edit swap draw the same rows (R-07)."""
        import re

        world.add("p1", A_SUB, {"plan"})
        client = _login(client, "a")
        form = dict(TestPersonaUpdateValidationPreservesInput._SUBMITTED)

        def owner_line(html: str) -> str:
            line = re.search(
                r'<p[^>]*data-testid="persona-owner-line"[^>]*>(.*?)</p>', html, re.S
            )
            assert line, "owner line missing"
            return " ".join(line.group(1).split())

        lines = [
            owner_line(client.get("/persona/p1").text),
            owner_line(
                client.get("/persona/p1/header", headers={"HX-Request": "true"}).text
            ),
            owner_line(
                client.put(
                    "/persona/p1", data=form, headers={"HX-Request": "true"}
                ).text
            ),
        ]
        assert lines[0] == lines[1] == lines[2]
        assert f"所有者: 氏名-{A_SUB}" in lines[0] and "共有: 企画部" in lines[0]

    @pytest.mark.parametrize(("who", "expected"), [("b", 200), ("d", 404)])
    def test_header_partial_needs_read_access(self, client, world, who, expected):
        world.add("p1", A_SUB, {"sales"})
        response = _login(client, who).get(
            "/persona/p1/header", headers={"HX-Request": "true"}
        )
        assert response.status_code == expected

    @pytest.mark.parametrize(
        ("who", "expected"), [("b", 403), ("d", 404), ("admin", 200)]
    )
    def test_edit_form(self, client, world, who, expected):
        world.add("p1", A_SUB, {"sales"})
        assert _login(client, who).get("/persona/p1/edit").status_code == expected

    @pytest.mark.parametrize(
        ("who", "expected"), [("b", 403), ("d", 404), ("admin", 200)]
    )
    def test_update(self, client, world, who, expected):
        world.add("p1", A_SUB, {"sales"})
        response = _login(client, who).put(
            "/persona/p1",
            data={
                "name": "新しい名前",
                "age": 30,
                "occupation": "職業",
                "background": "背景",
                "values": "v",
                "pain_points": "p",
                "goals": "g",
            },
        )
        assert response.status_code == expected

    @pytest.mark.parametrize(
        ("who", "expected"), [("b", 403), ("d", 404), ("admin", 200)]
    )
    def test_delete(self, client, world, who, expected):
        world.add("p1", A_SUB, {"sales"})
        assert _login(client, who).delete("/persona/p1").status_code == expected

    @staticmethod
    def _delete_from_list(client, persona_id: str):
        return client.delete(
            f"/persona/{persona_id}",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/persona/management",
            },
        )

    def test_deleting_an_already_deleted_persona_reloads_the_list(self, client, world):
        """R-04: the stale card goes away; the answer is still a 404 toast."""
        from web.error_messages import NOT_FOUND_TOAST_MESSAGE

        response = self._delete_from_list(_login(client, "a"), "gone")

        assert response.status_code == 404
        triggers = json.loads(response.headers["HX-Trigger"])
        assert triggers["showToast"]["message"] == NOT_FOUND_TOAST_MESSAGE
        assert triggers["listReload"] == {"target": "#persona-list"}

    def test_deleted_and_unreadable_answer_the_same(self, client, world):
        """An unreadable target gets the very same response."""
        world.add("p1", A_SUB)
        unreadable = self._delete_from_list(_login(client, "d"), "p1")
        missing = self._delete_from_list(client, "gone")
        assert unreadable.status_code == missing.status_code == 404
        assert unreadable.headers["HX-Trigger"] == missing.headers["HX-Trigger"]
        assert unreadable.content == missing.content == b""
        assert "p1" in world.store.bodies

    def test_deleting_a_missing_persona_from_its_detail_page_does_not_reload(
        self, client, world
    ):
        response = _login(client, "a").delete(
            "/persona/gone",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/persona/gone",
            },
        )
        assert response.status_code == 404
        assert "listReload" not in json.loads(response.headers["HX-Trigger"])

    def test_update_response_keeps_the_owner_and_shared_rows(self, client, world):
        """R-07: the swapped header shows the same rows as the detail page."""
        world.add("p1", A_SUB, {"sales", "plan"})
        response = _login(client, "a").put(
            "/persona/p1",
            data={
                "name": "新しい名前",
                "age": 30,
                "occupation": "職業",
                "background": "背景",
                "values": "v",
                "pain_points": "p",
                "goals": "g",
            },
        )
        assert response.status_code == 200
        html = response.text
        assert 'data-testid="persona-owner-line"' in html
        assert f"所有者: 氏名-{A_SUB}" in html
        assert "共有: 企画部、営業部" in html

    def test_update_response_of_an_unshared_persona_says_not_shared(
        self, client, world
    ):
        world.add("p1", A_SUB)
        html = (
            _login(client, "admin")
            .put(
                "/persona/p1",
                data={
                    "name": "新しい名前",
                    "age": 30,
                    "occupation": "職業",
                    "background": "背景",
                    "values": "v",
                    "pain_points": "p",
                    "goals": "g",
                },
            )
            .text
        )
        assert f"所有者: 氏名-{A_SUB}" in html
        assert "共有なし" in html


@pytest.mark.usefixtures("real_manager")
class TestPersonaApiScope:
    def test_list_returns_own_and_shared_only(self, client, world):
        world.add("p1", A_SUB)
        world.add("p2", B_SUB, {"sales"})
        world.add("p3", D_SUB)
        body = _login(client, "a").get("/api/personas").json()
        assert {p["id"] for p in body} == {"p1", "p2"}

    def test_unreadable_detail_is_not_found(self, client, world):
        world.add("p3", D_SUB)
        response = _login(client, "a").get("/api/personas/p3")
        assert response.status_code == 404
        assert response.json()["error_code"] == "persona_not_found"


class _ThreadRecorder:
    """Record the thread each mocked Manager method ran on."""

    def __init__(self) -> None:
        self.threads: dict[str, list[int]] = {}

    def returning(self, name: str, value=None, error: Exception | None = None):
        def side_effect(*_args, **_kwargs):
            self.threads.setdefault(name, []).append(threading.get_ident())
            if error is not None:
                raise error
            return value

        return side_effect

    def assert_off_the_loop(self, client, names: list[str]) -> None:
        # Thread of the TestClient event loop (all requests are handled on this loop)
        loop_thread = client.portal.call(threading.get_ident)
        for name in names:
            assert self.threads.get(name), f"{name} が呼ばれていない"
            assert loop_thread not in self.threads[name], (
                f"{name} がイベントループのスレッドで走った"
            )


class TestManagerCallsRunOffTheEventLoop:
    """Manager calls, including decision reads, run off the event loop."""

    @pytest.mark.parametrize(
        ("method", "path", "kwargs", "expected"),
        [
            ("get", "/persona/p1/memories", {}, ["get_memories"]),
            ("delete", "/persona/p1/memories/m1", {}, ["delete_memory"]),
            (
                "delete",
                "/persona/p1/memories",
                {},
                ["delete_all_memories", "safe_get_memories"],
            ),
            (
                "post",
                "/persona/p1/memories",
                {"data": {"topic_name": "t", "topic_content": "c"}},
                ["add_knowledge", "get_memories"],
            ),
            (
                "post",
                "/persona/p1/memories/upload-preview",
                {"files": {"file": ("k.txt", b"knowledge", "text/plain")}},
                ["ensure_memory_manageable", "convert_file_to_markdown"],
            ),
        ],
    )
    def test_memory_endpoints(self, client, method, path, kwargs, expected):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        rec = _ThreadRecorder()
        memory = Mock()
        memory.get_memories.side_effect = rec.returning("get_memories", ([], 1, 1))
        memory.delete_memory.side_effect = rec.returning("delete_memory", True)
        # On a delete-all failure, also pass through the side that re-reads the list (safe_get_memories)
        memory.delete_all_memories.side_effect = rec.returning(
            "delete_all_memories",
            error=PersonaMemoryManagerError(
                "service down", code=ErrorCode.MEMORY_SERVICE_UNAVAILABLE
            ),
        )
        memory.safe_get_memories.side_effect = rec.returning("safe_get_memories", [])
        memory.add_knowledge.side_effect = rec.returning("add_knowledge")
        memory.ensure_memory_manageable.side_effect = rec.returning(
            "ensure_memory_manageable"
        )
        files = Mock()
        files.convert_file_to_markdown.side_effect = rec.returning(
            "convert_file_to_markdown", "# knowledge"
        )
        with (
            patch(
                "web.routers.persona.get_persona_memory_manager", return_value=memory
            ),
            patch("web.routers.persona.get_file_manager", return_value=files),
        ):
            response = getattr(client, method)(path, **kwargs)

        assert response.status_code == 200
        rec.assert_off_the_loop(client, expected)

    @pytest.mark.parametrize(
        ("path", "data"),
        [
            ("/persona/save", TestPersonaSaveEndpoint._FORM),
            ("/persona/save-selected", {"persona_ids": "cand-1, cand-2"}),
        ],
    )
    def test_candidate_save_endpoints(self, client, path, data):
        from src.models.persona_candidate import CandidateSaveResult

        rec = _ThreadRecorder()
        gen = Mock()
        gen.claim_candidates.side_effect = rec.returning("claim_candidates", "claim")
        gen.finish_candidates.side_effect = rec.returning("finish_candidates")
        manager = Mock()
        manager.save_generated_personas.side_effect = rec.returning(
            "save_generated_personas",
            CandidateSaveResult(saved_ids=("cand-1",), saved_persona_ids=("new-1",)),
        )
        with (
            patch(
                "web.routers.persona.get_persona_generation_manager", return_value=gen
            ),
            patch("web.routers.persona.get_persona_manager", return_value=manager),
        ):
            response = client.post(path, data=data)

        assert response.status_code == 200
        rec.assert_off_the_loop(
            client,
            ["claim_candidates", "save_generated_personas", "finish_candidates"],
        )

    def test_update_and_its_form_re_render(self, client, sample_persona):
        form = {
            "name": "名前",
            "age": "30",
            "occupation": "職業",
            "background": "背景",
            "values": "v",
            "pain_points": "p",
            "goals": "g",
        }
        rec = _ThreadRecorder()
        manager = Mock()
        manager.avatar_upload_enabled.return_value = False
        manager.update_persona.side_effect = rec.returning(
            "update_persona", sample_persona
        )
        manager.describe_persona.side_effect = rec.returning(
            "describe_persona", _detail(sample_persona)
        )
        manager.get_persona_for_edit.side_effect = rec.returning(
            "get_persona_for_edit", sample_persona
        )
        with patch("web.routers.persona.get_persona_manager", return_value=manager):
            saved = client.put(f"/persona/{sample_persona.id}", data=form)
            manager.update_persona.side_effect = rec.returning(
                "update_persona",
                error=PersonaManagerError(
                    "too long",
                    code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                    context={"field": "occupation", "max_length": 100},
                ),
            )
            rejected = client.put(f"/persona/{sample_persona.id}", data=form)

        assert saved.status_code == 200
        assert rejected.status_code == 400
        rec.assert_off_the_loop(
            client, ["update_persona", "describe_persona", "get_persona_for_edit"]
        )


class TestCancelledSaveKeepsTheClaim:
    """Do not release the candidate hold until the save finishes, even if the request is cut off.

    If hold, save and cleanup are handed to separate threads, cancellation runs the
    ``finally`` cleanup mid-save and releases the hold, so a resend could save the
    same candidate again.
    """

    @staticmethod
    def _run_and_cancel(handler, **kwargs):
        import asyncio
        from types import SimpleNamespace

        from src.models.persona_candidate import CandidateSaveResult

        saving = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        order: list[str] = []

        def save(*_args, **_kwargs):
            order.append("save:start")
            saving.set()
            release.wait(5)
            order.append("save:end")
            return CandidateSaveResult(saved_ids=("cand-1",), saved_persona_ids=("n",))

        def finish(*_args, **_kwargs):
            order.append("finish")
            finished.set()

        gen = Mock()
        gen.claim_candidates.return_value = "the-claim"
        gen.finish_candidates.side_effect = finish
        manager = Mock()
        manager.save_generated_personas.side_effect = save
        request = SimpleNamespace(state=SimpleNamespace(principal=Mock()))

        async def scenario():
            task = asyncio.create_task(handler(request, **kwargs))
            assert await asyncio.to_thread(saving.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # When the request is cut off mid-save, the hold is not released yet
            assert not await asyncio.to_thread(finished.wait, 0.3)
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)

        with (
            patch(
                "web.routers.persona.get_persona_generation_manager", return_value=gen
            ),
            patch("web.routers.persona.get_persona_manager", return_value=manager),
        ):
            asyncio.run(scenario())

        assert order == ["save:start", "save:end", "finish"]
        gen.finish_candidates.assert_called_once()
        assert gen.finish_candidates.call_args.args[0] == "the-claim"

    def test_save_one(self):
        from web.routers.persona import save_persona

        form = {
            k: v for k, v in TestPersonaSaveEndpoint._FORM.items() if k != "persona_id"
        }
        self._run_and_cancel(
            save_persona,
            persona_id="cand-1",
            **form,
            gender="",
            country="",
            city="",
            tags="",
            selected_behavior_datasets="",
        )

    def test_save_selected(self):
        from web.routers.persona import save_selected_personas

        self._run_and_cancel(save_selected_personas, persona_ids="cand-1")
