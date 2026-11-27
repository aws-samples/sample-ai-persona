"""The errors of the advanced generation: its coded failure and the run's internal stops."""

from __future__ import annotations

from typing import Any

from ...models.errors import CodedError, ErrorCode


class AdvancedGenerationError(CodedError):
    """A refused or failed advanced generation request."""


class StopRun(Exception):
    """Internal: the run cannot go on (a transition was lost); no failure write."""


class Cancelled(Exception):
    """Internal: the user cancelled; the run moves to ``cancelled``."""


def fail(code: ErrorCode, message: str, **context: Any) -> AdvancedGenerationError:
    """An :class:`AdvancedGenerationError` with ``code`` and its safe ``context``."""
    return AdvancedGenerationError(message, code=code, context=context)
