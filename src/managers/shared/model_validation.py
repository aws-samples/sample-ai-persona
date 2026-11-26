"""Shared utilities for model selection validation.

Used by AgentDiscussionManager and InterviewManager: check that the selected
model_id is available, fill personas without a selection with the
environment default model, and check that the total document size stays
within the selected model's limit.
"""

from typing import Callable, Dict, Iterable, Mapping, Optional, Type

from ...config import config
from ...models.errors import CodedError, ErrorCode
from ...models.model_registry import ModelSpec, models_in_scope, resolve_model_spec


def _is_accepted(model_id: str) -> bool:
    """A model of the current scope's list, or the environment's default model."""
    if model_id == config.AGENT_MODEL_ID:
        return True
    return any(
        spec.model_id == model_id for spec in models_in_scope(config.INFERENCE_SCOPE)
    )


def _called_spec(model_id: str) -> ModelSpec:
    """The spec of the model actually called (after reading it for the scope)."""
    return resolve_model_spec(model_id, config.INFERENCE_SCOPE, config.AGENT_MODEL_ID)


def validate_model_selection(
    model_ids: Optional[Mapping[str, Optional[str]]],
    error_cls: Type[CodedError],
    unsupported_code: ErrorCode,
    disabled_code: ErrorCode,
    unsupported_message: Callable[[str, str], str] = (
        lambda identifier,
        model_id: f"unsupported model_id {model_id!r} for {identifier!r}"
    ),
    disabled_message: Callable[[str], str] = (
        lambda model_id: f"model {model_id!r} requires Mantle but "
        "ENABLE_ADDITIONAL_PERSONA_MODELS is disabled"
    ),
) -> None:
    """Check that the selected model_ids are available.

    An unsupported model_id raises ``unsupported_code``; an additional-persona
    base model while ENABLE_ADDITIONAL_PERSONA_MODELS is off raises
    ``disabled_code``.

    Args:
        model_ids: {identifier: model_id} map to check (None means the default model; skipped).
        error_cls: Exception class to raise (the caller's domain exception).
        unsupported_code: ErrorCode for an unsupported model_id.
        disabled_code: ErrorCode when additional-persona base models are disabled.
        unsupported_message: (identifier, model_id) -> log message.
        disabled_message: model_id -> log message.
    """
    if not model_ids:
        return
    for identifier, model_id in model_ids.items():
        if model_id is None:
            continue
        if not _is_accepted(model_id):
            raise error_cls(
                unsupported_message(identifier, model_id),
                code=unsupported_code,
            )
        spec = _called_spec(model_id)
        if spec.requires_mantle and not config.ENABLE_ADDITIONAL_PERSONA_MODELS:
            raise error_cls(
                disabled_message(model_id),
                code=disabled_code,
            )


def resolve_effective_persona_models(
    persona_ids: Iterable[str],
    persona_models: Optional[Mapping[str, Optional[str]]],
) -> Dict[str, str]:
    """Fill personas without a selection with config.AGENT_MODEL_ID (the environment default).

    The environment default may be set to a Mantle-family model such as Gemma4,
    so attachment type/size checks must cover the model actually called, not only
    explicitly chosen ones (skipping the check when persona_models is None would
    let a Mantle-family default bypass the size and type limits).

    Args:
        persona_ids: Target persona IDs (discussion passes persona_agent; interview
            converts session.participants to a persona_id list).
        persona_models: persona_id -> model_id map (default model if omitted).

    Returns:
        A copy of persona_models with the missing persona_ids filled in. Other
        keys in persona_models are kept so explicit choices are never dropped.
    """
    resolved: Dict[str, str] = dict(persona_models or {})  # type: ignore[arg-type]
    for persona_id in persona_ids:
        if not resolved.get(persona_id):
            resolved[persona_id] = config.AGENT_MODEL_ID
    return resolved


def validate_document_size_for_models(
    total_size: int,
    persona_models: Optional[Mapping[str, Optional[str]]],
    error_cls: Type[CodedError],
    too_large_code: ErrorCode,
    message: Callable[[str, int, int], str] = (
        lambda model_id, total, effective_max: (
            f"document total size {total} exceeds model {model_id!r} "
            f"limit (effective max {effective_max})"
        )
    ),
) -> None:
    """Check the total document size against the selected model's max_request_bytes (Gemma4 etc.).

    The limit is the effective one after allowing for base64 overhead (about 4/3).

    Args:
        total_size: Total document size in bytes (how it is summed is up to the
            caller: discussion counts new attachments only, interview adds the
            session's existing ones).
        persona_models: {persona_id: model_id} map to check.
        error_cls: Exception class to raise (the caller's domain exception).
        too_large_code: ErrorCode when the size is exceeded.
        message: (model_id, total_size, effective_max) -> log message.
    """
    if not persona_models or total_size == 0:
        return

    for model_id in set(persona_models.values()):
        if model_id is None:
            continue
        spec = _called_spec(model_id)
        if spec.max_request_bytes is None:
            continue
        effective_max = int(spec.max_request_bytes * 3 / 4)
        if total_size > effective_max:
            raise error_cls(
                message(model_id, total_size, effective_max),
                code=too_large_code,
                context={"max_size_mb": spec.max_request_bytes / (1024 * 1024)},
            )
