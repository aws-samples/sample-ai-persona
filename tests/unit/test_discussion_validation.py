"""Unit tests for the DiscussionValidation component.

Single source of discussion validation shared by discussion and agent_discussion. Thresholds,
fail-closed decisions and the differences between the two modes (content gate, mode
requirement) are checked with parameters. Assertions use error codes, not messages.
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
    """Test exception for injection (stands in for a Manager exception)."""


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
    # Messages live outside the body (DiscussionMessages), so only the body is built
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
        # Each message is non-empty but the total is < 100 chars; rejected with require_min_content=True.
        with raises_code(_Err, ErrorCode.DISCUSSION_RESULT_INVALID):
            dv.validate_results(
                [_msg("p1", "短い"), _msg("p2", "短い")],
                [],
                error_cls=_Err,
                require_min_content=True,
                count_statements_only=False,
            )

    def test_min_content_skipped_when_not_required(self):
        # The same short discussion passes with require_min_content=False (agent behavior).
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
