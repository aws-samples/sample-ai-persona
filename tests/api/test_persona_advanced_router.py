"""AdvancedGenerationRouter（web/routers/persona_advanced.py）の入口（U1、Step 29）。

Manager は spec 付きの Mock。SSE の読み直しの間隔は 0 にして、終わりまで読む。
対応: US1.1、US1.2、US2.1、US3.1、US5.1、US9.1、US10.1、C1、C2、C3、
BR1.1、BR2.4、BR2.6、BR3.4、BR7.2、NFR1.7、NFR3.9、NFR3.10。
"""

from __future__ import annotations

import json
import re
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
from tests import advanced_screen_samples as s
from tests.conftest import login_as

pytestmark = pytest.mark.api

TIMEOUT = ErrorCode.GENERATION_TIMEOUT.value


class Adv:
    """The two mocked Managers and the generation pool of the router."""

    def __init__(self) -> None:
        self.uploads = Mock(spec=SourceUploadManager)
        self.generations = Mock(spec=AdvancedPersonaGenerationManager)
        self.generations.is_available.return_value = True
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

    def test_running_generation_is_a_409_toast(
        self, client: TestClient, adv: Adv
    ) -> None:
        adv.generations.start_generation.side_effect = AdvancedGenerationError(
            "start refused", code=ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
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
