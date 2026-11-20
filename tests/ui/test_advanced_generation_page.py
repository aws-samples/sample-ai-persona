"""高度な生成（ベータ）のページの部品の振る舞い（U1、Step 33）。

実際のページのテンプレートを描き、アプリの JSON・断片・SSE と S3 への PUT は、ページの中の
要求をこのテストが差し替えて答える（起動中のアプリも実 AWS も使わない）。待ち時間（送り直しの
1・2・4 秒、URL の取り直し、SSE のつなぎ直し）は ``window.ADVANCED_UPLOAD_TIMING`` で縮める。
対応: US1.1、US2.1、US3.1、US5.1、US9.1、US10.1、BR2.4、BR2.5、BR3.4、BR8.4、BR10.2、
NFR3.8、NFR5.21、NFR10.1。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from src.managers.advanced_generation.manager import ElapsedClock
from tests import advanced_screen_samples as s
from tests.ui.conftest import UiHarness, render_page
from web.routers.persona_advanced import render_result, screen_limits

ELAPSED = "[data-testid='advanced-elapsed']"
PART_SIZE = 4
#: 6 parts of 4 bytes.
CSV = b"a,b\n1,2\n3,4\n5,6\n7,8\n9,0\n"
FAST = {
    "retryDelaysMs": [10, 20, 40],
    "reconnectDelaysMs": [50, 100],
    "disconnectAfterMs": 2000,
}


def _open(
    ui: UiHarness, *, timing: dict[str, Any] | None = None, **context: Any
) -> None:
    ui.page.add_init_script(
        f"window.ADVANCED_UPLOAD_TIMING = {json.dumps({**FAST, **(timing or {})})};"
    )
    ui.load_component(
        "persona/advanced_generation.html",
        limits=screen_limits(),
        generation_id=context.get("generation_id"),
        elapsed=context.get("elapsed"),
        show_advanced_tab=True,
        active_tab="advanced",
    )
    ui.settle()


class S3Fake:
    """Answers the part PUTs inside the page and records what arrived."""

    def __init__(
        self, ui: UiHarness, *, failures: dict[int, int] | None = None
    ) -> None:
        self.failures = dict(failures or {})
        self.puts: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        ui.page.route("**/__s3__/**", self._handle)

    def _handle(self, route: Any, request: Any) -> None:
        query = parse_qs(urlsplit(request.url).query)
        number = int(query["partNumber"][0])
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.puts.append(
            {
                "number": number,
                "version": query.get("v", ["1"])[0],
                "size": len(request.post_data_buffer or b""),
                "method": request.method,
            }
        )
        self.in_flight -= 1
        if self.failures.get(number, 0) > 0:
            self.failures[number] -= 1
            route.fulfill(status=500, body="")
            return
        route.fulfill(status=200, headers={"ETag": f'"etag-{number}"'}, body="")


def _session(numbers: range | list[int], *, version: int = 1) -> dict[str, Any]:
    return {
        "source_file_id": s.FILE_ID,
        "part_size_bytes": PART_SIZE,
        "part_count": 6,
        "part_urls": [
            {
                "part_number": n,
                "url": f"http://testserver/__s3__/part?partNumber={n}&v={version}",
            }
            for n in numbers
        ],
        "expires_at": "2099-01-01T00:00:00.000000+00:00",
    }


def _json(ui: UiHarness, url: str, body: dict[str, Any], status: int = 200) -> None:
    ui.respond(
        url,
        status=status,
        body=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )


def _ready_row() -> str:
    return render_page(
        "persona/partials/advanced_upload_row.html",
        source_file=s.ready_file(),
        row_error=None,
    )


def _choose_file(ui: UiHarness, data: bytes = CSV, name: str = "members.csv") -> None:
    ui.page.set_input_files(
        "[data-testid='advanced-file-input']",
        files=[{"name": name, "mimeType": "text/csv", "buffer": data}],
    )


def _upload_ready(ui: UiHarness, **fake: Any) -> S3Fake:
    s3 = S3Fake(ui, **fake)
    _json(ui, "/persona/advanced/uploads", _session(range(1, 7)))
    _json(
        ui,
        f"/persona/advanced/uploads/{s.FILE_ID}/complete",
        {"source_file_id": s.FILE_ID, "status": "ready"},
    )
    ui.respond(f"/persona/advanced/uploads/{s.FILE_ID}", body=_ready_row())
    return s3


def _completion_body(ui: UiHarness) -> dict[str, Any]:
    with ui.page.expect_request(
        lambda r: r.url.endswith(f"/uploads/{s.FILE_ID}/complete")
    ) as info:
        _choose_file(ui)
    return json.loads(info.value.post_data or "{}")


class TestFilePicker:
    def test_file_input_is_reachable_by_keyboard_with_a_name(
        self, ui: UiHarness
    ) -> None:
        """見た目を隠したファイルの選択も、キーボードで届き名前を持つ（a11y 3・27）。"""
        _open(ui)
        page = ui.page

        page.focus("[data-testid='advanced-file-input']")
        focused = page.evaluate("document.activeElement.dataset.testid")
        name = page.get_by_label("クリックしてファイルを選択").get_attribute(
            "data-testid"
        )

        assert focused == "advanced-file-input"
        assert name == "advanced-file-input"


class TestTabs:
    def test_tabs_are_links_reachable_by_keyboard_and_mark_the_page(
        self, ui: UiHarness
    ) -> None:
        _open(ui)
        page = ui.page

        page.focus("[data-testid='generation-tab-standard']")
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement.dataset.testid")

        assert focused == "generation-tab-advanced"
        assert (
            page.get_attribute(
                "[data-testid='generation-tab-advanced']", "aria-current"
            )
            == "page"
        )
        assert (
            page.get_attribute(
                "[data-testid='generation-tab-standard']", "aria-current"
            )
            is None
        )
        assert "ベータ" in page.inner_text("[data-testid='generation-tab-advanced']")
        ui.check_a11y()


class TestUpload:
    def test_parts_go_four_at_a_time_and_the_row_becomes_ready(
        self, ui: UiHarness
    ) -> None:
        _open(ui)
        s3 = _upload_ready(ui)

        body = _completion_body(ui)
        ui.page.wait_for_selector(
            "[data-testid='advanced-upload-row'][data-state='ready']"
        )

        assert [p["part_number"] for p in body["parts"]] == [1, 2, 3, 4, 5, 6]
        assert body["parts"][0]["etag"] == '"etag-1"'
        assert sorted(p["number"] for p in s3.puts) == [1, 2, 3, 4, 5, 6]
        assert all(p["method"] == "PUT" for p in s3.puts)
        assert sum(p["size"] for p in s3.puts) == len(CSV)
        assert s3.max_in_flight <= 4
        status = ui.page.inner_text("[data-testid='advanced-upload-status']")
        assert "準備OK" in status
        # 準備OK の表を選ぶと生成を始められる
        button = "[data-testid='advanced-generate-button']"
        assert ui.page.is_disabled(button)
        ui.page.check("[data-testid='advanced-upload-select']")
        assert ui.page.is_enabled(button)
        assert ui.page.inner_text("[data-testid='advanced-generate-reason']") == ""

    def test_failed_part_is_sent_again_up_to_three_times(self, ui: UiHarness) -> None:
        _open(ui)
        s3 = _upload_ready(ui, failures={2: 3})

        _completion_body(ui)

        assert [p["number"] for p in s3.puts].count(2) == 4

    def test_part_failing_four_times_cancels_the_upload(self, ui: UiHarness) -> None:
        _open(ui)
        s3 = S3Fake(ui, failures={1: 99})
        _json(ui, "/persona/advanced/uploads", _session(range(1, 7)))
        ui.respond(f"/persona/advanced/uploads/{s.FILE_ID}", body="")

        with ui.page.expect_request(
            lambda r: r.method == "DELETE" and r.url.endswith(f"/uploads/{s.FILE_ID}")
        ):
            _choose_file(ui)
        ui.page.wait_for_selector(
            "[data-testid='advanced-upload-row'][data-state='error']"
        )

        assert [p["number"] for p in s3.puts].count(1) == 4
        assert "上げられませんでした" in ui.page.inner_text(
            "[data-testid='advanced-upload-status']"
        )
        assert ui.page.locator("[data-testid='advanced-upload-select']").count() == 0

    def test_urls_are_refreshed_before_they_expire(self, ui: UiHarness) -> None:
        # 期限の 5 分前より前に取り直す（期限をすぐに迎える設定で確かめる）
        _open(ui, timing={"refreshLeadMs": 100 * 365 * 24 * 3600 * 1000})
        s3 = _upload_ready(ui)
        refreshed = _session(range(1, 7), version=2)
        refreshed["expires_at"] = "2099-01-01T00:00:00.000000+00:00"
        _json(ui, f"/persona/advanced/uploads/{s.FILE_ID}/part-urls", refreshed)

        with ui.page.expect_request(lambda r: r.url.endswith("/part-urls")) as info:
            _choose_file(ui)
        ui.page.wait_for_selector(
            "[data-testid='advanced-upload-row'][data-state='ready']"
        )

        asked = json.loads(info.value.post_data or "{}")["part_numbers"]
        assert asked and all(1 <= n <= 6 for n in asked)
        assert any(p["version"] == "2" for p in s3.puts)

    def test_too_large_file_is_refused_before_any_request(self, ui: UiHarness) -> None:
        _open(ui)
        ui.page.evaluate(
            "document.getElementById('advanced-generation').dataset.maxBytes = '3'"
        )

        _choose_file(ui)
        ui.page.wait_for_selector("#toast-alert [data-toast]", state="attached")

        assert ui.requests == []
        assert ui.page.locator("[data-testid='advanced-upload-row']").count() == 0

    def test_column_names_and_cells_are_shown_as_text(self, ui: UiHarness) -> None:
        _open(ui)
        _upload_ready(ui)

        _completion_body(ui)
        ui.page.wait_for_selector(
            "[data-testid='advanced-upload-row'][data-state='ready']"
        )
        ui.page.click(f"[data-testid='advanced-columns-{s.FILE_ID}-open-button']")
        ui.page.wait_for_selector(f"#advanced-columns-{s.FILE_ID} [role='dialog']")

        dialog = ui.page.inner_text(f"#advanced-columns-{s.FILE_ID}")
        assert s.SCRIPT_TEXT in dialog
        assert ui.page.locator(f"#advanced-columns-{s.FILE_ID} script").count() == 0
        ui.check_a11y()


class SseFake:
    """Serves the progress stream; each connection gets the next canned body."""

    def __init__(self, ui: UiHarness, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.last_event_ids: list[str | None] = []
        ui.page.route(
            re.compile(r".*/persona/advanced/generations/[^/?]+/events(\?.*)?$"),
            self._handle,
        )

    def _handle(self, route: Any, request: Any) -> None:
        # ブラウザの自動のつなぎ直しはヘッダー、画面のつなぎ直しはクエリで届く
        query = parse_qs(urlsplit(request.url).query).get("last_event_id", [None])[0]
        self.last_event_ids.append(request.all_headers().get("last-event-id") or query)
        body = self.bodies.pop(0) if self.bodies else "event: done\ndata: \n\n"
        route.fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            body=body,
        )


def _frame(event: str, data: str, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines += [f"data: {line}" for line in data.split("\n")]
    return "\n".join(lines) + "\n\n"


class TestProgress:
    def test_reconnects_from_the_last_event_and_closes_on_done(
        self, ui: UiHarness
    ) -> None:
        first = (
            "retry: 50\n\n"
            + _frame("stage", json.dumps({"status": "preparing"}), 1)
            + _frame("progress", json.dumps({"message": "300 行を読み込みました"}), 2)
        )
        second = (
            _frame("stage", json.dumps({"status": "verifying"}), 3)
            + _frame("result", render_result(s.result()), 4)
            + "event: done\ndata: \n\n"
        )
        sse = SseFake(ui, [first, second])
        ui.respond(
            f"/persona/advanced/generations/{s.GENERATION_ID}/record",
            body=render_page(
                "persona/partials/advanced_record_dialog.html", record=s.record()
            ),
        )

        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_selector("[data-testid='advanced-result']")
        ui.page.wait_for_timeout(300)

        assert sse.last_event_ids[:2] == [None, "2"]
        assert len(sse.last_event_ids) == 2  # done のあとはつなぎ直さない
        log = ui.page.inner_text("[data-testid='advanced-log']")
        assert "300 行を読み込みました" in log
        mark = ui.page.inner_text(
            "[data-testid='advanced-stage-preparing'] [data-role='mark']"
        )
        assert mark == "（済）"
        assert ui.page.evaluate("document.activeElement.id") == "advanced-result-title"

    def test_share_badges_are_text_and_dropped_values_are_not_shown(
        self, ui: UiHarness
    ) -> None:
        SseFake(
            ui,
            [
                _frame("result", render_result(s.result()), 4)
                + "event: done\ndata: \n\n"
            ],
        )
        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_selector("[data-testid='advanced-result']")

        badges = ui.page.locator("[data-testid='advanced-share-badge']")
        assert badges.count() == 1
        assert badges.first.inner_text() == "全体の 50%"
        rows = ui.page.locator("[data-testid='advanced-segment-row']")
        assert "50%" in rows.nth(0).inner_text()
        assert "50%" not in rows.nth(1).inner_text()
        assert s.SCRIPT_TEXT in rows.nth(1).inner_text()  # 名前は文字として出る
        ui.page.wait_for_selector("[data-testid='advanced-unverified-warning']")
        # 1 件も選んでいないあいだは保存できない
        save = "[data-testid='advanced-save-button']"
        assert ui.page.is_disabled(save)
        ui.page.check("[data-testid='advanced-candidate-check'] >> nth=0")
        assert ui.page.is_enabled(save)
        assert ui.page.input_value("[data-role='persona-ids']") == "cand-1"
        ui.check_a11y()

    def test_error_event_shows_the_failure_and_opens_the_inputs(
        self, ui: UiHarness
    ) -> None:
        error = {
            "error_code": "generation_timeout",
            "kind": "transient",
            "message": "生成に時間がかかりすぎたため止めました。",
        }
        SseFake(ui, [_frame("error", json.dumps(error), 5) + "event: done\ndata: \n\n"])

        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_selector("[data-testid='advanced-failure']:not([hidden])")

        failure = ui.page.inner_text("[data-testid='advanced-failure']")
        assert "生成が止まりました" in failure
        assert error["message"] in failure
        assert ui.page.evaluate("document.getElementById('advanced-inputs').open")
        ui.check_a11y()

    def test_running_generation_collapses_the_inputs(self, ui: UiHarness) -> None:
        SseFake(
            ui,
            [
                "retry: 60000\n\n"
                + _frame("stage", json.dumps({"status": "running"}), 1)
            ],
        )

        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_selector(
            "[data-testid='advanced-stage-running'][data-state='current']"
        )

        assert not ui.page.evaluate("document.getElementById('advanced-inputs').open")
        assert ui.page.is_disabled("[data-testid='advanced-generate-button']")

    def test_elapsed_time_counts_every_second_without_being_read_out(
        self, ui: UiHarness
    ) -> None:
        # AC9.1.1、interaction-spec: 1 秒ごとに更新するが読み上げない
        # （つなぎ直しても done を返さず、生成が続いているあいだを再現する）
        SseFake(
            ui,
            [
                "retry: 60000\n\n"
                + _frame("stage", json.dumps({"status": "preparing"}), 1)
            ]
            + [": keepalive\n\n"] * 100,
        )

        _open(
            ui,
            generation_id=s.GENERATION_ID,
            elapsed=ElapsedClock(seconds=134, running=True),
        )

        assert ui.page.inner_text(ELAPSED) == "経過 02:14"
        ui.page.wait_for_function(
            "(sel) => document.querySelector(sel).textContent.includes('02:15')",
            arg=ELAPSED,
            timeout=3000,
        )
        assert ui.page.evaluate(
            "(sel) => document.querySelector(sel).closest('[aria-live]') === null",
            ELAPSED,
        )
        ui.check_a11y()

    def test_elapsed_time_stops_when_the_generation_ends(self, ui: UiHarness) -> None:
        SseFake(
            ui,
            [
                _frame("stage", json.dumps({"status": "verifying"}), 1)
                + _frame("result", render_result(s.result()), 2)
                + "event: done\ndata: \n\n"
            ],
        )

        _open(
            ui,
            generation_id=s.GENERATION_ID,
            elapsed=ElapsedClock(seconds=59, running=True),
        )
        ui.page.wait_for_selector("[data-testid='advanced-result']")
        stopped = ui.page.inner_text(ELAPSED)
        ui.page.wait_for_timeout(1500)

        assert ui.page.inner_text(ELAPSED) == stopped

    def test_finished_generation_shows_its_total_without_counting(
        self, ui: UiHarness
    ) -> None:
        SseFake(ui, ["retry: 60000\n\n"])

        _open(
            ui,
            generation_id=s.GENERATION_ID,
            elapsed=ElapsedClock(seconds=180, running=False),
        )
        ui.page.wait_for_timeout(1500)

        assert ui.page.inner_text(ELAPSED) == "経過 03:00"


@pytest.mark.parametrize("state", ["inspecting", "error"])
def test_row_states_are_written_in_words(ui: UiHarness, state: str) -> None:
    row = (
        s.ready_file().update(status=s.SourceFileStatus.INSPECTING)
        if state == "inspecting"
        else s.error_file()
    )
    html = render_page(
        "persona/partials/advanced_upload_row.html",
        source_file=row,
        row_error="このファイルは使えません" if state == "error" else None,
    )
    ui.respond(f"/persona/advanced/uploads/{s.FILE_ID}", body=html)
    _open(ui)
    ui.page.evaluate(
        "(html) => { document.getElementById('advanced-upload-rows').innerHTML = html;"
        " htmx.process(document.getElementById('advanced-upload-rows')); }",
        html,
    )

    text = ui.page.inner_text("[data-testid='advanced-upload-status']")
    assert ("検査中" if state == "inspecting" else "エラー:") in text
    assert ui.page.locator("[data-testid='advanced-upload-select']").count() == 0
