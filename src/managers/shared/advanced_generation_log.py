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
    # U3 (observability-design.md): counts, codes and time of the preparation.
    # No line number, column name or value.
    "advanced_generation.prepared": frozenset(
        {
            "generation_id",
            "status",
            "error_code",
            "rows_read",
            "rows_excluded",
            "excluded_reasons",
            "duration_ms",
            "memory_limit",
        }
    ),
    # U4 (observability-design.md NFR6.13, NFR6.14, NFR7.3): counts, time and
    # tokens only. No file name, heading, body or exception text.
    "advanced_generation.document_prepared": frozenset(
        {
            "generation_id",
            "status",
            "error_code",
            "document_index",
            "format",
            "pages",
            "chars",
            "chunks",
            "no_text_pages",
            "elapsed_ms",
        }
    ),
    "advanced_generation.document_mode": frozenset(
        {"generation_id", "mode", "total_chars", "chunks"}
    ),
    "advanced_generation.evidence_extracted": frozenset(
        {
            "generation_id",
            "chunks",
            "extracted",
            "discarded_unverified",
            "elapsed_ms",
            "evidence_input_tokens",
            "evidence_output_tokens",
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
            # U4: the kind and the estimate of a document (no file name)
            "kind",
            "estimated_chars",
        }
    ),
    # U2 (observability-design.md §1): IDs, states, codes, counts, sizes and
    # milliseconds only. No owner, file name, column name or row value.
    "advanced_generation.source_deleted": frozenset(
        {"source_file_id", "status", "versions", "error_code"}
    ),
    "advanced_generation.source_refreshed": frozenset(
        {
            "generation_id",
            "source_file_id",
            "status",
            "size_bytes",
            "duration_ms",
            "error_code",
        }
    ),
    "advanced_generation.sources_reconciled": frozenset(
        {"filled", "deleted", "remaining", "status", "error_code"}
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
