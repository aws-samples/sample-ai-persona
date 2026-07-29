"""
Error codes and the coded-exception base class for the AI Persona System.

Design principle: an exception message belongs to the developer, and the
user-facing wording belongs to the presentation layer.

- Exception message: a technical fact for logs. Never rendered to a response.
- ``ErrorCode``: a machine-readable error kind, resolved to user-facing wording
  by ``web/error_messages.py``.
- ``CodedError.context``: structured values used both for diagnostic logging and
  for interpolating the wording template.

See ``docs/note/exception-message-design.md`` for the full rationale.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error kinds shared across all layers.

    Granularity follows what the user can do about the error: an error that
    calls for a distinct user action gets its own code, while internal
    operation failures collapse into a per-feature catch-all code.

    Every member except ``UNKNOWN`` must have an entry in the wording catalog
    (``web/error_messages.py``); ``tests/unit/test_error_messages.py`` enforces
    this.
    """

    # Fallback for exceptions that carry no code yet.
    UNKNOWN = "unknown"

    # --- Generation capacity ---
    GENERATION_CAPACITY_EXCEEDED = "generation_capacity_exceeded"
    REPORT_CAPACITY_EXCEEDED = "report_capacity_exceeded"


class CodedError(Exception):
    """Base class for exceptions carrying an :class:`ErrorCode`.

    Args:
        message: A technical fact for logs. May be in English. This is never
            rendered into a response.
        code: Overrides the class-level default. Use this when a single
            exception type covers several distinct user-facing situations,
            instead of declaring one subclass per wording.
        context: Structured values for logging and wording interpolation. Only
            put values here that are safe to show a user (size limits, allowed
            formats, counts) -- never IDs, paths, or third-party exception text.
    """

    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.context: dict[str, object] = dict(context or {})
