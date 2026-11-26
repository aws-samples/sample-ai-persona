"""Discussion validation shared by discussion (non-agent) and agent_discussion (agent).

Both Managers used to carry near-duplicate copies of
`_validate_discussion_input` / `_validate_discussion_results` /
`_validate_discussion_for_save`, spreading the thresholds (persona limit,
topic length, counts) over two places. They are collected here as a single
source for decisions that affect security and correctness. The content is
business rules (thresholds, fail-closed), so it cannot live in shared/.

Each Manager injects its own exception class (`error_cls`) and ErrorCodes.
Parts that differ between the modes (minimum-length gate on generated results,
statement-only counting, mode requirement on save) are taken as explicit flags
(a Component must not import Managers or Routers).
"""

import logging
from typing import List, Optional, Type

from ...models.discussion import Discussion
from ...models.errors import CodedError, ErrorCode
from ...models.message import Message
from ...models.persona import Persona

logger = logging.getLogger(__name__)

# Business thresholds (single source for both modes).
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
    """Validate the input (persona set, topic) before a discussion starts.

    Common to both modes: empty / minimum 2 / **maximum 5** / each persona
    (falsy, missing id/name) / **duplicate ids** / topic (empty, under 5, over
    200). The maximum, per-persona and duplicate checks used to exist only on
    the non-agent side; they now apply to both modes.
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
    messages: List[Message],
    original_personas: List[Persona],
    *,
    error_cls: Type[CodedError],
    require_min_content: bool,
    count_statements_only: bool,
) -> None:
    """Validate the discussion result (message sequence) after AI generation.

    Common to both modes: at least 2 messages / warning for personas that did
    not speak.

    ``require_min_content``: reject a total length under 100 characters (True
    for non-agent only).
    ``count_statements_only``: count per persona only message_type=="statement"
    (True for agent only; affects the warning's accuracy).
    """
    if not messages or len(messages) < MIN_MESSAGES:
        message_count = len(messages) if messages else 0
        raise error_cls(
            f"generated discussion has {message_count} messages, "
            f"minimum is {MIN_MESSAGES}",
            code=ErrorCode.DISCUSSION_RESULT_INVALID,
        )

    persona_message_count: dict[str, int] = {}
    for message in messages:
        if count_statements_only and message.message_type != "statement":
            continue
        persona_message_count[message.persona_id] = (
            persona_message_count.get(message.persona_id, 0) + 1
        )

    for persona in original_personas:
        if persona_message_count.get(persona.id, 0) == 0:
            logger.warning(f"ペルソナ {persona.name} の発言が見つかりませんでした")

    if require_min_content:
        total_content_length = sum(len(msg.content) for msg in messages)
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
    messages: Optional[List[Message]] = None,
) -> None:
    """Validate the structure of a discussion object before saving.

    Common: falsy / id / topic / at least 2 participants / created_at /
    non-empty persona_id and content for each message. ``code`` differs per
    Manager (injected). With ``require_mode``, ``discussion.mode`` must match
    (agent uses "agent").
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

    if messages:
        for i, message in enumerate(messages):
            if not message.persona_id or not message.content:
                raise error_cls(
                    f"message at index {i} has no persona_id or content",
                    code=code,
                )
