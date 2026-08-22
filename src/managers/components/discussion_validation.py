"""discussion（非エージェント）/ agent_discussion（エージェント）共通の議論バリデーション。

以前は両 Manager に `_validate_discussion_input` / `_validate_discussion_results` /
`_validate_discussion_for_save` が near-duplicate でコピーされ、閾値（ペルソナ数上限・
topic 長・件数）が2箇所に分散していた。security/正当性に関わる判定の単一ソース化のため
Component に集約する。中身はビジネスルール（閾値・fail-closed）なので shared/ には置けない。

各 Manager は自ドメインの例外クラス（`error_cls`）と ErrorCode を注入する。両モードで
挙動が異なる部分（生成結果の最小文字数ゲート・statement 限定カウント・保存時の mode 要件）
は明示フラグで受ける（Component は Manager/Router を import しない）。
"""

import logging
from typing import List, Optional, Type

from ...models.discussion import Discussion
from ...models.errors import CodedError, ErrorCode
from ...models.persona import Persona

logger = logging.getLogger(__name__)

# ビジネス閾値（両モード共通の単一ソース）。
MIN_PERSONAS = 2
MAX_PERSONAS = 5
MIN_TOPIC_LENGTH = 5
MAX_TOPIC_LENGTH = 200
MIN_MESSAGES = 2
MIN_TOTAL_CONTENT_LENGTH = 100


def validate_personas_and_topic(
    personas: List[Persona],
    topic: str,
    *,
    error_cls: Type[CodedError],
) -> None:
    """議論開始前の入力（ペルソナ集合・topic）を検証する。

    両モード共通: 空 / 最小2 / **最大5** / 個別ペルソナ（falsy・id/name 欠落）/
    **重複 id** / topic（空・5未満・200超）。以前はこれらのうち上限・個別・重複が
    非エージェント側にしか無かったが、統一して両モードに適用する。
    """
    if not personas:
        raise error_cls(
            "persona list is empty",
            code=ErrorCode.DISCUSSION_PERSONAS_REQUIRED,
        )
    if len(personas) < MIN_PERSONAS:
        raise error_cls(
            f"{len(personas)} personas given, minimum is {MIN_PERSONAS}",
            code=ErrorCode.DISCUSSION_TOO_FEW_PERSONAS,
            context={"min_personas": MIN_PERSONAS},
        )
    if len(personas) > MAX_PERSONAS:
        raise error_cls(
            f"{len(personas)} personas given, maximum is {MAX_PERSONAS}",
            code=ErrorCode.DISCUSSION_TOO_MANY_PERSONAS,
            context={"max_personas": MAX_PERSONAS},
        )

    for i, persona in enumerate(personas):
        if not persona:
            raise error_cls(
                f"persona at index {i} is falsy",
                code=ErrorCode.DISCUSSION_PERSONA_INVALID,
            )
        if not persona.id or not persona.name:
            raise error_cls(
                f"persona at index {i} has no id or name",
                code=ErrorCode.DISCUSSION_PERSONA_INVALID,
            )

    persona_ids = [persona.id for persona in personas]
    if len(set(persona_ids)) != len(persona_ids):
        raise error_cls(
            "persona list contains duplicate ids",
            code=ErrorCode.DISCUSSION_PERSONA_DUPLICATED,
        )

    if not topic or not topic.strip():
        raise error_cls("topic is blank", code=ErrorCode.DISCUSSION_TOPIC_REQUIRED)
    if len(topic.strip()) < MIN_TOPIC_LENGTH:
        raise error_cls(
            f"topic length {len(topic.strip())} below minimum {MIN_TOPIC_LENGTH}",
            code=ErrorCode.DISCUSSION_TOPIC_TOO_SHORT,
            context={"min_length": MIN_TOPIC_LENGTH},
        )
    if len(topic.strip()) > MAX_TOPIC_LENGTH:
        raise error_cls(
            f"topic length {len(topic.strip())} exceeds {MAX_TOPIC_LENGTH}",
            code=ErrorCode.DISCUSSION_TOPIC_TOO_LONG,
            context={"max_length": MAX_TOPIC_LENGTH},
        )


def validate_results(
    discussion: Discussion,
    original_personas: List[Persona],
    *,
    error_cls: Type[CodedError],
    require_min_content: bool,
    count_statements_only: bool,
) -> None:
    """AI 生成後の議論結果を検証する。

    両モード共通: 結果 falsy / メッセージ最小2 / ペルソナ別発言有無の warning。

    ``require_min_content``: 全メッセージの合計文字数 < 100 を拒否（非エージェントのみ True）。
    ``count_statements_only``: ペルソナ別カウントを message_type=="statement" に限定
    （エージェントのみ True。warning の精度に影響）。
    """
    if not discussion:
        raise error_cls(
            "generated discussion is falsy",
            code=ErrorCode.DISCUSSION_RESULT_INVALID,
        )
    if not discussion.messages or len(discussion.messages) < MIN_MESSAGES:
        message_count = len(discussion.messages) if discussion.messages else 0
        raise error_cls(
            f"generated discussion has {message_count} messages, "
            f"minimum is {MIN_MESSAGES}",
            code=ErrorCode.DISCUSSION_RESULT_INVALID,
        )

    persona_message_count: dict[str, int] = {}
    for message in discussion.messages:
        if count_statements_only and message.message_type != "statement":
            continue
        persona_message_count[message.persona_id] = (
            persona_message_count.get(message.persona_id, 0) + 1
        )

    for persona in original_personas:
        if persona_message_count.get(persona.id, 0) == 0:
            logger.warning(f"ペルソナ {persona.name} の発言が見つかりませんでした")

    if require_min_content:
        total_content_length = sum(len(msg.content) for msg in discussion.messages)
        if total_content_length < MIN_TOTAL_CONTENT_LENGTH:
            raise error_cls(
                f"generated discussion content length {total_content_length} "
                f"below minimum {MIN_TOTAL_CONTENT_LENGTH}",
                code=ErrorCode.DISCUSSION_RESULT_INVALID,
            )


def validate_for_save(
    discussion: Discussion,
    *,
    error_cls: Type[CodedError],
    code: ErrorCode,
    require_mode: Optional[str] = None,
) -> None:
    """保存前の議論オブジェクトの構造を検証する。

    共通: falsy / id / topic / participants 最小2 / created_at / 各メッセージの
    persona_id・content 非空。``code`` は Manager ごとに異なる（注入）。
    ``require_mode`` 指定時は ``discussion.mode`` が一致することを要求（エージェントは "agent"）。
    """
    if not discussion:
        raise error_cls("discussion is falsy", code=code)
    if not discussion.id:
        raise error_cls("discussion has no id", code=code)
    if not discussion.topic or not discussion.topic.strip():
        raise error_cls("discussion has no topic", code=code)
    if not discussion.participants or len(discussion.participants) < MIN_PERSONAS:
        raise error_cls(
            f"discussion has {len(discussion.participants or [])} participants, "
            f"minimum is {MIN_PERSONAS}",
            code=code,
        )
    if not discussion.created_at:
        raise error_cls("discussion has no created_at", code=code)
    if require_mode is not None and discussion.mode != require_mode:
        raise error_cls(
            f"invalid discussion mode: {discussion.mode!r}, expected {require_mode!r}",
            code=code,
        )

    if discussion.messages:
        for i, message in enumerate(discussion.messages):
            if not message.persona_id or not message.content:
                raise error_cls(
                    f"message at index {i} has no persona_id or content",
                    code=code,
                )
