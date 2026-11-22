"""Pure helpers of the document part of the advanced generation (U4).

Only ``AdvancedPersonaGenerationManager`` uses these, so they are a module of
that Manager's package. Nothing here calls a Service or the model: the
functions shape values only.

- The progress wording of a prepared document and of the mode (BR24.5,
  BR25.2), with the character count in ten thousands rounded up (BR22.3).
- The full text joined from its chunks without the overlap (BR25.3).
- Document evidence with IDs, sources and locations set by the Manager, never
  by the model (NFR4.4): ``ev-d<document>-<chunk>-<n>`` (chunk ``0`` for the
  full text).
- The assignment of evidence to known segments (BR28.1) and the trimming of
  evidence no persona cites before the result is written (BR27.3, NFR5.38).
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ...models.analysis import DocumentReport, Evidence, EvidenceKind
from .prompts import (
    DocumentEvidenceOutput,
    EvidenceAssignmentItem,
    EvidenceExtraction,
    SpeakerAttributeOutput,
)

#: Pages without text named one by one in the progress (BR24.3).
_PAGES_LISTED = 5
_ATTRIBUTE_MAX_CHARS = 50
_ATTRIBUTES_MAX = 10


def man_chars(chars: int) -> int:
    """Characters in ten thousands, rounded up (``約 N 万字``, BR22.3)."""
    return math.ceil(max(int(chars), 0) / 10_000)


def prepared_message(position: int, report: DocumentReport) -> str:
    """``n 件目の文書: N ページ・約 M 万字・分割 K 個`` (BR24.5) and left-out pages."""
    parts = []
    if report.pages is not None:
        parts.append(f"{report.pages} ページ")
    parts.append(f"約 {man_chars(report.chars)} 万字")
    parts.append(f"分割 {report.chunks} 個")
    message = f"{position} 件目の文書: " + "・".join(parts)
    pages = report.no_text_pages
    if pages:
        listed = "、".join(str(n) for n in pages[:_PAGES_LISTED])
        if len(pages) > _PAGES_LISTED:
            listed += f" など {len(pages)}"
        message += f"（{listed} ページは文字がないため除外しました）"
    return message


def mode_message(total_chars: int, chunks: int, full_text: bool) -> str:
    """The mode decided right after the preparation (BR25.2)."""
    how = "全文を使います" if full_text else "根拠を取り出します"
    return f"文書の合計 約 {man_chars(total_chars)} 万字・分割 {chunks} 個（{how}）"


def join_chunks(texts: Sequence[str], overlap: int) -> str:
    """The text of one document from its chunks (each but the first overlaps)."""
    return "".join(t if i == 0 else t[overlap:] for i, t in enumerate(texts))


def speaker_dict(
    attributes: Iterable[SpeakerAttributeOutput],
) -> Optional[dict[str, str]]:
    """Speaker attributes as a short map (None when the document says none)."""
    found: dict[str, str] = {}
    for attribute in attributes:
        name = attribute.name.strip()[:_ATTRIBUTE_MAX_CHARS]
        value = attribute.value.strip()[:_ATTRIBUTE_MAX_CHARS]
        if name and value and len(found) < _ATTRIBUTES_MAX:
            found[name] = value
    return found or None


def chunk_location(info: Mapping[str, Any], page: Optional[int]) -> str:
    """``p.12 / 第3章 > 不満``: the page (or the chunk's pages) and the headings."""
    first, last = info.get("page_from"), info.get("page_to")
    parts = []
    if isinstance(first, int) and isinstance(last, int):
        if isinstance(page, int) and first <= page <= last:
            parts.append(f"p.{page}")
        elif first == last:
            parts.append(f"p.{first}")
        else:
            parts.append(f"p.{first}-{last}")
    headings = [str(h) for h in info.get("heading_path") or [] if str(h).strip()]
    if headings:
        parts.append(" > ".join(headings))
    return " / ".join(parts) or "本文"


def evidence_from_extraction(
    output: Optional[EvidenceExtraction],
    *,
    source_index: int,
    source_id: str,
    chunk_index: int,
    info: Mapping[str, Any],
    max_items: int,
    max_quote_chars: int,
) -> list[Evidence]:
    """The evidence of one chunk, cut to the limits (BR26.1, NFR4.4)."""
    if output is None:
        return []
    evidence: list[Evidence] = []
    for item in output.items:
        quote = item.quote.strip()[:max_quote_chars]
        if not quote.strip() or len(evidence) >= max_items:
            continue
        evidence.append(
            Evidence.for_document(
                id=f"ev-d{source_index}-{chunk_index}-{len(evidence)}",
                source_id=source_id,
                location=chunk_location(info, item.page),
                kind=EvidenceKind(item.kind),
                claim=item.claim.strip(),
                quote=quote,
                speaker_attributes=speaker_dict(item.speaker_attributes),
            )
        )
    return evidence


def evidence_from_synthesis(
    items: Sequence[DocumentEvidenceOutput],
    *,
    documents: Sequence[tuple[int, str]],
    segment_ids: set[str],
    max_quote_chars: int,
) -> tuple[list[Evidence], dict[str, str]]:
    """Evidence the full-text synthesis quoted, with the Manager's IDs (BR25.3).

    Args:
        items: The synthesis output's ``document_evidence``.
        documents: ``(source_index, source_id)`` in the order the texts were
            given (``document_index`` is a position in this list).
        segment_ids: Segments of the table; any other ``segment_id`` is dropped.
        max_quote_chars: Longest quote kept.

    Returns:
        The evidence, and the model's IDs mapped to the new ones.
    """
    evidence: list[Evidence] = []
    renamed: dict[str, str] = {}
    for item in items:
        if not 0 <= item.document_index < len(documents):
            continue
        quote = item.quote.strip()[:max_quote_chars]
        if not quote:
            continue
        source_index, source_id = documents[item.document_index]
        new_id = f"ev-d{source_index}-0-{len(evidence)}"
        segment = item.segment_id if item.segment_id in segment_ids else None
        evidence.append(
            Evidence.for_document(
                id=new_id,
                source_id=source_id,
                location="本文",
                kind=EvidenceKind(item.kind),
                claim=item.claim.strip(),
                quote=quote,
                speaker_attributes=speaker_dict(item.speaker_attributes),
                segment_id=segment,
                assignment_reason=(item.assignment_reason or None) if segment else None,
            )
        )
        renamed.setdefault(item.id, new_id)
    return evidence, renamed


def apply_assignments(
    evidence: Sequence[Evidence],
    assignments: Sequence[EvidenceAssignmentItem],
    *,
    segment_ids: set[str],
) -> list[Evidence]:
    """Put the chosen segment on each evidence; unknown IDs are dropped (BR28.1)."""
    chosen = {
        a.evidence_id: a
        for a in assignments
        if a.segment_id and a.segment_id in segment_ids
    }
    out = []
    for item in evidence:
        assignment = chosen.get(item.id)
        if assignment is None:
            out.append(item.update(segment_id=None, assignment_reason=None))
        else:
            out.append(
                item.update(
                    segment_id=assignment.segment_id,
                    assignment_reason=assignment.reason.strip() or None,
                )
            )
    return out


def trim_evidence(
    evidence: Sequence[Evidence],
    *,
    cited: set[str],
    budget: int,
    size_of: Callable[[Evidence], int],
) -> list[Evidence]:
    """Drop document evidence no persona cites, from the end, until it fits.

    Table evidence and cited evidence are always kept; when that alone is over
    ``budget``, the store's own check refuses the result (BR27.3).
    ``size_of`` gives the stored size of one evidence.
    """
    kept = list(evidence)
    total = sum(size_of(e) for e in kept)
    for index in range(len(kept) - 1, -1, -1):
        if total <= budget:
            break
        item = kept[index]
        if item.is_document and item.id not in cited:
            total -= size_of(item)
            del kept[index]
    return kept
