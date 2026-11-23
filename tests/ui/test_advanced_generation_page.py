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
from src.models.source_file import SourceFileListItem, SourceFileStatus
from tests import advanced_screen_samples as s
from tests.ui.conftest import UiHarness, render_page
from web.routers.persona_advanced import (
    render_plan_card,
    render_result,
    screen_limits,
)

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
        upload_rows=context.get("upload_rows", []),
        upload_list_failed=False,
        detailed_analysis_offered=context.get("detailed_analysis_offered", False),
    )
    ui.settle()


def _row_context(
    source_file: Any, row_error: str | None = None, stuck: bool = False
) -> dict[str, Any]:
    """What the router gives one row (U2: the list item and the stuck flag)."""
    return {
        "source_file": source_file,
        "item": SourceFileListItem.of(source_file, now=s.NOW, retention_days=7),
        "row_error": row_error,
        "stuck": stuck,
    }


def _row(source_file: Any, row_error: str | None = None, stuck: bool = False) -> str:
    return render_page(
        "persona/partials/advanced_upload_row.html",
        **_row_context(source_file, row_error, stuck),
    )


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
    return _row(s.ready_file())


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
    html = _row(row, "このファイルは使えません" if state == "error" else None)
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


# --- U2: 一覧・削除・取り直し（US2.4、US3.2、US4.1、US4.2、NFR1.11、NFR10.2） --------

ROWS = "[data-testid='advanced-upload-row']"
WEEK = s.NOW.replace(hour=12) + __import__("datetime").timedelta(days=7)


def _listed(*files: tuple[Any, str | None]) -> list[dict[str, Any]]:
    return [_row_context(f, error) for f, error in files]


def _second_ready(**changes: Any) -> Any:
    return s.ready_file(display_seq=2, expires_at=WEEK, **changes)


class TestFileList:
    def test_rows_show_the_name_the_expiry_and_the_state_in_words(
        self, ui: UiHarness
    ) -> None:
        _open(
            ui,
            upload_rows=_listed(
                (_second_ready(), None),
                (
                    s.error_file("source_empty_column_name").update(
                        source_file_id="file-2", expires_at=WEEK
                    ),
                    "1 行目に空の列名があります。列名を入れて保存し直してください",
                ),
            ),
        )

        rows = ui.page.locator(ROWS)
        assert rows.count() == 2
        first, second = rows.nth(0).inner_text(), rows.nth(1).inner_text()
        assert "members.csv (2)" in first and "準備OK" in first
        assert re.search(r"2026/11/2[67]", first)  # the day it goes away (local)
        assert "エラー:" in second and "列名を入れて保存し直してください" in second
        assert ui.page.locator("[data-testid='advanced-upload-empty']").is_hidden()

    def test_the_delete_button_names_the_file(self, ui: UiHarness) -> None:
        _open(ui, upload_rows=_listed((_second_ready(), None)))

        button = ui.page.get_by_role("button", name="members.csv (2) を削除")

        assert button.count() == 1

    def test_a_failed_deletion_stays_with_its_words_and_a_button(
        self, ui: UiHarness
    ) -> None:
        _open(
            ui,
            upload_rows=_listed(
                (_second_ready(status=SourceFileStatus.DELETING), None)
            ),
        )

        text = ui.page.locator(ROWS).inner_text()
        assert "削除できませんでした" in text
        assert ui.page.get_by_role("button", name="members.csv (2) を削除").count() == 1

    def test_a_file_in_use_cannot_be_deleted_and_says_why(self, ui: UiHarness) -> None:
        busy = _second_ready(in_use_by="gen-1", in_use_until=WEEK)
        _open(ui, upload_rows=_listed((busy, None)))

        button = ui.page.get_by_role("button", name="members.csv (2) を削除")
        assert button.get_attribute("aria-disabled") == "true"
        reason = button.get_attribute("aria-describedby")
        assert ui.page.inner_text(f"#{reason}") == "生成中のため削除できません"
        button.dispatch_event("click")  # aria-disabled: Playwright would not click it
        assert ui.page.locator("[role='dialog']:visible").count() == 0
        assert ui.page.locator("[data-testid='advanced-upload-select']").count() == 0

    def test_the_list_has_no_axe_violations(self, ui: UiHarness) -> None:
        _open(
            ui,
            upload_rows=_listed(
                (_second_ready(), None),
                (
                    _second_ready(
                        source_file_id="file-3",
                        in_use_by="gen-1",
                        in_use_until=WEEK,
                    ),
                    None,
                ),
                (
                    _second_ready(
                        source_file_id="file-4", status=SourceFileStatus.DELETING
                    ),
                    None,
                ),
                (
                    s.error_file("source_not_a_table").update(
                        source_file_id="file-2", expires_at=WEEK
                    ),
                    "CSV として読めませんでした",
                ),
            ),
        )

        ui.check_a11y()


class TestDeleteFlow:
    def test_keyboard_only_confirm_removes_the_row_and_announces_it(
        self, ui: UiHarness
    ) -> None:
        ui.respond(
            f"/persona/advanced/sources/{s.FILE_ID}",
            body="",
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "dialogClose": {"id": f"advanced-delete-{s.FILE_ID}"},
                        "advancedSourceDeleted": {},
                    }
                )
            },
        )
        _open(ui, upload_rows=_listed((_second_ready(), None)))

        ui.page.get_by_role("button", name="members.csv (2) を削除").focus()
        ui.page.keyboard.press("Enter")
        dialog = ui.page.get_by_role("dialog")
        dialog.wait_for()
        assert "members.csv (2)" in dialog.inner_text()
        ui.page.get_by_role("button", name="削除する").focus()
        ui.page.keyboard.press("Enter")

        ui.page.locator(ROWS).wait_for(state="detached")
        assert "members.csv (2) を削除しました" in ui.page.inner_text(
            "#advanced-upload-announcer"
        )
        assert ui.requests.count(f"/persona/advanced/sources/{s.FILE_ID}") == 1
        assert ui.page.locator("[data-testid='advanced-upload-empty']").is_visible()

    def test_cancel_keeps_the_row(self, ui: UiHarness) -> None:
        _open(ui, upload_rows=_listed((_second_ready(), None)))

        ui.page.get_by_role("button", name="members.csv (2) を削除").click()
        ui.page.get_by_role("button", name="やめる").click()

        assert ui.page.locator(ROWS).count() == 1
        ui.verify_requests()


class TestInspectingRow:
    TIMING = {"inspectPollMs": 50, "inspectStopMs": 400}

    def test_an_inspecting_row_is_read_again_until_it_is_ready(
        self, ui: UiHarness
    ) -> None:
        inspecting = s.ready_file(status=SourceFileStatus.INSPECTING, metadata=None)
        ui.respond(f"/persona/advanced/uploads/{s.FILE_ID}", body=_ready_row())
        _open(ui, timing=self.TIMING, upload_rows=_listed((inspecting, None)))

        ui.page.locator(f"{ROWS}[data-state='ready']").wait_for()

        assert "準備OK" in ui.page.locator(ROWS).inner_text()

    def test_the_rereading_stops_after_the_limit_with_a_retry_button(
        self, ui: UiHarness
    ) -> None:
        inspecting = s.ready_file(status=SourceFileStatus.INSPECTING, metadata=None)
        ui.respond(f"/persona/advanced/uploads/{s.FILE_ID}", body=_row(inspecting))
        _open(ui, timing=self.TIMING, upload_rows=_listed((inspecting, None)))

        ui.page.get_by_role("button", name="確定し直す").wait_for()
        reads = ui.requests.count(f"/persona/advanced/uploads/{s.FILE_ID}")
        ui.page.wait_for_timeout(300)

        assert 2 <= reads <= 12
        assert ui.requests.count(f"/persona/advanced/uploads/{s.FILE_ID}") == reads

    def test_the_poll_interval_comes_from_the_page(self, ui: UiHarness) -> None:
        _open(ui)

        page = ui.page.locator("#advanced-generation")
        assert page.get_attribute("data-inspect-poll-seconds") == "2"
        assert page.get_attribute("data-inspect-stop-seconds") == "900"


class TestDocuments:
    """U4 Step 26: AC2.2.2, AC3.3.3, AC6.2.4, NFR10.3 (rows, estimate, result)."""

    SELECT = "[data-testid='advanced-upload-select']"

    def _open_with_both(self, ui: UiHarness) -> None:
        _open(
            ui,
            upload_rows=_listed(
                (s.ready_file(expires_at=WEEK), None),
                (s.ready_document(expires_at=WEEK), None),
            ),
        )

    def test_a_document_row_is_written_in_words_and_its_estimate_adds_up(
        self, ui: UiHarness
    ) -> None:
        self._open_with_both(ui)
        estimate = "[data-testid='advanced-document-estimate']"
        assert ui.page.is_hidden(estimate)

        ui.page.check(f"tr[data-kind='document'] {self.SELECT}")

        assert "PDF / 約 160 万字の見込み" in ui.page.inner_text(
            "tr[data-kind='document']"
        )
        assert "約 160 万字の見込み" in ui.page.inner_text(estimate)
        assert ui.page.is_enabled("[data-testid='advanced-generate-button']")
        ui.page.check(f"tr[data-kind='table'] {self.SELECT}")
        assert ui.page.is_enabled("[data-testid='advanced-generate-button']")
        ui.check_a11y()

    def test_an_old_word_file_is_refused_with_advice(self, ui: UiHarness) -> None:
        _open(ui)

        _choose_file(ui, data=b"\xd0\xcf\x11\xe0", name="old.doc")
        ui.page.wait_for_selector("#toast-alert [data-toast]", state="attached")

        assert ui.requests == []
        assert ".docx" in ui.page.inner_text("#toast-alert")

    def test_the_result_lists_the_documents_and_the_overall_evidence(
        self, ui: UiHarness
    ) -> None:
        SseFake(
            ui,
            [
                _frame("result", render_result(s.document_result()), 4)
                + "event: done\ndata: \n\n"
            ],
        )
        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_selector("[data-testid='advanced-documents']")

        documents = ui.page.inner_text("[data-testid='advanced-documents']")
        assert "120 ページ" in documents and "812,345 字" in documents
        assert s.SCRIPT_TEXT in documents  # the file name is shown as text
        overall = ui.page.inner_text("[data-testid='advanced-overall-evidence']")
        assert "週末は家族と過ごす時間を何より大切にしている" in overall
        ui.check_a11y()


# --- U5: the plan card and the cancel ------------------------------------------

CANCEL_URL = f"/persona/advanced/generations/{s.GENERATION_ID}/cancel"
PLAN_URL = f"/persona/advanced/generations/{s.GENERATION_ID}/plan"
#: The stream ends after its body; the page waits long before it reconnects.
HOLD = {"reconnectDelaysMs": [60000]}


def _card_stream() -> str:
    waiting = s.generation(
        s.GenerationStatus.AWAITING_PLAN_APPROVAL,
        options={"confirm_plan": True},
        plan=s.PLAN.update(interpreted_purpose=s.SCRIPT_TEXT),
        purpose="30 代",
    )
    return (
        "retry: 60000\n\n"
        + _frame("stage", json.dumps({"status": "planning"}), 1)
        + _frame("stage", json.dumps({"status": "awaiting_plan_approval"}), 2)
        + _frame("plan", render_plan_card(waiting), 3)
    )


class TestGenerationControls:
    """Step 21: BR29.2, BR31.4, BR33.3, NFR3.28, NFR10.4, AC8.1.1, AC9.3.1."""

    def test_the_plan_confirmation_is_off_by_default(self, ui: UiHarness) -> None:
        _open(ui)

        box = "[data-testid='advanced-confirm-plan']"
        assert not ui.page.is_checked(box)
        help_text = ui.page.inner_text("#advanced-confirm-plan-help")
        assert f"{screen_limits()['plan_wait_minutes']} 分" in help_text

    def test_the_plan_card_shows_the_plan_as_text_and_its_buttons(
        self, ui: UiHarness
    ) -> None:
        SseFake(ui, [_card_stream()])

        _open(ui, generation_id=s.GENERATION_ID, timing=HOLD)
        ui.page.wait_for_selector("[data-testid='advanced-plan-card']")

        purpose = ui.page.inner_text("[data-testid='advanced-plan-purpose']")
        assert purpose == s.SCRIPT_TEXT  # NFR3.28: shown as text, never run
        assert ui.page.evaluate("window.__xss === undefined")
        stage = "[data-testid='advanced-stage-awaiting_plan_approval']"
        assert ui.page.is_visible(stage)
        assert ui.page.get_attribute(stage, "data-state") == "current"
        assert ui.page.input_value("[data-testid='advanced-plan-revised']") == "30 代"
        for name in ("approve", "replan", "cancel"):
            ui.page.focus(f"[data-testid='advanced-plan-{name}']")
            assert ui.active("document.activeElement.dataset.testid") == (
                f"advanced-plan-{name}"
            )
        ui.check_a11y()

    def test_approving_replaces_the_card_with_its_answer(self, ui: UiHarness) -> None:
        SseFake(ui, [_card_stream()])
        ui.respond(
            PLAN_URL,
            body=render_page(
                "persona/partials/advanced_plan_card.html",
                generation_id=s.GENERATION_ID,
                plan=None,
                purpose="",
                awaiting=False,
                accepted="approve",
                limits=screen_limits(),
            ),
        )
        _open(ui, generation_id=s.GENERATION_ID, timing=HOLD)
        ui.page.wait_for_selector("[data-testid='advanced-plan-approve']")

        ui.page.click("[data-testid='advanced-plan-approve']")
        ui.page.wait_for_selector("[data-testid='advanced-plan-accepted']")

        assert PLAN_URL in ui.requests
        assert ui.page.locator("[data-testid='advanced-plan-approve']").count() == 0

    def test_cancel_says_cancelling_then_cancelled_and_keeps_the_form(
        self, ui: UiHarness
    ) -> None:
        SseFake(
            ui,
            [
                "retry: 50\n\n" + _frame("stage", json.dumps({"status": "running"}), 1),
                _frame("cancelled", "", 2) + "event: done\ndata: \n\n",
            ],
        )
        ui.respond(
            CANCEL_URL,
            body=render_page(
                "persona/partials/advanced_cancel.html",
                generation_id=s.GENERATION_ID,
                cancelling=True,
            ),
        )
        # the cancelled frame comes on the next connection, 1.5 s later
        _open(ui, generation_id=s.GENERATION_ID, timing={"reconnectDelaysMs": [1500]})
        # the inputs fold while a generation runs; the value is set directly
        ui.page.evaluate(
            "document.querySelector(\"[data-testid='advanced-purpose']\").value"
            " = '首都圏の 30 代'"
        )
        ui.page.wait_for_selector(
            "[data-testid='advanced-stage-running'][data-state='current']"
        )

        ui.page.click("[data-testid='advanced-cancel-button']")
        ui.page.wait_for_selector("[data-testid='advanced-cancelled']:not([hidden])")

        assert CANCEL_URL in ui.requests
        assert ui.page.is_hidden("[data-testid='advanced-failure']")
        assert ui.page.is_hidden("[data-testid='advanced-cancel-area']")
        assert "中止しました" in ui.page.inner_text(
            "[data-testid='advanced-cancelled']"
        )
        # BR33.3: the form stays as it was and no longer says a run is going on
        assert ui.page.input_value("[data-testid='advanced-purpose']") == (
            "首都圏の 30 代"
        )
        reason = ui.page.inner_text("[data-testid='advanced-generate-reason']")
        assert "実行中" not in reason
        ui.check_a11y()

    def test_the_cancel_answer_disables_the_button(self, ui: UiHarness) -> None:
        SseFake(ui, ["retry: 60000\n\n" + _frame("stage", '{"status": "running"}', 1)])
        ui.respond(
            CANCEL_URL,
            body=render_page(
                "persona/partials/advanced_cancel.html",
                generation_id=s.GENERATION_ID,
                cancelling=True,
            ),
        )
        _open(ui, generation_id=s.GENERATION_ID, timing=HOLD)

        ui.page.click("[data-testid='advanced-cancel-button']")
        ui.page.wait_for_selector("[data-testid='advanced-cancelling']")

        assert ui.page.is_disabled("[data-testid='advanced-cancel-button']")


# ---------------------------------------------------------------------------
# U6: the detailed analysis box, up to three tables, the record's code kinds
# and the settings section (BR34, BR35.4, BR37.1, BR38.1, NFR3.37, NFR10.5)
# ---------------------------------------------------------------------------

GENERATE = "[data-testid='advanced-generate-button']"


def _tables(count: int) -> list[dict[str, Any]]:
    return [
        _row_context(s.ready_file(source_file_id=f"table-{i}")) for i in range(count)
    ]


def _check_all(ui: UiHarness) -> None:
    for box in ui.page.query_selector_all("[data-testid='advanced-upload-select']"):
        box.check()
    ui.settle()


class TestDetailedAnalysisAndTables:
    def test_the_box_is_checked_labelled_and_reachable(self, ui: UiHarness) -> None:
        _open(ui, detailed_analysis_offered=True)

        box = "[data-testid='advanced-detailed-analysis']"
        assert ui.page.is_checked(box)
        ui.page.focus(box)
        ui.page.keyboard.press("Space")
        assert not ui.page.is_checked(box)
        label = ui.page.inner_text("label[for='advanced-detailed-analysis']")
        assert label == "AI による詳細分析"
        ui.check_a11y()

    def test_no_box_when_not_offered(self, ui: UiHarness) -> None:
        _open(ui)

        assert (
            ui.page.query_selector("[data-testid='advanced-detailed-analysis']") is None
        )

    def test_three_tables_may_start(self, ui: UiHarness) -> None:
        _open(ui, upload_rows=_tables(3))

        _check_all(ui)

        assert not ui.page.is_disabled(GENERATE)

    def test_four_tables_cannot_start_and_say_why(self, ui: UiHarness) -> None:
        _open(ui, upload_rows=_tables(4))

        _check_all(ui)

        assert ui.page.is_disabled(GENERATE)
        reason = ui.page.inner_text("[data-testid='advanced-generate-reason']")
        assert "CSV は 3 つまで選べます" in reason
        ui.check_a11y()


def _record_with_ai_code() -> Any:
    record = s.record()
    return record.update(
        code_blocks=(
            *record.code_blocks,
            {
                "kind": "ai_written",
                "code": f"emit_result(df, 'seg', 'source_1')  # {s.SCRIPT_TEXT}",
                "sha256": "c" * 64,
                "title": "結合した列: customer_id, source_2_amount_sum（source_2 の一致 80%）",
            },
        )
    )


class TestProgressBar:
    def test_bar_follows_the_stages_in_words_and_value(self, ui: UiHarness) -> None:
        SseFake(
            ui,
            [
                _frame("stage", json.dumps({"status": "running"}), 1)
                + _frame("progress", json.dumps({"message": "x"}), 2)
            ],
        )
        _open(ui, generation_id=s.GENERATION_ID)
        ui.page.wait_for_function(
            "document.querySelector(\"[data-testid='advanced-progress-count']\")"
            ".textContent.includes('/')"
        )

        count = ui.page.inner_text("[data-testid='advanced-progress-count']")
        assert count == "3 / 5 段階"
        assert "実行" in ui.page.inner_text("[data-testid='advanced-progress-label']")
        bar = ui.page.locator("[data-testid='advanced-progress-bar']")
        assert bar.get_attribute("aria-valuenow") == "40"
        assert bar.get_attribute("role") == "progressbar"
        ui.check_a11y()


class TestCandidateCardHasTheOrdinaryProfile:
    def test_background_values_pain_points_and_goals_are_shown(
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

        card = ui.page.locator("[data-testid='advanced-candidate']").first
        assert (
            "背景"
            in card.locator(
                "[data-testid='advanced-candidate-background']"
            ).inner_text()
        )
        assert (
            "効率"
            in card.locator("[data-testid='advanced-candidate-values']").inner_text()
        )
        assert (
            "時間"
            in card.locator(
                "[data-testid='advanced-candidate-pain_points']"
            ).inner_text()
        )
        assert (
            "成長"
            in card.locator("[data-testid='advanced-candidate-goals']").inner_text()
        )
        assert (
            "歳"
            in card.locator("[data-testid='advanced-candidate-profile']").inner_text()
        )
        ui.check_a11y()


class TestRecordReadsAsTheFlowOfTheAnalysis:
    """The record is for looking back: purpose, plan, what ran, files, code."""

    def test_flow_is_shown_and_internal_values_are_not(self, ui: UiHarness) -> None:
        record = s.record().update(
            tool_calls=(
                {"tool_call_id": "t1", "tool": "profile_dataset", "ok": True},
                {"tool_call_id": "t2", "tool": "run_code", "ok": False},
            ),
            source_display_names=("members.csv",),
        )
        ui.load_component(
            "fragment.html",
            container_id="advanced-record-area",
            fragment="<h2>分析の記録</h2>"
            + render_page(
                "persona/partials/advanced_record_dialog.html", record=record
            ),
        )
        ui.settle()

        text = ui.page.inner_text("[data-testid='advanced-record']")
        for heading in (
            "読み取った目的",
            "分析の計画",
            "実際に行った分析",
            "使ったファイル",
        ):
            assert heading in text
        assert "members.csv" in text
        calls = ui.page.locator("[data-testid='advanced-record-call']")
        assert calls.count() == 2
        assert "データの全体像" in calls.nth(0).inner_text()
        assert (
            ui.page.locator("[data-testid='advanced-record-call-failed']").count() == 1
        )
        for hidden in ("チェックサム", "ハッシュ", "乱数のシード", "前準備の版"):
            assert hidden not in text
        ui.check_a11y()


class TestRecordCodeKinds:
    def test_kinds_are_written_and_code_is_text(self, ui: UiHarness) -> None:
        ui.load_component(
            "fragment.html",
            container_id="advanced-record-area",
            # the dialog's own title stands above its content
            fragment="<h2>分析の記録</h2>"
            + render_page(
                "persona/partials/advanced_record_dialog.html",
                record=_record_with_ai_code(),
            ),
        )
        ui.settle()

        kinds = ui.page.locator("[data-testid='advanced-record-code-kind']")
        assert "決まった関数" in kinds.nth(0).inner_text()
        # what was done is told in words, not by the function name
        assert "似た特徴の人どうしをまとめて" in kinds.nth(0).inner_text()
        assert "cluster_segments" not in kinds.nth(0).inner_text()
        assert "AI が書いたプログラムで" in kinds.nth(1).inner_text()
        assert "AI による詳細分析" in kinds.nth(1).inner_text()
        joined = ui.page.inner_text("[data-testid='advanced-record-joined']")
        assert "customer_id" in joined and "80%" in joined
        ui.page.locator("[data-testid='advanced-record-code']").nth(1).locator(
            "summary"
        ).click()
        assert s.SCRIPT_TEXT in ui.page.locator("pre code").nth(1).inner_text()
        assert ui.page.query_selector("script:has-text('alert(1)')") is None
        ui.check_a11y()


def _load_plan_card(ui: UiHarness, plan: Any) -> None:
    ui.load_component(
        "fragment.html",
        container_id="advanced-plan-area",
        fragment="<h2>計画</h2>"
        + render_page(
            "persona/partials/advanced_plan_card.html",
            generation_id=s.GENERATION_ID,
            plan=plan,
            purpose="",
            awaiting=False,
            accepted=None,
            limits=screen_limits(),
        ),
    )
    ui.settle()


class TestPlanSteps:
    """U6 addendum: the plan card numbers the steps; the record names playbooks."""

    def test_the_card_numbers_the_steps_with_their_names_and_reasons(
        self, ui: UiHarness
    ) -> None:
        from src.models.analysis import AnalysisStep, AnalysisStepKind

        plan = s.PLAN.update(
            steps=(
                AnalysisStep(AnalysisStepKind.JOIN, "購入の表を足す"),
                AnalysisStep(AnalysisStepKind.RFM, s.SCRIPT_TEXT),
                AnalysisStep(AnalysisStepKind.COMPARE_GROUPS, "差を確かめる"),
            )
        )

        _load_plan_card(ui, plan)

        steps = ui.page.locator("[data-testid='advanced-plan-step']")
        assert steps.count() == 3
        assert [steps.nth(i).get_attribute("data-kind") for i in range(3)] == [
            "join",
            "rfm",
            "compare_groups",
        ]
        assert steps.nth(0).inner_text() == "CSV を結合: 購入の表を足す"
        assert "新しさ・頻度・金額で点数化" in steps.nth(1).inner_text()
        assert s.SCRIPT_TEXT in steps.nth(1).inner_text()  # text, never run
        assert ui.page.evaluate("window.__xss === undefined")
        assert "群の差を検定" in steps.nth(2).inner_text()
        tag = ui.page.eval_on_selector(
            "[data-testid='advanced-plan-steps']", "e => e.tagName"
        )
        assert tag == "OL"
        # the method stays as before
        assert ui.page.is_visible("[data-testid='advanced-plan-method']")
        ui.check_a11y()

    def test_a_plan_without_steps_shows_its_method_as_one_step(
        self, ui: UiHarness
    ) -> None:
        _load_plan_card(ui, s.PLAN)

        steps = ui.page.locator("[data-testid='advanced-plan-step']")
        assert steps.count() == 1
        assert steps.nth(0).inner_text() == "似た人をまとめる: 指示が空欄のため"

    def test_the_record_names_the_playbooks_read(self, ui: UiHarness) -> None:
        record = s.record().update(
            tool_calls=(
                {
                    "tool_call_id": "tc-1",
                    "tool": "load_playbook",
                    "ok": True,
                    "playbook": "rfm",
                },
                {
                    "tool_call_id": "tc-2",
                    "tool": "load_playbook",
                    "ok": True,
                    "playbook": "join",
                },
                {
                    "tool_call_id": "tc-3",
                    "tool": "load_playbook",
                    "ok": True,
                    "playbook": "rfm",
                },
            )
        )
        ui.load_component(
            "fragment.html",
            container_id="advanced-record-area",
            fragment="<h2>分析の記録</h2>"
            + render_page(
                "persona/partials/advanced_record_dialog.html", record=record
            ),
        )
        ui.settle()

        names = ui.page.locator("[data-testid='advanced-record-playbook']")
        assert [names.nth(i).get_attribute("data-name") for i in range(2)] == [
            "rfm",
            "join",
        ]
        assert names.count() == 2
        assert "新しさ・頻度・金額で点数化" in names.nth(0).inner_text()
        ui.check_a11y()

    def test_a_record_without_playbooks_says_nothing_of_them(
        self, ui: UiHarness
    ) -> None:
        ui.load_component(
            "fragment.html",
            container_id="advanced-record-area",
            fragment="<h2>分析の記録</h2>"
            + render_page(
                "persona/partials/advanced_record_dialog.html", record=s.record()
            ),
        )
        ui.settle()

        assert ui.page.locator("[data-testid='advanced-record-playbooks']").count() == 0


def _section_view(**setting: Any) -> Any:
    from src.managers.components.advanced_generation_policy import (
        AdvancedGenerationSetting,
        SettingSectionView,
    )
    from src.models.authz import Department

    values: dict[str, Any] = {
        "audience_mode": "departments",
        "department_groups": ("sales", "gone"),
        "allow_detailed_analysis": False,
        "version": 4,
        "updated_by": "admin-1",
        "updated_at": "2026-11-20T09:00:00+00:00",
    }
    values.update(setting)
    return SettingSectionView(
        setting=AdvancedGenerationSetting(**values),
        departments=(Department("sales", "営業部"), Department("hr", "人事部")),
        missing_groups=("gone",),
        updated_by_name="管理 太郎",
    )


class TestSettingsSection:
    def _open(self, ui: UiHarness, **setting: Any) -> None:
        ui.load_component(
            "fragment.html",
            container_id="advanced-generation-settings",
            fragment=render_page(
                "settings/partials/advanced_generation_settings.html",
                view=_section_view(**setting),
                error=None,
                form=None,
            ),
        )
        ui.settle()

    def test_the_section_shows_its_values_and_the_version(self, ui: UiHarness) -> None:
        self._open(ui)

        assert ui.page.is_checked("[data-testid='advanced-setting-departments']")
        assert ui.page.input_value("input[name='version']") == "4"
        assert "（見つからない部署）" in ui.page.inner_text(
            "[data-testid='department-picker']"
        )
        assert "管理 太郎" in ui.page.inner_text(
            "[data-testid='advanced-setting-updated']"
        )
        ui.check_a11y()

    def test_the_switch_is_a_labelled_toggle_not_only_a_colour(
        self, ui: UiHarness
    ) -> None:
        self._open(ui)
        switch = "[data-testid='advanced-setting-detailed']"

        assert ui.page.get_attribute(switch, "aria-checked") == "false"
        assert "OFF" in ui.page.inner_text(switch)
        ui.page.focus(switch)
        ui.page.keyboard.press("Enter")
        ui.settle()

        assert ui.page.get_attribute(switch, "aria-checked") == "true"
        assert "ON" in ui.page.inner_text(switch)
        hidden = "input[name='allow_detailed_analysis']"
        assert ui.page.input_value(hidden) == "true"
        ui.check_a11y()

    def test_the_save_button_stays_usable_without_a_department_change(
        self, ui: UiHarness
    ) -> None:
        self._open(ui)

        assert not ui.page.is_disabled("[data-testid='advanced-setting-save']")
