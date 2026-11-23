"""AdvancedGenerationRouter（web/routers/persona_advanced.py）の入口（U1、Step 29）。

Manager は spec 付きの Mock。SSE の読み直しの間隔は 0 にして、終わりまで読む。
対応: US1.1、US1.2、US2.1、US3.1、US5.1、US9.1、US10.1、C1、C2、C3、
BR1.1、BR2.4、BR2.6、BR3.4、BR7.2、NFR1.7、NFR3.9、NFR3.10。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.managers.advanced_generation.manager import (
    AdvancedGenerationError,
    AdvancedPersonaGenerationManager,
    ElapsedClock,
    GenerationProgress,
)
from src.managers.source_upload_manager import SourceUploadError, SourceUploadManager
from src.models.advanced_generation import EventType, GenerationStatus
from src.models.errors import AccessDeniedError, ErrorCode
from src.models.source_file import SourceFileListItem, SourceFileStatus
from tests import advanced_screen_samples as s
from tests.conftest import login_as

pytestmark = pytest.mark.api

TIMEOUT = ErrorCode.GENERATION_TIMEOUT.value


class Adv:
    """The two mocked Managers and the generation pool of the router."""

    def __init__(self) -> None:
        self.uploads = Mock(spec=SourceUploadManager)
        # U2: the page lists the user's files; each row is a real list item
        self.uploads.list_uploads.return_value = []
        self.uploads.list_item.side_effect = lambda f: SourceFileListItem.of(
            f, now=s.NOW, retention_days=7
        )
        self.uploads.inspection_stuck.return_value = False
        self.generations = Mock(spec=AdvancedPersonaGenerationManager)
        self.generations.is_available.return_value = True
        # U6 BR35.4: the box shows only when the Manager offers it
        self.generations.detailed_analysis_offered.return_value = False
        self.generations.running_generation_id.return_value = None
        self.generations.elapsed_clock.return_value = ElapsedClock(
            seconds=0, running=True
        )
        self.pool = Mock()


@pytest.fixture
def adv(monkeypatch: pytest.MonkeyPatch) -> Iterator[Adv]:
    from web.routers import persona_advanced as router

    state = Adv()
    monkeypatch.setattr(router, "get_source_upload_manager", lambda: state.uploads)
    monkeypatch.setattr(
        router, "get_advanced_generation_manager", lambda: state.generations
    )
    monkeypatch.setattr(router, "generation_executor", state.pool)
    monkeypatch.setattr(router, "SSE_POLL_SECONDS", 0)
    yield state


def _sse(text: str) -> list[dict[str, Any]]:
    """Split an event stream into ``{event, id, data, comment}`` blocks."""
    blocks = []
    for raw in text.split("\n\n"):
        if not raw.strip():
            continue
        block: dict[str, Any] = {"data": []}
        for line in raw.split("\n"):
            if line.startswith(":"):
                block["comment"] = line[1:].strip()
            elif ":" in line:
                field, value = line.split(":", 1)
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    block["data"].append(value)
                else:
                    block[field] = value
        block["data"] = "\n".join(block["data"])
        blocks.append(block)
    return blocks


def _tab(html: str, name: str) -> str:
    """The opening ``<a>`` tag of one tab of the strip."""
    found = re.search(rf'<a\b[^>]*data-testid="generation-tab-{name}"[^>]*>', html)
    assert found is not None, f"tab {name} not found"
    return found.group(0)


def _progress(status: GenerationStatus, *events: Any) -> GenerationProgress:
    return GenerationProgress(generation=s.generation(status), events=tuple(events))


class TestPage:
    def test_available_user_gets_the_page_with_the_tab_strip(
        self, client: TestClient, adv: Adv
    ) -> None:
        response = client.get("/persona/generation/advanced")

        assert response.status_code == 200
        html = response.text
        assert 'data-testid="generation-tabs"' in html
        assert 'aria-current="page"' in _tab(html, "advanced")
        assert "aria-current" not in _tab(html, "standard")
        assert "ベータ" in html
        assert 'data-testid="advanced-file-input"' in html
        assert 'data-testid="advanced-progress"' not in html

    def test_the_detailed_analysis_box_shows_checked_when_offered(
        self, client: TestClient, adv: Adv
    ) -> None:
        # U6 BR35.4, AC5.4.4
        adv.generations.detailed_analysis_offered.return_value = True

        html = client.get("/persona/generation/advanced").text

        box = re.search(
            r'<input[^>]*data-testid="advanced-detailed-analysis"[^>]*>', html
        )
        assert box is not None and "checked" in box.group(0)
        assert 'name="use_detailed_analysis"' in box.group(0)

    def test_no_detailed_analysis_box_when_not_offered(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = client.get("/persona/generation/advanced").text

        assert 'data-testid="advanced-detailed-analysis"' not in html
        assert 'data-max-tables="3"' in html

    def test_unavailable_user_goes_back_to_the_ordinary_page(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.is_available.return_value = False

        response = client.get("/persona/generation/advanced", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/persona/generation?notice=advanced_unavailable"
        )

    def test_running_generation_reopens_the_progress_section(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.running_generation_id.return_value = s.GENERATION_ID

        html = client.get("/persona/generation/advanced").text

        assert 'data-testid="advanced-progress"' in html
        assert f'data-generation-id="{s.GENERATION_ID}"' in html

    def test_generation_named_in_the_query_is_reopened(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = client.get(
            f"/persona/generation/advanced?generation={s.GENERATION_ID}"
        ).text

        assert f'data-generation-id="{s.GENERATION_ID}"' in html

    def test_reopened_generation_shows_its_elapsed_time(
        self, client: TestClient, adv: Adv
    ) -> None:
        # AC9.1.1: 開き直しても、生成の始まりからの経過時間が出る
        adv.generations.elapsed_clock.return_value = ElapsedClock(
            seconds=134, running=True
        )

        html = client.get(
            f"/persona/generation/advanced?generation={s.GENERATION_ID}"
        ).text

        assert re.search(
            r'data-testid="advanced-elapsed"[^>]*data-elapsed-seconds="134"', html
        )
        assert "02:14" in html
        adv.generations.elapsed_clock.assert_called_once()

    def test_unreadable_generation_has_no_elapsed_time(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.elapsed_clock.side_effect = AdvancedGenerationError(
            "missing", code=ErrorCode.GENERATION_NOT_FOUND
        )

        response = client.get(
            f"/persona/generation/advanced?generation={s.GENERATION_ID}"
        )

        assert response.status_code == 200
        assert 'data-testid="advanced-progress"' in response.text
        assert 'data-testid="advanced-elapsed"' not in response.text

    def test_signed_out_request_does_not_reach_the_page(
        self, test_app: Any, adv: Adv
    ) -> None:
        with TestClient(test_app, follow_redirects=False) as anonymous:
            response = anonymous.get("/persona/generation/advanced")

        assert response.status_code in (302, 303, 401)
        adv.generations.is_available.assert_not_called()


class TestUploads:
    def test_start_returns_the_part_urls_as_json(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.start_upload.return_value = s.upload_session()

        response = client.post(
            "/persona/advanced/uploads",
            data={
                "filename": "members.csv",
                "size_bytes": str(40 * 1024 * 1024),
                "content_type": "text/csv",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source_file_id"] == s.FILE_ID
        assert [p["part_number"] for p in body["part_urls"]] == [1, 2, 3]
        assert "expires_at" in body
        assert response.headers["cache-control"] == "no-store"
        kwargs = adv.uploads.start_upload.call_args.kwargs
        assert kwargs == {
            "filename": "members.csv",
            "size_bytes": 40 * 1024 * 1024,
            "content_type": "text/csv",
        }

    def test_refused_start_issues_no_url(self, client: TestClient, adv: Adv) -> None:
        adv.uploads.start_upload.side_effect = SourceUploadError(
            "file over the limit",
            code=ErrorCode.UPLOAD_TOO_LARGE,
            context={"max_bytes": 10, "max_size_mb": 1000},
        )

        response = client.post(
            "/persona/advanced/uploads",
            data={
                "filename": "big.csv",
                "size_bytes": "99",
                "content_type": "text/csv",
            },
        )

        assert response.status_code == 413
        body = response.json()
        assert body["error_code"] == ErrorCode.UPLOAD_TOO_LARGE.value
        assert body["kind"] == "capacity"
        assert "1000MB" in body["detail"]
        assert "part_urls" not in body
        assert "file over the limit" not in response.text

    def test_without_ai_execute_is_forbidden(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.start_upload.side_effect = AccessDeniedError("no ai:execute")

        response = client.post(
            "/persona/advanced/uploads",
            data={"filename": "a.csv", "size_bytes": "1", "content_type": "text/csv"},
        )

        assert response.status_code == 403
        assert response.json()["error_code"] == ErrorCode.ACCESS_DENIED.value

    def test_post_without_the_custom_header_is_refused_by_csrf(
        self, test_app: Any, adv: Adv
    ) -> None:
        with TestClient(test_app) as bare:
            login_as(bare)
            response = bare.post(
                "/persona/advanced/uploads",
                data={
                    "filename": "a.csv",
                    "size_bytes": "1",
                    "content_type": "text/csv",
                },
            )

        assert response.status_code == 403
        assert response.json()["error_code"] == ErrorCode.CSRF_FAILED.value
        adv.uploads.start_upload.assert_not_called()

    def test_part_urls_are_refreshed_for_the_numbers_asked(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.refresh_part_urls.return_value = s.upload_session((2, 3))

        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/part-urls",
            json={"part_numbers": [2, 3]},
        )

        assert response.status_code == 200
        assert [p["part_number"] for p in response.json()["part_urls"]] == [2, 3]
        args = adv.uploads.refresh_part_urls.call_args.args
        assert args[1:] == (s.FILE_ID, [2, 3])

    def test_someone_elses_upload_is_not_found(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.refresh_part_urls.side_effect = SourceUploadError(
            "not uploading", code=ErrorCode.UPLOAD_NOT_FOUND
        )

        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/part-urls",
            json={"part_numbers": [1]},
        )

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.UPLOAD_NOT_FOUND.value

    def test_complete_returns_the_inspection_as_json(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.complete_upload.return_value = s.ready_file()

        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/complete",
            json={"parts": [{"part_number": 1, "etag": '"e1"'}]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["source_metadata"]["encoding"] == "cp932"
        assert adv.uploads.complete_upload.call_args.args[1:] == (
            s.FILE_ID,
            [(1, '"e1"')],
        )

    def test_failed_inspection_answers_with_the_code(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.complete_upload.side_effect = SourceUploadError(
            "inspection failed", code=ErrorCode.UPLOAD_SOURCE_INVALID
        )

        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/complete",
            json={"parts": [{"part_number": 1, "etag": "e"}]},
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == ErrorCode.UPLOAD_SOURCE_INVALID.value
        assert "inspection failed" not in response.text

    def test_malformed_parts_are_a_request_error(
        self, client: TestClient, adv: Adv
    ) -> None:
        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/complete",
            json={"parts": "x"},
        )

        assert response.status_code == 422
        adv.uploads.complete_upload.assert_not_called()

    def test_row_fragment_escapes_names_and_cells(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.get_upload.return_value = s.ready_file()

        response = client.get(f"/persona/advanced/uploads/{s.FILE_ID}")

        assert response.status_code == 200
        assert s.SCRIPT_TEXT not in response.text
        assert "&lt;script&gt;" in response.text
        assert 'data-testid="advanced-upload-row"' in response.text
        assert 'value="file-1"' in response.text
        assert s.PART_URL not in response.text

    def test_row_fragment_of_someone_elses_file_is_404(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.get_upload.side_effect = SourceUploadError(
            "x", code=ErrorCode.UPLOAD_NOT_FOUND
        )

        response = client.get(f"/persona/advanced/uploads/{s.FILE_ID}")

        assert response.status_code == 404

    def test_cancel_drops_the_upload(self, client: TestClient, adv: Adv) -> None:
        response = client.delete(f"/persona/advanced/uploads/{s.FILE_ID}")

        assert response.status_code == 200
        assert adv.uploads.cancel_upload.call_args.args[1] == s.FILE_ID

    def test_cancel_of_a_missing_upload_is_a_404_toast(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.cancel_upload.side_effect = SourceUploadError(
            "x", code=ErrorCode.UPLOAD_NOT_FOUND
        )

        response = client.delete(f"/persona/advanced/uploads/{s.FILE_ID}")

        assert response.status_code == 404
        assert "showToast" in response.headers["HX-Trigger"]


class TestStartGeneration:
    def _post(self, client: TestClient, **data: str) -> Any:
        form = {"source_file_ids": s.FILE_ID, "purpose": "", "persona_count": "2"}
        form.update(data)
        return client.post("/persona/advanced/generations", data=form)

    def test_start_hands_the_run_to_the_pool_and_returns_section_c(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.return_value = s.GENERATION_ID

        response = self._post(client, purpose="30代")

        assert response.status_code == 200
        assert f'data-generation-id="{s.GENERATION_ID}"' in response.text
        assert re.search(
            r'data-testid="advanced-elapsed"[^>]*data-elapsed-seconds="0"'
            r'[^>]*data-running="true"',
            response.text,
        )
        assert adv.generations.start_generation.call_args.kwargs == {
            "source_file_ids": [s.FILE_ID],
            "purpose": "30代",
            "persona_count": 2,
            "confirm_plan": False,
            "use_detailed_analysis": False,
        }
        adv.pool.submit.assert_called_once_with(adv.generations.run, s.GENERATION_ID)
        adv.generations.abort_start.assert_not_called()

    def test_pool_refusal_undoes_the_start(self, client: TestClient, adv: Adv) -> None:
        adv.generations.start_generation.return_value = s.GENERATION_ID
        adv.pool.submit.side_effect = RuntimeError("cannot schedule new futures")

        response = self._post(client)

        assert response.status_code == 503
        adv.generations.abort_start.assert_called_once_with(s.GENERATION_ID)
        assert "cannot schedule" not in response.headers.get("HX-Trigger", "")

    def test_running_generation_reopens_its_progress_with_a_409(
        self, client: TestClient, adv: Adv
    ) -> None:
        # U5 BR30.2, AC9.2.2: the notice and the running generation's progress
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "start refused", code=ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        )
        adv.generations.running_generation_id.return_value = s.GENERATION_ID

        response = self._post(client)

        assert response.status_code == 409
        assert response.headers["X-Render-Response"] == "true"
        assert 'data-testid="advanced-already-running"' in response.text
        assert f'data-generation-id="{s.GENERATION_ID}"' in response.text
        assert "start refused" not in response.text
        adv.pool.submit.assert_not_called()

    def test_running_generation_not_found_again_shows_the_notice_only(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "start refused", code=ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        )
        adv.generations.running_generation_id.return_value = None

        response = self._post(client)

        assert response.status_code == 409
        assert 'data-testid="advanced-already-running"' in response.text
        assert 'data-testid="advanced-progress"' not in response.text

    def test_confirm_plan_is_passed_on(self, client: TestClient, adv: Adv) -> None:
        adv.generations.start_generation.return_value = s.GENERATION_ID

        self._post(client, confirm_plan="true")

        assert adv.generations.start_generation.call_args.kwargs["confirm_plan"]

    def test_the_detailed_analysis_box_is_passed_on(
        self, client: TestClient, adv: Adv
    ) -> None:
        # U6 BR35.2: the Manager decides; the Router passes the box as sent
        adv.generations.start_generation.return_value = s.GENERATION_ID

        self._post(client, use_detailed_analysis="true")

        kwargs = adv.generations.start_generation.call_args.kwargs
        assert kwargs["use_detailed_analysis"] is True

    def test_four_tables_are_a_400_in_the_form(
        self, client: TestClient, adv: Adv
    ) -> None:
        # U6 BR37.1: the server refuses too (the screen disables the button)
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "too many tables",
            code=ErrorCode.UPLOAD_SOURCE_INVALID,
            context={"reason": "tables", "max": 3},
        )

        response = self._post(client)

        assert response.status_code == 400
        assert "表は 3 つまで選べます" in response.text
        adv.pool.submit.assert_not_called()

    def test_a_file_not_ready_yet_is_a_409_toast(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "source file is not ready yet",
            code=ErrorCode.GENERATION_SOURCE_NOT_READY,
        )

        response = self._post(client)

        assert response.status_code == 409
        assert "showToast" in response.headers["HX-Trigger"]
        adv.pool.submit.assert_not_called()

    def test_invalid_input_is_shown_inside_the_form(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "purpose too long",
            code=ErrorCode.GENERATION_PURPOSE_TOO_LONG,
            context={"max_chars": 2000},
        )

        response = self._post(client)

        assert response.status_code == 400
        assert response.headers["HX-Retarget"] == "find .advanced-form-error"
        assert response.headers["X-Render-Response"] == "true"
        assert "2000" in response.text
        assert "purpose too long" not in response.text

    def test_start_without_the_custom_header_is_refused(
        self, test_app: Any, adv: Adv
    ) -> None:
        with TestClient(test_app) as bare:
            login_as(bare)
            response = bare.post(
                "/persona/advanced/generations",
                data={"source_file_ids": s.FILE_ID, "persona_count": "2"},
            )

        assert response.status_code == 403
        adv.generations.start_generation.assert_not_called()


class TestFrame:
    """``_frame`` keeps one event intact whatever line breaks the value holds."""

    @pytest.mark.parametrize("data", ["a\r\nb", "a\rb", "a\nb", "a\r\n\rb\n"])
    def test_carriage_returns_do_not_split_the_event(self, data: str) -> None:
        from web.routers.persona_advanced import _frame

        frame = _frame("result", data, 3)

        # SSE は \r\n・\r・\n のどれでも行を分ける。\r が残ると data でない行ができる
        assert "\r" not in frame
        body = frame[: -len("\n\n")]
        assert "\n\n" not in body
        lines = body.split("\n")
        assert lines[:2] == ["event: result", "id: 3"]
        assert all(line.startswith("data: ") for line in lines[2:])
        joined = "\n".join(line[len("data: ") :] for line in lines[2:])
        assert joined == data.replace("\r\n", "\n").replace("\r", "\n")

    def test_empty_data_is_one_empty_data_line(self) -> None:
        from web.routers.persona_advanced import _frame

        assert _frame("done", "") == "event: done\ndata: \n\n"


class TestEvents:
    URL = f"/persona/advanced/generations/{s.GENERATION_ID}/events"

    def test_running_generation_streams_until_done(
        self, client: TestClient, adv: Adv
    ) -> None:
        stage = s.event(
            1, EventType.STAGE, json.dumps({"status": "preparing", "started_at": "t"})
        )
        progress = s.event(
            2, EventType.PROGRESS, json.dumps({"message": "rows_read", "detail": {}})
        )
        result = s.event(3, EventType.RESULT, json.dumps({"persona_count": 2}))
        adv.generations.stream_events.side_effect = [
            _progress(GenerationStatus.PREPARING, stage),
            _progress(GenerationStatus.RUNNING, progress),
            _progress(GenerationStatus.DONE, result),
        ]
        adv.generations.get_result.return_value = s.result()

        response = client.get(self.URL)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        blocks = _sse(response.text)
        names = [b.get("event") for b in blocks if "event" in b]
        assert names == ["stage", "progress", "result", "done"]
        assert [b.get("id") for b in blocks if "event" in b][:3] == ["1", "2", "3"]
        result_html = next(b["data"] for b in blocks if b.get("event") == "result")
        assert 'data-testid="advanced-result"' in result_html
        assert s.SCRIPT_TEXT not in result_html
        after = [
            c.kwargs.get("after_sequence")
            for c in adv.generations.stream_events.call_args_list
        ]
        assert after == [None, 1, 2]

    def test_last_event_id_resumes_after_that_sequence(
        self, client: TestClient, adv: Adv
    ) -> None:
        error = s.event(
            7,
            EventType.ERROR,
            json.dumps({"error_code": TIMEOUT, "kind": "transient"}),
        )
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.FAILED, error
        )

        response = client.get(self.URL, headers={"Last-Event-ID": "6"})

        assert adv.generations.stream_events.call_args.kwargs["after_sequence"] == 6
        blocks = [b for b in _sse(response.text) if "event" in b]
        assert [b["event"] for b in blocks] == ["error", "done"]
        payload = json.loads(blocks[0]["data"])
        assert payload["error_code"] == TIMEOUT
        assert payload["kind"] == "transient"
        assert payload["message"]

    def test_manual_reconnect_resumes_from_the_query_parameter(
        self, client: TestClient, adv: Adv
    ) -> None:
        # 画面が自分でつなぎ直すとき（EventSource はヘッダーを付けられない）
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.FAILED,
            s.event(9, EventType.ERROR, json.dumps({"error_code": TIMEOUT})),
        )

        client.get(f"{self.URL}?last_event_id=8")
        assert adv.generations.stream_events.call_args.kwargs["after_sequence"] == 8

        client.get(f"{self.URL}?last_event_id=8", headers={"Last-Event-ID": "5"})
        assert adv.generations.stream_events.call_args.kwargs["after_sequence"] == 5

        client.get(f"{self.URL}?last_event_id=x")
        assert adv.generations.stream_events.call_args.kwargs["after_sequence"] is None

    def test_finished_generation_sends_only_its_last_result_and_done(
        self, client: TestClient, adv: Adv
    ) -> None:
        events = (
            s.event(1, EventType.STAGE, json.dumps({"status": "preparing"})),
            s.event(2, EventType.PROGRESS, json.dumps({"message": "x"})),
            s.event(3, EventType.RESULT, json.dumps({"persona_count": 2})),
        )
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.DONE, *events
        )
        adv.generations.get_result.return_value = s.result()

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        assert [b["event"] for b in blocks] == ["result", "done"]
        assert blocks[0]["id"] == "3"

    def test_failed_generation_without_an_error_event_still_gets_one(
        self, client: TestClient, adv: Adv
    ) -> None:
        failed = GenerationProgress(
            generation=s.generation(
                GenerationStatus.FAILED,
                error_code=ErrorCode.ADVANCED_GENERATION_BUSY.value,
            ),
            events=(),
        )
        adv.generations.stream_events.return_value = failed

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        assert [b["event"] for b in blocks] == ["error", "done"]
        assert (
            json.loads(blocks[0]["data"])["error_code"]
            == ErrorCode.ADVANCED_GENERATION_BUSY.value
        )

    @pytest.mark.parametrize(
        ("code", "context", "expected"),
        [
            (
                ErrorCode.GENERATION_SOURCE_UNREADABLE,
                {"row_examples": [3, 21], "rows_read": 900, "rows_excluded": 40},
                ["3、21 行目などに問題があります", "900", "40"],
            ),
            (
                ErrorCode.GENERATION_CAPACITY,
                {"rows_read": 1_200_000, "columns_count": 80},
                ["1,200,000 行", "80 列"],
            ),
            (
                ErrorCode.GENERATION_INSUFFICIENT_DATA,
                {"rows_read": 10, "required_rows": 50, "persona_count": 5},
                ["5 人", "必要な行 50", "人数を減らす"],
            ),
        ],
    )
    def test_a_failed_generation_shows_numbers_from_the_stored_context(
        self,
        client: TestClient,
        adv: Adv,
        code: ErrorCode,
        context: dict,
        expected: list[str],
    ) -> None:
        # AC5.2.2, AC5.2.3, AC5.5.1: wording comes from the code and integers
        failed = GenerationProgress(
            generation=s.generation(
                GenerationStatus.FAILED,
                error_code=code.value,
                error_context=context,
            ),
            events=(),
        )
        adv.generations.stream_events.return_value = failed

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        payload = json.loads(blocks[0]["data"])
        assert payload["error_code"] == code.value
        for part in expected:
            assert part in payload["message"]

    def test_keepalive_comment_while_nothing_happens(
        self, client: TestClient, adv: Adv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web.routers import persona_advanced as router

        monkeypatch.setattr(router, "SSE_KEEPALIVE_SECONDS", 0)
        adv.generations.stream_events.side_effect = [
            _progress(GenerationStatus.RUNNING),
            _progress(GenerationStatus.RUNNING),
            _progress(
                GenerationStatus.FAILED,
                s.event(
                    1,
                    EventType.ERROR,
                    json.dumps({"error_code": TIMEOUT, "kind": "transient"}),
                ),
            ),
        ]

        blocks = _sse(client.get(self.URL).text)

        assert any(b.get("comment") == "keepalive" for b in blocks)
        assert all("id" not in b for b in blocks if b.get("comment") == "keepalive")

    def test_someone_elses_generation_is_404(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.stream_events.side_effect = AdvancedGenerationError(
            "generation not found", code=ErrorCode.GENERATION_NOT_FOUND
        )

        response = client.get(self.URL)

        assert response.status_code == 404
        assert "generation not found" not in response.text

    def test_expired_generation_sends_the_expired_error_and_done(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.stream_events.side_effect = AdvancedGenerationError(
            "generation expired", code=ErrorCode.GENERATION_EXPIRED
        )

        response = client.get(self.URL)

        assert response.status_code == 200
        blocks = [b for b in _sse(response.text) if "event" in b]
        assert [b["event"] for b in blocks] == ["error", "done"]
        assert (
            json.loads(blocks[0]["data"])["error_code"]
            == ErrorCode.GENERATION_EXPIRED.value
        )


class TestRecord:
    URL = f"/persona/advanced/generations/{s.GENERATION_ID}/record"

    def test_record_is_shown_with_the_code_escaped(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.get_record.return_value = s.record()

        response = client.get(self.URL)

        assert response.status_code == 200
        assert s.SCRIPT_TEXT not in response.text
        assert "&lt;script&gt;" in response.text
        assert "b" * 64 in response.text
        assert "42" in response.text
        assert "prepare 1.0.0" in response.text

    def test_missing_record_is_404(self, client: TestClient, adv: Adv) -> None:
        adv.generations.get_record.side_effect = AdvancedGenerationError(
            "no analysis record", code=ErrorCode.GENERATION_NOT_FOUND
        )

        response = client.get(self.URL)

        assert response.status_code == 404
        assert response.headers["X-Render-Response"] == "true"
        assert "no analysis record" not in response.text


# --- U2: 一覧・削除・二重の確定・他人からの隔離（W2-1、W2-3、W2-6、W2-8） ----------


def _triggers(response: Any) -> dict[str, Any]:
    return json.loads(response.headers.get("HX-Trigger", "{}"))


class TestSourceList:
    def test_page_lists_the_users_files_through_the_manager(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.list_uploads.return_value = [
            s.ready_file(display_seq=2, expires_at=s.NOW + timedelta(days=7)),
            s.error_file("source_duplicate_column_name"),
        ]

        response = client.get("/persona/generation/advanced")

        assert response.status_code == 200
        html = response.text
        assert html.count('data-testid="advanced-upload-row"') >= 2
        assert "members.csv (2)" in html
        assert "11/27" in html  # the day it goes away
        assert "1 行目に同じ列名が 2 つ以上あります" in html
        principal = adv.uploads.list_uploads.call_args.args[0]
        assert principal.sub == s.OWNER

    def test_page_passes_the_poll_interval(self, client: TestClient, adv: Adv) -> None:
        html = client.get("/persona/generation/advanced").text

        assert 'data-inspect-poll-seconds="2"' in html
        assert 'data-inspect-stop-seconds="900"' in html

    def test_a_deleting_row_says_the_deletion_failed(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.list_uploads.return_value = [
            s.ready_file(status=SourceFileStatus.DELETING)
        ]

        html = client.get("/persona/generation/advanced").text

        assert "削除できませんでした" in html
        assert 'data-testid="advanced-source-delete-open"' in html

    def test_an_unreadable_list_still_shows_the_page(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.list_uploads.side_effect = SourceUploadError(
            "store down /secret/path", code=ErrorCode.DATABASE_OPERATION_FAILED
        )

        response = client.get("/persona/generation/advanced")

        assert response.status_code == 200
        assert "/secret/path" not in response.text
        assert 'data-testid="advanced-upload-list-error"' in response.text


class TestDeleteSource:
    URL = f"/persona/advanced/sources/{s.FILE_ID}"

    def test_delete_removes_the_row_and_closes_the_dialog(
        self, client: TestClient, adv: Adv
    ) -> None:
        response = client.delete(self.URL)

        assert response.status_code == 200
        assert response.text == ""
        assert adv.uploads.delete_source.call_args.args[1] == s.FILE_ID
        assert _triggers(response)["dialogClose"] == {
            "id": f"advanced-delete-{s.FILE_ID}"
        }

    def test_a_file_in_use_is_a_toast(self, client: TestClient, adv: Adv) -> None:
        adv.uploads.delete_source.side_effect = SourceUploadError(
            "in use", code=ErrorCode.SOURCE_FILE_IN_USE
        )

        response = client.delete(self.URL)

        assert response.status_code == 503
        assert "showToast" in _triggers(response)
        assert (
            "生成が終わってから消してください"
            in json.loads(json.dumps(_triggers(response)))["showToast"]["message"]
        )
        assert "in use" not in response.text

    def test_a_failed_s3_deletion_shows_the_deleting_row_and_a_toast(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.delete_source.side_effect = SourceUploadError(
            "s3 failed arn:aws:s3:::bucket", code=ErrorCode.S3_OPERATION_FAILED
        )
        adv.uploads.get_upload.return_value = s.ready_file(
            status=SourceFileStatus.DELETING
        )

        response = client.delete(self.URL)

        assert response.status_code == 200
        assert "削除できませんでした" in response.text
        triggers = _triggers(response)
        assert "showToast" in triggers and "dialogClose" in triggers
        assert "arn:aws" not in response.text + response.headers["HX-Trigger"]

    def test_delete_without_the_custom_header_is_refused_by_csrf(
        self, test_app: Any, adv: Adv
    ) -> None:
        with TestClient(test_app) as bare:
            login_as(bare)
            response = bare.delete(self.URL)

        assert response.status_code == 403
        adv.uploads.delete_source.assert_not_called()


class TestDoubleCompletionRoute:
    def test_a_second_completion_is_200_with_the_current_row(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.complete_upload.return_value = s.uploading_file(
            status=SourceFileStatus.INSPECTING
        )

        response = client.post(
            f"/persona/advanced/uploads/{s.FILE_ID}/complete",
            json={"parts": [{"part_number": 1, "etag": "e"}]},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "inspecting"


def _isolation_routes() -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """(name, manager, method, url, kwargs) of every route that names a file or run."""
    upload = f"/persona/advanced/uploads/{s.FILE_ID}"
    run = f"/persona/advanced/generations/{s.GENERATION_ID}"
    parts = {"json": {"parts": [{"part_number": 1, "etag": "e"}]}}
    return [
        ("get_upload", "uploads", "GET", upload, {}),
        ("complete_upload", "uploads", "POST", f"{upload}/complete", parts),
        (
            "refresh_part_urls",
            "uploads",
            "POST",
            f"{upload}/part-urls",
            {"json": {"part_numbers": [1]}},
        ),
        ("cancel_upload", "uploads", "DELETE", upload, {}),
        (
            "delete_source",
            "uploads",
            "DELETE",
            f"/persona/advanced/sources/{s.FILE_ID}",
            {},
        ),
        (
            "start_generation",
            "generations",
            "POST",
            "/persona/advanced/generations",
            {"data": {"source_file_ids": s.FILE_ID, "persona_count": "2"}},
        ),
        ("stream_events", "generations", "GET", f"{run}/events", {}),
        ("get_record", "generations", "GET", f"{run}/record", {}),
    ]


class TestSomeoneElsesLooksMissing:
    """BR15.1, BR15.2: the same status, code and body for someone else's and none."""

    @pytest.mark.parametrize(
        ("name", "manager", "method", "url", "kwargs"), _isolation_routes()
    )
    def test_same_answer(
        self,
        client: TestClient,
        adv: Adv,
        name: str,
        manager: str,
        method: str,
        url: str,
        kwargs: dict[str, Any],
    ) -> None:
        code = (
            ErrorCode.UPLOAD_NOT_FOUND
            if manager == "uploads"
            else (
                ErrorCode.GENERATION_SOURCE_NOT_FOUND
                if name == "start_generation"
                else ErrorCode.GENERATION_NOT_FOUND
            )
        )
        error_type = (
            SourceUploadError if manager == "uploads" else AdvancedGenerationError
        )
        target = getattr(getattr(adv, manager), name)
        answers = []
        for message in ("owner differs: someone-else", "no such item"):
            target.side_effect = error_type(message, code=code)
            response = client.request(method, url, **kwargs)
            answers.append(
                (
                    response.status_code,
                    response.text,
                    response.headers.get("HX-Trigger"),
                )
            )

        assert answers[0] == answers[1]
        assert answers[0][0] == 404
        assert "someone-else" not in "".join(str(a) for a in answers[0])


class TestAnalysisRecordSources:
    def test_the_generation_record_names_its_file(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.get_record.return_value = s.record().update(
            source_file_ids=("file-1",), source_display_names=("members.csv (2)",)
        )

        html = client.get(
            f"/persona/advanced/generations/{s.GENERATION_ID}/record"
        ).text

        assert "members.csv (2)" in html


class TestDocumentScreen:
    """U4 Step 26: US2.2, US3.3, AC2.2.2, AC3.3.3, AC6.2.4, AC6.3.1, NFR4.4."""

    EVENTS = f"/persona/advanced/generations/{s.GENERATION_ID}/events"

    def test_the_picker_takes_documents_and_names_the_old_word(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = client.get("/persona/generation/advanced").text

        accept = re.search(r'id="advanced-file-input"[^>]*accept="([^"]*)"', html)
        assert accept is not None
        for ext in (".csv", ".tsv", ".pdf", ".docx", ".txt", ".md"):
            assert ext in accept.group(1)
        assert 'data-max-document-bytes="100000000"' in html
        assert ".docx で保存し直して" in html
        assert 'data-testid="advanced-document-estimate"' in html

    def test_a_document_row_shows_its_format_size_and_estimate(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.get_upload.return_value = s.ready_document()

        html = client.get(f"/persona/advanced/uploads/{s.DOCUMENT_ID}").text

        assert 'data-kind="document"' in html
        assert "PDF" in html and "80.0MB" in html
        assert "約 160 万字の見込み" in html
        assert "前準備で確定" in html
        assert re.search(r'type="checkbox"[^>]*name="source_file_ids"', html)
        assert 'data-estimated-chars="1600000"' in html
        assert "列を見る" not in html

    def test_a_table_row_is_chosen_with_the_same_checkbox(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.uploads.get_upload.return_value = s.ready_file()

        html = client.get(f"/persona/advanced/uploads/{s.FILE_ID}").text

        assert 'data-kind="table"' in html
        assert re.search(r'type="checkbox"[^>]*name="source_file_ids"', html)
        assert "列を見る" in html

    def test_two_files_go_to_the_start_together(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.return_value = s.GENERATION_ID

        response = client.post(
            "/persona/advanced/generations",
            data={
                "source_file_ids": [s.FILE_ID, s.DOCUMENT_ID],
                "purpose": "",
                "persona_count": "2",
            },
        )

        assert response.status_code == 200
        assert adv.generations.start_generation.call_args.kwargs["source_file_ids"] == [
            s.FILE_ID,
            s.DOCUMENT_ID,
        ]

    def _result_html(self, client: TestClient, adv: Adv) -> str:
        done = s.event(3, EventType.RESULT, json.dumps({"persona_count": 2}))
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.DONE, done
        )
        adv.generations.get_result.return_value = s.document_result()
        (result,) = [
            b for b in _sse(client.get(self.EVENTS).text) if b.get("event") == "result"
        ]
        return str(result["data"])

    def test_the_result_lists_the_documents_and_their_quotes(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = self._result_html(client, adv)

        assert 'data-testid="advanced-documents"' in html
        assert "120 ページ" in html and "812,345 字" in html
        assert "&lt;script&gt;" in html and s.SCRIPT_TEXT not in html
        assert "p.12 / 第3章 &gt; 不満" in html
        assert "30代の学生の発言" in html
        assert "照合できない根拠 1 件" in html

    def test_evidence_without_a_segment_is_shown_for_the_whole(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = self._result_html(client, adv)

        whole = html.split('data-testid="advanced-overall-evidence"', 1)
        assert len(whole) == 2
        assert "全体に対する根拠" in html
        assert "週末は家族と過ごす時間を何より大切にしている" in whole[1]

    def _failure(self, client: TestClient, adv: Adv, code: ErrorCode, **ctx) -> str:
        failed = GenerationProgress(
            generation=s.generation(
                GenerationStatus.FAILED,
                error_code=code.value,
                error_context={k: v for k, v in ctx.items() if k != "filename"},
            ),
            events=(),
            failure_context=dict(ctx),
        )
        adv.generations.stream_events.return_value = failed
        blocks = [b for b in _sse(client.get(self.EVENTS).text) if "event" in b]
        return str(json.loads(blocks[0]["data"])["message"])

    def test_a_document_without_text_is_named_from_the_users_files(
        self, client: TestClient, adv: Adv
    ) -> None:
        message = self._failure(
            client, adv, ErrorCode.DOCUMENT_NO_TEXT, source_index=1, filename="白紙.pdf"
        )

        assert "白紙.pdf" in message

    def test_too_many_characters_shows_the_count_and_the_limit(
        self, client: TestClient, adv: Adv
    ) -> None:
        message = self._failure(
            client,
            adv,
            ErrorCode.GENERATION_SOURCE_TOO_LARGE,
            total_chars=2_400_000,
            max_chars=2_000_000,
        )

        assert "2,400,000" in message and "2,000,000" in message


# --- U5: the plan card, the cancel and the stream (C1, C3) --------------------


class TestControlRoutes:
    """Step 19: BR29.3, BR29.4, BR30.4, BR31.1, NFR3.26, NFR3.27."""

    PLAN = f"/persona/advanced/generations/{s.GENERATION_ID}/plan"
    CANCEL = f"/persona/advanced/generations/{s.GENERATION_ID}/cancel"

    def test_approve_is_accepted(self, client: TestClient, adv: Adv) -> None:
        response = client.post(self.PLAN, data={"action": "approve"})

        assert response.status_code == 200
        assert 'data-testid="advanced-plan-accepted"' in response.text
        args = adv.generations.decide_plan.call_args
        assert args.args[1:] == (s.GENERATION_ID, "approve")
        assert args.kwargs == {"revised_purpose": None}

    def test_a_cancel_sent_to_the_plan_route_is_refused(
        self, client: TestClient, adv: Adv
    ) -> None:
        # Review fix 3: cancel goes through /cancel only; /plan never accepts it
        adv.generations.decide_plan.side_effect = AdvancedGenerationError(
            "not a plan decision", code=ErrorCode.REQUEST_INVALID
        )

        response = client.post(self.PLAN, data={"action": "cancel"})

        assert response.status_code == 400
        assert response.headers["HX-Retarget"] == "find .advanced-plan-error"
        assert 'data-testid="advanced-plan-accepted"' not in response.text
        assert "承認しました" not in response.text
        assert adv.generations.decide_plan.call_args.args[1:] == (
            s.GENERATION_ID,
            "cancel",
        )
        adv.generations.request_control.assert_not_called()

    def test_replan_sends_the_revised_purpose(
        self, client: TestClient, adv: Adv
    ) -> None:
        response = client.post(
            self.PLAN, data={"action": "replan", "revised_purpose": "20 代"}
        )

        assert response.status_code == 200
        assert 'data-testid="advanced-plan-accepted"' in response.text
        args = adv.generations.decide_plan.call_args
        assert args.args[1:] == (s.GENERATION_ID, "replan")
        assert args.kwargs == {"revised_purpose": "20 代"}

    def test_cancel_says_it_is_cancelling(self, client: TestClient, adv: Adv) -> None:
        response = client.post(self.CANCEL)

        assert response.status_code == 200
        assert 'data-testid="advanced-cancelling"' in response.text
        assert "中止しています" in response.text
        assert adv.generations.request_control.call_args.args[1:] == (
            s.GENERATION_ID,
            "cancel",
        )

    @pytest.mark.parametrize(
        ("url", "code", "status"),
        [
            (PLAN, ErrorCode.GENERATION_NOT_FOUND, 404),
            (CANCEL, ErrorCode.GENERATION_NOT_FOUND, 404),
            (PLAN, ErrorCode.GENERATION_NOT_AWAITING_PLAN, 409),
            (CANCEL, ErrorCode.GENERATION_ALREADY_FINISHED, 409),
        ],
    )
    def test_refusals_are_toasts_with_their_status(
        self, client: TestClient, adv: Adv, url: str, code: ErrorCode, status: int
    ) -> None:
        refusal = AdvancedGenerationError("refused: internal detail", code=code)
        adv.generations.request_control.side_effect = refusal
        adv.generations.decide_plan.side_effect = refusal

        response = client.post(url, data={"action": "approve"})

        assert response.status_code == status
        assert "showToast" in response.headers["HX-Trigger"]
        assert "internal detail" not in response.headers["HX-Trigger"]

    def test_a_too_long_instruction_is_shown_in_the_card(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.decide_plan.side_effect = AdvancedGenerationError(
            "revised purpose too long",
            code=ErrorCode.GENERATION_PURPOSE_TOO_LONG,
            context={"max_chars": 2000},
        )

        response = client.post(
            self.PLAN, data={"action": "replan", "revised_purpose": "x"}
        )

        assert response.status_code == 400
        assert response.headers["HX-Retarget"] == "find .advanced-plan-error"
        assert response.headers["X-Render-Response"] == "true"
        assert "2000" in response.text

    @pytest.mark.parametrize("url", [PLAN, CANCEL])
    def test_without_the_csrf_header_nothing_changes(
        self, test_app: Any, adv: Adv, url: str
    ) -> None:
        with TestClient(test_app) as bare:
            login_as(bare)
            response = bare.post(url, data={"action": "approve"})

        assert response.status_code == 403
        adv.generations.request_control.assert_not_called()
        adv.generations.decide_plan.assert_not_called()


class TestControlStream:
    """Step 19: C3 ``cancelled`` and ``plan`` (BR29.2, BR31.2, NFR3.28)."""

    URL = f"/persona/advanced/generations/{s.GENERATION_ID}/events"

    def test_a_cancel_is_sent_as_cancelled_then_done(
        self, client: TestClient, adv: Adv
    ) -> None:
        stage = s.event(1, EventType.STAGE, json.dumps({"status": "running"}))
        cancelled = s.event(2, EventType.CANCELLED, "{}")
        adv.generations.stream_events.side_effect = [
            _progress(GenerationStatus.RUNNING, stage),
            _progress(GenerationStatus.CANCELLED, cancelled),
        ]

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        assert [b["event"] for b in blocks] == ["stage", "cancelled", "done"]
        assert blocks[1]["data"] == "" and blocks[1]["id"] == "2"

    def test_reconnecting_to_a_cancelled_one_sends_cancelled_and_done(
        self, client: TestClient, adv: Adv
    ) -> None:
        events = (
            s.event(1, EventType.STAGE, json.dumps({"status": "running"})),
            s.event(2, EventType.CANCELLED, "{}"),
        )
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.CANCELLED, *events
        )

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        assert [b["event"] for b in blocks] == ["cancelled", "done"]

    def test_a_cancelled_one_without_its_event_still_gets_cancelled(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.stream_events.return_value = _progress(
            GenerationStatus.CANCELLED
        )

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        assert [b["event"] for b in blocks] == ["cancelled", "done"]

    def test_a_plan_to_confirm_is_sent_as_the_card(
        self, client: TestClient, adv: Adv
    ) -> None:
        plan = s.PLAN.update(interpreted_purpose=s.SCRIPT_TEXT)
        waiting = s.generation(
            GenerationStatus.AWAITING_PLAN_APPROVAL,
            options={"confirm_plan": True},
            plan=plan,
            purpose="30 代",
        )
        event = s.event(3, EventType.PLAN, json.dumps({"method": "cluster"}))
        adv.generations.stream_events.side_effect = [
            GenerationProgress(generation=waiting, events=(event,)),
            _progress(GenerationStatus.CANCELLED),
        ]

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        card = next(b["data"] for b in blocks if b["event"] == "plan")
        assert 'data-testid="advanced-plan-card"' in card
        assert s.SCRIPT_TEXT not in card  # NFR3.28: escaped
        assert "&lt;script&gt;" in card
        assert 'data-testid="advanced-plan-approve"' in card
        assert "30 代" in card  # the current instruction fills the input

    def test_a_plan_without_confirmation_stays_a_log_line(
        self, client: TestClient, adv: Adv
    ) -> None:
        event = s.event(3, EventType.PLAN, json.dumps({"method": "cluster"}))
        adv.generations.stream_events.side_effect = [
            _progress(GenerationStatus.RUNNING, event),
            _progress(GenerationStatus.CANCELLED),
        ]

        blocks = [b for b in _sse(client.get(self.URL).text) if "event" in b]

        plan = next(b["data"] for b in blocks if b["event"] == "plan")
        assert json.loads(plan) == {"method": "cluster"}

    def test_the_page_tells_the_plan_wait_from_config(
        self, client: TestClient, adv: Adv
    ) -> None:
        html = client.get("/persona/generation/advanced").text

        assert 'data-testid="advanced-confirm-plan"' in html
        assert "15 分" in html
