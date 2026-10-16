"""DiscussionValidation Component の単体テスト。

discussion / agent_discussion で共有する議論バリデーションの単一ソース。閾値と
fail-closed 判定、両モードの差（content ゲート・mode 要件）をパラメータで検証する。
エラーコードで検証する（文言はアサートしない）。
"""

import pytest

from src.managers.components import discussion_validation as dv
from src.models.discussion import Discussion
from src.models.errors import CodedError, ErrorCode
from src.models.message import Message
from src.models.persona import Persona
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit


class _Err(CodedError):
    """注入用のテスト例外（Manager 例外の代役）。"""


def _persona(name="p"):
    return Persona.create_new(
        name=name,
        age=30,
        occupation="x",
        background="y",
        values=["a"],
        pain_points=["b"],
        goals=["c"],
    )


def _discussion(topic="有効なトピック", participants=None, mode="agent"):
    # U6: 発言は本体の外（DiscussionMessages）に置くため、本体だけを作る
    return Discussion.create_new(
        topic=topic, participants=participants or ["p1", "p2"], mode=mode
    )


def _msg(
    persona_id="p1",
    content=(
        "これは合計文字数ゲート(100文字)を1メッセージ単体で余裕を持って超えるために、"
        "意図的にかなり長めに用意したテスト用のペルソナ発言内容のサンプル文字列です。"
    ),
    mtype="statement",
):
    return Message.create_new(
        persona_id=persona_id,
        persona_name="name",
        content=content,
        message_type=mtype,
    )


class TestValidatePersonasAndTopic:
    def test_valid_passes(self):
        dv.validate_personas_and_topic(
            [_persona(), _persona()], "有効なトピック", error_cls=_Err
        )

    def test_empty_personas(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_PERSONAS_REQUIRED):
            dv.validate_personas_and_topic([], "有効なトピック", error_cls=_Err)

    def test_too_few(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_TOO_FEW_PERSONAS, min_personas=2):
            dv.validate_personas_and_topic([_persona()], "topic!", error_cls=_Err)

    def test_too_many_over_5(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_TOO_MANY_PERSONAS, max_personas=5):
            dv.validate_personas_and_topic(
                [_persona() for _ in range(6)], "topic!", error_cls=_Err
            )

    def test_individual_missing_id_or_name(self):
        bad = _persona()
        bad = bad.__class__(**{**bad.to_dict(), "name": ""})
        with raises_code(_Err, ErrorCode.DISCUSSION_PERSONA_INVALID):
            dv.validate_personas_and_topic(
                [_persona(), bad], "有効なトピック", error_cls=_Err
            )

    def test_duplicate_ids(self):
        p = _persona()
        with raises_code(_Err, ErrorCode.DISCUSSION_PERSONA_DUPLICATED):
            dv.validate_personas_and_topic([p, p], "有効なトピック", error_cls=_Err)

    def test_topic_blank(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_TOPIC_REQUIRED):
            dv.validate_personas_and_topic(
                [_persona(), _persona()], "   ", error_cls=_Err
            )

    def test_topic_too_short(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_TOPIC_TOO_SHORT, min_length=5):
            dv.validate_personas_and_topic(
                [_persona(), _persona()], "ab", error_cls=_Err
            )

    def test_topic_too_long(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_TOPIC_TOO_LONG, max_length=200):
            dv.validate_personas_and_topic(
                [_persona(), _persona()], "x" * 201, error_cls=_Err
            )


class TestValidateResults:
    def test_valid_passes(self):
        dv.validate_results(
            [_msg("p1"), _msg("p2")],
            [],
            error_cls=_Err,
            require_min_content=True,
            count_statements_only=False,
        )

    def test_too_few_messages(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_RESULT_INVALID):
            dv.validate_results(
                [_msg("p1")],
                [],
                error_cls=_Err,
                require_min_content=False,
                count_statements_only=False,
            )

    def test_min_content_enforced_when_required(self):
        # 各発言は非空だが合計 < 100 文字。require_min_content=True で拒否。
        with raises_code(_Err, ErrorCode.DISCUSSION_RESULT_INVALID):
            dv.validate_results(
                [_msg("p1", "短い"), _msg("p2", "短い")],
                [],
                error_cls=_Err,
                require_min_content=True,
                count_statements_only=False,
            )

    def test_min_content_skipped_when_not_required(self):
        # 同じ短い議論でも require_min_content=False なら通過（エージェント挙動）。
        dv.validate_results(
            [_msg("p1", "短い"), _msg("p2", "短い")],
            [],
            error_cls=_Err,
            require_min_content=False,
            count_statements_only=True,
        )


class TestValidateForSave:
    def test_valid_passes(self):
        dv.validate_for_save(
            _discussion(),
            error_cls=_Err,
            code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            require_mode="agent",
            messages=[_msg("p1"), _msg("p2")],
        )

    def test_too_few_participants(self):
        d = _discussion(participants=["only-one"])
        with raises_code(_Err, ErrorCode.DISCUSSION_INVALID):
            dv.validate_for_save(d, error_cls=_Err, code=ErrorCode.DISCUSSION_INVALID)

    def test_mode_mismatch_rejected(self):
        d = _discussion(mode="classic")
        with raises_code(_Err, ErrorCode.DISCUSSION_OPERATION_FAILED):
            dv.validate_for_save(
                d,
                error_cls=_Err,
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
                require_mode="agent",
            )

    def test_mode_not_checked_when_require_mode_none(self):
        d = _discussion(mode="classic")
        dv.validate_for_save(d, error_cls=_Err, code=ErrorCode.DISCUSSION_INVALID)

    def test_empty_message_content_rejected(self):
        with raises_code(_Err, ErrorCode.DISCUSSION_INVALID):
            dv.validate_for_save(
                _discussion(),
                error_cls=_Err,
                code=ErrorCode.DISCUSSION_INVALID,
                messages=[_msg("p1"), _msg("p2", content="")],
            )
