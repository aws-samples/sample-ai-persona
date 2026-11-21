"""Allowed log fields of the advanced generation (beta) events (NFR6.1〜NFR6.5).

Every structured line of the advanced generation goes through
:func:`log_advanced_event`, which keeps only the fields listed for its event
in :data:`ALLOWED_FIELDS` (observability-design.md §1). Anything else (row
values, URLs, exception text) is dropped here, in one place, before
:func:`~.structured_log.log_event` writes the line. Both
``SourceUploadManager`` and ``AdvancedPersonaGenerationManager`` log through
it, so the list lives in ``shared/``.
"""

from __future__ import annotations

from typing import Any

from .structured_log import log_event

#: Event name -> the only fields it may carry.
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "advanced_generation.stage": frozenset({"generation_id", "stage", "elapsed_ms"}),
    "advanced_generation.finished": frozenset(
        {
            "generation_id",
            "status",
            "stage",
            "error_code",
            "cause",
            "method",
            "rows_read",
            "rows_excluded",
            "persona_count",
            "durations_ms",
            "total_ms",
            "tool_calls",
        }
    ),
    "advanced_generation.upload_completed": frozenset(
        {
            "source_file_id",
            "size_bytes",
            "status",
            "error_code",
            "encoding",
            "estimated_rows",
        }
    ),
}


def log_advanced_event(event: str, **fields: Any) -> None:
    """Write one advanced generation event with its allowed fields only.

    Raises:
        ValueError: ``event`` is not in :data:`ALLOWED_FIELDS`.
    """
    allowed = ALLOWED_FIELDS.get(event)
    if allowed is None:
        raise ValueError(f"unknown advanced generation event: {event}")
    log_event(event, **{k: v for k, v in fields.items() if k in allowed})
