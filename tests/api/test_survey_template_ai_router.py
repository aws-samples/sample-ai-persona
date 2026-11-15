"""Survey ルーターの API テスト（AI でのテンプレート作成、レポート生成、ペルソナ元データの取り込み）。

本物の U7 の Manager を moto の上で動かし（tests/api/u7_world.py）、外部に出るところ
（AI・データ分析エージェント）だけを Mock にする。エラーは文言ではなくコードで確かめる
（期待値はカタログをコードで引いて作る）。例外の文言が画面に漏れないことも確かめる。
"""

import json
import time
from contextlib import ExitStack

import pytest

from src.models.errors import ErrorCode
from src.services.ai_service import AIServiceError
from tests.api.u7_world import ADMIN_USER, ME, ApiWorld
from tests.conftest import NO_ROLE
from web.error_messages import user_message_for_code

CHAT = "/survey/templates/ai-chat"
GENERATE = "/survey/templates/ai-generate"
#: The template form posts with fetch (no htmx), like ``_postAI`` does.
FETCH = {"HX-Request": "", "X-Requested-With": "XMLHttpRequest"}
#: Text only the exception carries; it must never reach the response.
SECRET = "arn:aws:bedrock:secret-internal-detail"


@pytest.fixture
def aw(client, survey_world):
    """ME signed in, with the real U7 Managers behind the routers."""
    ME.login(client)
    world = ApiWorld(survey_world)
    with ExitStack() as stack:
        world.patch_routers(stack)
        yield world


def _message(code: ErrorCode, **context) -> str:
    return user_message_for_code(code, context or None)


def _conversation(*turns: tuple[str, str]) -> dict:
    return {"messages": [{"role": r, "content": c} for r, c in turns]}


def _post(client, url: str, body: dict | list | str):
    if isinstance(body, str):
        return client.post(
            url,
            content=body,
            headers={**FETCH, "Content-Type": "application/json"},
        )
    return client.post(url, json=body, headers=FETCH)


# =============================================================================
# AI ヒアリングの 1 ターン（POST /templates/ai-chat、Issue #23、BR2.3）
# =============================================================================


class TestAiChat:
    def test_returns_the_assistant_turn_for_the_trimmed_history(self, aw, client):
        aw.ai.chat_for_survey.return_value = "対象者の年代を教えてください。"

        response = _post(
            client,
            CHAT,
            _conversation(
                ("user", "  新商品の調査をしたい  "),
                ("assistant", "どんな商品ですか？"),
                ("user", "飲料です"),
            ),
        )

        assert response.status_code == 200
        assert response.json() == {
            "assistant_message": "対象者の年代を教えてください。"
        }
        sent = aw.ai.chat_for_survey.call_args.args[0]
        assert sent == [
            {"role": "user", "content": "新商品の調査をしたい"},
            {"role": "assistant", "content": "どんな商品ですか？"},
            {"role": "user", "content": "飲料です"},
        ]
        assert aw.ai.chat_for_survey.call_args.kwargs["system_prompt"]

    def test_last_turn_must_be_the_user(self, aw, client):
        response = _post(
            client, CHAT, _conversation(("user", "調査"), ("assistant", "はい"))
        )

        assert response.status_code == 400
        assert response.json() == {
            "error": _message(ErrorCode.SURVEY_AI_CONVERSATION_INVALID)
        }
        aw.ai.chat_for_survey.assert_not_called()

    def test_ai_failure_is_500_with_the_catalog_wording_only(self, aw, client):
        aw.ai.chat_for_survey.side_effect = AIServiceError(
            f"survey chat failed {SECRET}", code=ErrorCode.AI_OPERATION_FAILED
        )

        response = _post(client, CHAT, _conversation(("user", "調査")))

        assert response.status_code == 500
        assert response.json() == {"error": _message(ErrorCode.AI_OPERATION_FAILED)}
        assert SECRET not in response.text

    def test_uncoded_failure_falls_back_to_the_chat_wording(self, aw, client):
        aw.ai.chat_for_survey.side_effect = RuntimeError(SECRET)

        response = _post(client, CHAT, _conversation(("user", "調査")))

        assert response.status_code == 500
        assert response.json() == {"error": "AIの応答生成に失敗しました"}
        assert SECRET not in response.text

    def test_without_ai_service_says_it_is_unavailable(self, aw, client):
        aw.templates.ai_service = None

        response = _post(client, CHAT, _conversation(("user", "調査")))

        assert response.status_code == 500
        assert response.json() == {"error": _message(ErrorCode.SURVEY_AI_UNAVAILABLE)}


# =============================================================================
# 設問ドラフトの生成（POST /templates/ai-generate、Issue #23、BR2.3）
# =============================================================================


class TestAiGenerate:
    def test_returns_the_normalized_draft(self, aw, client):
        aw.ai.generate_survey_questions_draft.return_value = {
            "summary": "飲料の購買調査",
            "template_name": "飲料アンケート",
            "questions": [
                {
                    "question_type": "multiple_choice",
                    "text": " 好きな味は？ ",
                    "options": [" 甘い ", "", "苦い", "酸っぱい"],
                    "allow_multiple": True,
                    # 選択肢の数を超える上限は 0（無制限）に丸める
                    "max_selections": 9,
                },
                {"question_type": "free_text", "text": "理由を教えてください"},
                {"question_type": "scale_rating", "text": "満足度は？"},
            ],
        }

        response = _post(client, GENERATE, _conversation(("user", "飲料の調査")))

        assert response.status_code == 200
        body = response.json()
        assert body["summary"] == "飲料の購買調査"
        assert body["template_name"] == "飲料アンケート"
        choice, free, scale = body["questions"]
        assert choice["question_type"] == "multiple_choice"
        assert choice["text"] == "好きな味は？"
        assert choice["options"] == ["甘い", "苦い", "酸っぱい"]
        assert choice["allow_multiple"] is True
        assert "max_selections" not in choice
        assert (free["question_type"], free["text"]) == (
            "free_text",
            "理由を教えてください",
        )
        assert (scale["question_type"], scale["text"]) == ("scale_rating", "満足度は？")
        assert len({q["id"] for q in body["questions"]}) == 3

    def test_no_questions_from_the_ai_is_a_retryable_500(self, aw, client):
        aw.ai.generate_survey_questions_draft.return_value = {
            "summary": "",
            "questions": [],
        }

        response = _post(client, GENERATE, _conversation(("user", "調査")))

        assert response.status_code == 500
        assert response.json() == {"error": _message(ErrorCode.SURVEY_AI_NO_QUESTIONS)}

    def test_question_of_an_unknown_type_is_refused(self, aw, client):
        # Manager は AI の出力の不備も SurveyTemplateValidationError で返すため 400 になる
        aw.ai.generate_survey_questions_draft.return_value = {
            "questions": [{"question_type": "ranking", "text": "順位"}]
        }

        response = _post(client, GENERATE, _conversation(("user", "調査")))

        assert response.status_code == 400
        assert response.json() == {"error": _message(ErrorCode.SURVEY_AI_NO_QUESTIONS)}

    def test_choice_question_with_one_option_is_refused(self, aw, client):
        aw.ai.generate_survey_questions_draft.return_value = {
            "questions": [
                {"question_type": "multiple_choice", "text": "色", "options": ["赤"]}
            ]
        }

        response = _post(client, GENERATE, _conversation(("user", "調査")))

        assert response.status_code == 400
        assert response.json() == {
            "error": _message(
                ErrorCode.SURVEY_TEMPLATE_TOO_FEW_OPTIONS, question_text="色"
            )
        }

    def test_ai_failure_is_500_with_the_catalog_wording_only(self, aw, client):
        aw.ai.generate_survey_questions_draft.side_effect = AIServiceError(
            f"question draft JSON parsing failed {SECRET}",
            code=ErrorCode.AI_OPERATION_FAILED,
        )

        response = _post(client, GENERATE, _conversation(("user", "調査")))

        assert response.status_code == 500
        assert response.json() == {"error": _message(ErrorCode.AI_OPERATION_FAILED)}
        assert SECRET not in response.text

    def test_uncoded_failure_suggests_continuing_the_conversation(self, aw, client):
        aw.ai.generate_survey_questions_draft.side_effect = RuntimeError(SECRET)

        response = _post(client, GENERATE, _conversation(("user", "調査")))

        assert response.status_code == 500
        error = response.json()["error"]
        assert error.startswith("設問ドラフトの生成に失敗しました。")
        assert SECRET not in response.text


# =============================================================================
# 2 つの AI の入口に共通の入力検査と権限
# =============================================================================


def _ai_mock(aw, url: str):
    return (
        aw.ai.chat_for_survey if url == CHAT else aw.ai.generate_survey_questions_draft
    )


@pytest.mark.parametrize("url", [CHAT, GENERATE])
class TestAiInputs:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("{not json", id="broken-json"),
            pytest.param([{"role": "user", "content": "x"}], id="not-an-object"),
            pytest.param({"messages": "x"}, id="messages-not-a-list"),
            pytest.param({"messages": ["x"]}, id="element-not-an-object"),
            pytest.param(
                {"messages": [{"role": "system", "content": "x"}]}, id="unknown-role"
            ),
            pytest.param(
                {"messages": [{"role": "user", "content": "   "}]}, id="blank-content"
            ),
        ],
    )
    def test_malformed_body_is_400_before_the_ai(self, aw, client, url, body):
        response = _post(client, url, body)

        assert response.status_code == 400
        _ai_mock(aw, url).assert_not_called()

    def test_empty_history_is_refused(self, aw, client, url):
        response = _post(client, url, {"messages": []})

        assert response.status_code == 400
        assert response.json() == {
            "error": _message(ErrorCode.SURVEY_AI_CONVERSATION_INVALID)
        }
        _ai_mock(aw, url).assert_not_called()

    def test_history_over_40_turns_names_the_limit(self, aw, client, url):
        turns = [("user", "a"), ("assistant", "b")] * 20 + [("user", "c")]

        response = _post(client, url, _conversation(*turns))

        assert response.status_code == 400
        assert response.json() == {
            "error": _message(
                ErrorCode.SURVEY_AI_CONVERSATION_TOO_LONG, max_messages=40
            )
        }
        _ai_mock(aw, url).assert_not_called()

    def test_message_over_2000_characters_names_the_limit(self, aw, client, url):
        response = _post(client, url, _conversation(("user", "あ" * 2001)))

        assert response.status_code == 400
        assert response.json() == {
            "error": _message(ErrorCode.SURVEY_AI_MESSAGE_TOO_LONG, max_length=2000)
        }
        _ai_mock(aw, url).assert_not_called()

    def test_user_without_a_role_never_reaches_the_ai(self, aw, client, url):
        # ロールのない利用者は AuthMiddleware が /no-role へ送る（AI_EXECUTE を持たない）
        NO_ROLE.login(client)

        response = client.post(
            url,
            json=_conversation(("user", "調査")),
            headers=FETCH,
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].endswith("/no-role")
        _ai_mock(aw, url).assert_not_called()


# =============================================================================
# インサイトレポートの生成（POST /results/{id}/report/generate、BR2.11）
# =============================================================================


def _sse_payloads(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


class TestReportStream:
    def _generate(self, client, survey_id: str):
        return client.post(
            f"/survey/results/{survey_id}/report/generate", headers=FETCH
        )

    def test_streams_the_chunks_then_saves_the_whole_report(self, aw, client):
        survey = aw.survey(name="自分の結果")
        aw.ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(
            ["# 要約\n", "満足度は高い"]
        )

        response = self._generate(client, survey.id)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert _sse_payloads(response.text) == [
            {"type": "chunk", "content": "# 要約\n"},
            {"type": "chunk", "content": "満足度は高い"},
            {"type": "done"},
        ]
        saved = aw.world.survey_store.get(survey.id, consistent=True)
        assert saved.insight_report.content == "# 要約\n満足度は高い"

    def test_ai_failure_mid_stream_sends_an_error_and_saves_nothing(self, aw, client):
        survey = aw.survey(name="自分の結果")

        def stream(**kw):
            yield "# 途中まで"
            raise AIServiceError(SECRET, code=ErrorCode.AI_OPERATION_FAILED)

        aw.ai.generate_standard_report_streaming.side_effect = stream

        response = self._generate(client, survey.id)

        events = _sse_payloads(response.text)
        assert events[0] == {"type": "chunk", "content": "# 途中まで"}
        assert events[-1] == {
            "type": "error",
            "message": _message(ErrorCode.AI_OPERATION_FAILED),
        }
        assert SECRET not in response.text
        assert (
            aw.world.survey_store.get(survey.id, consistent=True).insight_report is None
        )

    def test_report_too_large_to_save_ends_with_an_error(self, aw, client):
        survey = aw.survey(name="自分の結果")
        aw.ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(
            ["あ" * (400 * 1024)]
        )

        response = self._generate(client, survey.id)

        events = _sse_payloads(response.text)
        assert events[-1]["type"] == "error"
        assert {"type": "done"} not in events
        assert (
            aw.world.survey_store.get(survey.id, consistent=True).insight_report is None
        )


# =============================================================================
# ペルソナ元データの取り込み（CSV・DWH、admin だけ、BR4.1）
# =============================================================================

_CSV = "persona,age,好きな色\n東京の会社員,34,青\n大阪の学生,21,赤\n".encode()


def _assert_not_found_toast(response) -> None:
    assert response.status_code == 404
    assert response.content == b""
    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert toast["type"] == "error"
    assert "none.csv" not in toast["message"]


class TestCustomUpload:
    @pytest.fixture(autouse=True)
    def _admin(self, aw, client):
        ADMIN_USER.login(client)

    def test_csv_is_kept_and_its_columns_offered_for_mapping(self, aw, client):
        response = client.post(
            "/survey/persona-data/upload-custom",
            files={"file": ("顧客.csv", _CSV, "text/csv")},
        )

        assert response.status_code == 200
        html = response.text
        assert 'value="顧客"' in html  # 既定の名前はファイル名
        for column in ("persona", "age", "好きな色"):
            assert f'<option value="{column}"' in html
        assert "東京の会社員" in html
        listed = aw.world.s3.s3_client.list_objects_v2(
            Bucket="u7-test-bucket", Prefix="persona-dataset/temp/"
        )
        assert listed["KeyCount"] == 1
        key = listed["Contents"][0]["Key"]
        assert key.endswith("-顧客.csv")
        assert f'name="temp_key" value="{key}"' in html

    def test_non_csv_is_refused_without_storing(self, aw, client):
        response = client.post(
            "/survey/persona-data/upload-custom",
            files={"file": ("顧客.xlsx", b"PK\x03\x04", "application/octet-stream")},
        )

        assert response.status_code == 200
        assert (
            _message(ErrorCode.FILE_FORMAT_NOT_ALLOWED, allowed_formats="CSV")
            in response.text
        )
        listed = aw.world.s3.s3_client.list_objects_v2(
            Bucket="u7-test-bucket", Prefix="persona-dataset/"
        )
        assert listed.get("KeyCount", 0) == 0

    def test_unreadable_csv_shows_the_fallback_and_stores_nothing(self, aw, client):
        response = client.post(
            "/survey/persona-data/upload-custom",
            files={"file": ("壊れた.csv", b'a,b\n1,2,3,4\n"x', "text/csv")},
        )

        assert response.status_code == 200
        assert "CSVファイルの解析に失敗しました" in response.text
        listed = aw.world.s3.s3_client.list_objects_v2(
            Bucket="u7-test-bucket", Prefix="persona-dataset/"
        )
        assert listed.get("KeyCount", 0) == 0

    def test_prompt_preview_uses_the_mapping_and_extra_columns(self, aw, client):
        aw.world.s3.upload_file(_CSV, "persona-dataset/temp/a.csv")

        response = client.post(
            "/survey/persona-data/preview-prompt",
            data={
                "temp_key": "persona-dataset/temp/a.csv",
                "mapping_persona": "persona",
                "mapping_age": "age",
                "extra_column_3": "好きな色",
                "extra_label_3": "好きな色の名前",
            },
        )

        assert response.status_code == 200
        html = response.text
        assert "東京の会社員" in html
        assert "34" in html
        assert "好きな色の名前" in html and "青" in html
        assert "大阪の学生" not in html  # 1 行目だけ

    def test_prompt_preview_of_a_missing_upload_is_a_404_toast(self, aw, client):
        # 一時ファイルがない（S3_OBJECT_NOT_FOUND）は NOT_FOUND なので共通の例外ハンドラが答える
        response = client.post(
            "/survey/persona-data/preview-prompt",
            data={"temp_key": "persona-dataset/temp/none.csv"},
        )

        _assert_not_found_toast(response)


class TestDwh:
    @pytest.fixture(autouse=True)
    def _admin(self, aw, client):
        ADMIN_USER.login(client)

    def test_preview_merges_the_suggested_mapping(self, aw, client):
        aw.world.s3.upload_file(_CSV, "persona-dataset/temp/dwh-a.csv")
        aw.datasets.agent_service.suggest_column_mapping_with_llm.return_value = {
            "mapping": {"persona": "persona", "occupation": "存在しない列"},
            "extra_columns": [
                {"csv_column": "好きな色", "label": "色", "description": "好み"}
            ],
        }

        response = client.post(
            "/survey/persona-data/dwh-preview",
            data={"temp_key": "persona-dataset/temp/dwh-a.csv", "dataset_name": "抽出"},
        )

        assert response.status_code == 200
        html = response.text
        assert 'name="temp_key" value="persona-dataset/temp/dwh-a.csv"' in html
        assert 'value="抽出"' in html
        assert '<option value="persona" selected' in html
        # CSV にない列の提案は捨て、CSV にある列はその他の列として出す
        assert "存在しない列" not in html
        assert 'value="色"' in html

    def test_preview_of_a_missing_extract_is_a_404_toast(self, aw, client):
        response = client.post(
            "/survey/persona-data/dwh-preview",
            data={"temp_key": "persona-dataset/temp/none.csv", "dataset_name": "抽出"},
        )

        _assert_not_found_toast(response)
        aw.datasets.agent_service.suggest_column_mapping_with_llm.assert_not_called()

    def _extract(self, client, condition: str = "20代女性"):
        return client.post(
            "/survey/persona-data/dwh-extract", data={"condition": condition}
        )

    @staticmethod
    def _events(text: str) -> list[tuple[str, str]]:
        events = []
        for block in text.split("\n\n"):
            lines = block.splitlines()
            if not lines or not lines[0].startswith("event: "):
                continue
            data = "\n".join(
                line[len("data: ") :] for line in lines[1:] if line.startswith("data: ")
            )
            events.append((lines[0][len("event: ") :], data))
        return events

    @staticmethod
    def _until_read(event_queue) -> None:
        # ルーターが読み終える前に終わると、残りの進捗は捨てられる（flush）
        deadline = time.monotonic() + 5
        while not event_queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_extract_streams_progress_then_keeps_the_csv(self, aw, client, monkeypatch):
        def extract(principal, condition, event_queue):
            event_queue.put({"type": "status", "content": "データ抽出中..."})
            event_queue.put(
                {"type": "tool_call", "content": "ask_data_agent", "detail": "SQL"}
            )
            event_queue.put(
                {
                    "type": "tool_result",
                    "tool_name": "ask_data_agent",
                    "content": "x" * 600,
                }
            )
            self._until_read(event_queue)
            return {
                "csv_bytes": _CSV,
                "row_count": 2,
                "columns": ["persona", "age", "好きな色"],
                "samples": {"persona": ["東京の会社員"]},
                "auto_mapping": {"persona": "persona"},
                "suggested_name": "20代女性",
            }

        monkeypatch.setattr(aw.datasets, "extract_segment_from_dwh", extract)

        events = self._events(self._extract(client).text)

        kinds = [kind for kind, _ in events if kind != "keepalive"]
        assert kinds[0] == "status"
        assert kinds[-2:] == ["complete", "done"]
        thinking = [json.loads(d) for k, d in events if k == "thinking"]
        assert {"type": "tool_call", "content": "ask_data_agent", "detail": "SQL"} in (
            thinking
        )
        result = next(t for t in thinking if t["type"] == "tool_result")
        assert len(result["content"]) == 500
        complete = json.loads(next(d for k, d in events if k == "complete"))
        assert complete["dataset_name"] == "20代女性"
        assert complete["row_count"] == 2
        assert complete["temp_key"].startswith("persona-dataset/temp/dwh-")
        kept = aw.world.s3.download_file(f"s3://u7-test-bucket/{complete['temp_key']}")
        assert kept == _CSV

    def test_blank_condition_is_one_error_event(self, aw, client, monkeypatch):
        called = []
        monkeypatch.setattr(
            aw.datasets, "extract_segment_from_dwh", lambda *a: called.append(a)
        )

        events = self._events(self._extract(client, "   ").text)

        assert [kind for kind, _ in events] == ["error"]
        assert called == []

    def test_extraction_error_is_sent_as_the_catalog_wording(
        self, aw, client, monkeypatch
    ):
        from src.managers.survey_dataset_manager import SurveyDatasetValidationError

        def extract(principal, condition, event_queue):
            raise SurveyDatasetValidationError(
                SECRET,
                code=ErrorCode.SEGMENT_ROW_COUNT_TOO_LOW,
                context={"row_count": 3, "min_rows": 100},
            )

        monkeypatch.setattr(aw.datasets, "extract_segment_from_dwh", extract)

        events = self._events(self._extract(client).text)

        assert events[-1] == (
            "error",
            _message(ErrorCode.SEGMENT_ROW_COUNT_TOO_LOW, row_count=3, min_rows=100),
        )
        assert all(kind != "complete" for kind, _ in events)

    def test_unexpected_error_is_sent_without_its_text(self, aw, client, monkeypatch):
        def extract(principal, condition, event_queue):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(aw.datasets, "extract_segment_from_dwh", extract)

        response = self._extract(client)

        assert self._events(response.text)[-1] == (
            "error",
            "セグメント抽出中にエラーが発生しました。",
        )
        assert SECRET not in response.text
