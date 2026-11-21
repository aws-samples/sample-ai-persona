"""Result contract of advanced generation (beta) analysis (contract C8, entities.md).

Every method returns the same shapes so that ``AnalysisVerifier`` checks only
these: the preparation report, the plan, the segments a persona stands for,
their evidence and a summary of the run. ``AnalysisToolPolicy`` is the value
the Manager decides and hands to the Service (which tools, which limits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ._advanced_common import drop_none, iso, parse_dt
from .source_file import SourceEncoding

#: Version of the analysis record body (contract C7).
RECORD_SCHEMA_VERSION = 1

#: Seed used when a run does not say otherwise (BR5.3).
DEFAULT_SEED = 42

#: Keys of ``AnalysisToolPolicy.limits`` (NFR5.8〜NFR5.10).
LIMIT_MAX_CALLS = "max_calls"
LIMIT_MAX_TOTAL_SECONDS = "max_total_seconds"
LIMIT_MAX_OUTPUT_CHARS = "max_output_chars"

# ``<data``, ``</data``, ``< / DATA`` and the like inside the data.
_DATA_MARKER = re.compile(r"<(\s*/?\s*data)", re.IGNORECASE)
_ZERO_WIDTH_SPACE = chr(0x200B)
_SAFE_ATTR = re.compile(r"[^A-Za-z0-9_.-]")


def wrap_untrusted_data(text: str, source: str, **attributes: str) -> str:
    """Put file values or tool output between data markers (BR5.6, NFR4.1).

    The prompts tell the agent that what is inside ``<data ...>…</data>`` is
    data, not instructions. A marker that appears inside the data is broken
    up so the data cannot close the block or open a new one. Attribute values
    keep only ``[A-Za-z0-9_.-]``.
    """
    safe_text = _DATA_MARKER.sub("<" + _ZERO_WIDTH_SPACE + r"\1", text)
    attrs = {"source": source, **attributes}
    rendered = " ".join(f'{k}="{_SAFE_ATTR.sub("", str(v))}"' for k, v in attrs.items())
    return f"<data {rendered}>\n{safe_text}\n</data>"


class AnalysisMethod(StrEnum):
    """Methods U1 can run (time series, text and others come in later units)."""

    FILTER = "filter"
    COMPARE = "compare"
    CLUSTER = "cluster"


class SegmentDefinitionKind(StrEnum):
    """How a segment can be reproduced on the same data (FR10.5)."""

    FILTER = "filter"
    CLUSTER = "cluster"
    DERIVED = "derived"


class EvidenceKind(StrEnum):
    """What a piece of evidence supports in a persona."""

    ATTRIBUTE = "attribute"
    BEHAVIOR = "behavior"
    VALUE = "value"
    PAIN = "pain"
    GOAL = "goal"
    UTTERANCE = "utterance"


class DocumentMode(StrEnum):
    """How a generation uses its documents (U4 BR25.2)."""

    FULL_TEXT = "full_text"
    EXTRACTION = "extraction"


#: Version of the fixed document preparation (recorded in the analysis run).
DOCUMENT_PREPARE_VERSION = "doc-1"


class AnalysisTool(StrEnum):
    """Fixed tools an analysis agent may use (BR5.2; ``run_code`` is U6)."""

    PROFILE_DATASET = "profile_dataset"
    ANALYZE_DATASET = "analyze_dataset"
    CLUSTER_SEGMENTS = "cluster_segments"


#: The tools of U1, in the order they are offered (BR5.2).
U1_ANALYSIS_TOOLS: tuple[AnalysisTool, ...] = (
    AnalysisTool.PROFILE_DATASET,
    AnalysisTool.ANALYZE_DATASET,
    AnalysisTool.CLUSTER_SEGMENTS,
)

#: Encodings a finished preparation may report (``undetermined`` is settled).
_PREPARED_ENCODINGS = frozenset(
    {
        SourceEncoding.UTF8,
        SourceEncoding.UTF8_SIG,
        SourceEncoding.CP932,
        SourceEncoding.EUC_JP,
    }
)


@dataclass(frozen=True)
class ReportColumn:
    """A column of the prepared Parquet."""

    name: str
    type: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReportColumn":
        return cls(name=str(data["name"]), type=str(data["type"]))


@dataclass(frozen=True)
class PrepareReport:
    """Result of the preparation of one table in the sandbox (BR4.2〜BR4.4).

    Attributes:
        source_id: The source file.
        rows_read: Rows kept (excluded rows not counted); the population.
        rows_excluded: Rows that could not be read.
        columns: Columns of the prepared data.
        encoding: The settled encoding.
        prepare_version: Version of the fixed preparation code.
        encoding_retried: Read as UTF-8 first, then again as CP932 (FD-Q5).
        excluded_reasons: Counts per reason and up to five example lines.
    """

    source_id: str
    rows_read: int
    rows_excluded: int
    columns: tuple[ReportColumn, ...]
    encoding: SourceEncoding
    prepare_version: str
    encoding_retried: bool = False
    excluded_reasons: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        encoding = SourceEncoding(self.encoding)
        if encoding not in _PREPARED_ENCODINGS:
            raise ValueError("a prepared table has a settled encoding")
        object.__setattr__(self, "encoding", encoding)
        if self.rows_read < 0 or self.rows_excluded < 0:
            raise ValueError("row counts must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "source_id": self.source_id,
                "rows_read": int(self.rows_read),
                "rows_excluded": int(self.rows_excluded),
                "columns": [c.to_dict() for c in self.columns],
                "encoding": self.encoding.value,
                "encoding_retried": bool(self.encoding_retried),
                "prepare_version": self.prepare_version,
                "excluded_reasons": self.excluded_reasons,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PrepareReport":
        reasons = data.get("excluded_reasons")
        return cls(
            source_id=str(data["source_id"]),
            rows_read=int(data["rows_read"]),
            rows_excluded=int(data.get("rows_excluded", 0)),
            columns=tuple(ReportColumn.from_dict(c) for c in data.get("columns", [])),
            encoding=SourceEncoding(str(data["encoding"])),
            prepare_version=str(data["prepare_version"]),
            encoding_retried=bool(data.get("encoding_retried", False)),
            excluded_reasons=dict(reasons) if reasons is not None else None,
        )


@dataclass(frozen=True)
class DocumentReport:
    """Result of the preparation of one document in the sandbox (U4 BR24, C8).

    Attributes:
        source_id: The source file.
        chars: Settled characters of the body (pages without text left out).
        chunks: How many chunks the body was split into.
        headings: Headings found (the preferred split points).
        no_text_pages: 1-based numbers of PDF pages without text.
        pages: Pages of a PDF; none for docx, txt and md.
    """

    source_id: str
    chars: int
    chunks: int
    headings: int
    no_text_pages: tuple[int, ...] = ()
    pages: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "no_text_pages", tuple(int(n) for n in self.no_text_pages)
        )
        if min(self.chars, self.chunks, self.headings) < 0:
            raise ValueError("counts must not be negative")
        if self.pages is None:
            if self.no_text_pages:
                raise ValueError("pages without text need a page count")
            return
        if self.pages < 1:
            raise ValueError("a document with pages has at least one")
        if len(self.no_text_pages) >= self.pages:
            raise ValueError("a document with no text is refused (BR24.4)")
        if any(not 1 <= n <= self.pages for n in self.no_text_pages):
            raise ValueError("page numbers are within the document")

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "source_id": self.source_id,
                "chars": int(self.chars),
                "chunks": int(self.chunks),
                "headings": int(self.headings),
                "no_text_pages": list(self.no_text_pages),
                "pages": int(self.pages) if self.pages is not None else None,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentReport":
        pages = data.get("pages")
        return cls(
            source_id=str(data["source_id"]),
            chars=int(data["chars"]),
            chunks=int(data.get("chunks", 0)),
            headings=int(data.get("headings", 0)),
            no_text_pages=tuple(int(n) for n in data.get("no_text_pages") or ()),
            pages=int(pages) if pages is not None else None,
        )


@dataclass(frozen=True)
class AnalysisPlan:
    """The plan an agent makes before the analysis (FR9.1, BR5.1).

    Attributes:
        interpreted_purpose: How the agent read the user's instruction.
        questions: What the analysis answers (at least one).
        method: One of :class:`AnalysisMethod`.
        rationale: Why the method was chosen.
        budget: Upper bounds the plan works within (tool calls, time).
        split_axis: Column to split a filtered group by (FD-Q6).
        deviation_reason: Required when the method departs from the guide.
    """

    interpreted_purpose: str
    questions: tuple[str, ...]
    method: AnalysisMethod
    rationale: str
    budget: dict[str, Any]
    split_axis: str | None = None
    deviation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", AnalysisMethod(self.method))
        object.__setattr__(self, "questions", tuple(self.questions))
        if not self.questions:
            raise ValueError("a plan has at least one question")

    def update(self, **changes: Any) -> "AnalysisPlan":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "interpreted_purpose": self.interpreted_purpose,
                "questions": list(self.questions),
                "method": self.method.value,
                "rationale": self.rationale,
                "budget": dict(self.budget),
                "split_axis": self.split_axis,
                "deviation_reason": self.deviation_reason,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisPlan":
        return cls(
            interpreted_purpose=str(data.get("interpreted_purpose", "")),
            questions=tuple(str(q) for q in data.get("questions", [])),
            method=AnalysisMethod(str(data["method"])),
            rationale=str(data.get("rationale", "")),
            budget=dict(data.get("budget", {})),
            split_axis=data.get("split_axis"),
            deviation_reason=data.get("deviation_reason"),
        )


@dataclass(frozen=True)
class Segment:
    """A group of rows a persona stands for (contract C8).

    Attributes:
        id: Segment ID.
        label: Display name.
        definition: ``{kind: filter | cluster | derived, spec}``.
        size: Rows in the group, a value seen in a tool output (BR8.1).
        share: ``size`` divided by the population (0〜1).
        features: ``[{name, value, baseline}]``.
        evidence_ids: Evidence the segment rests on (at least one).
        overlaps: Whether it may overlap other segments; only non-overlapping
            segments are summed against the population (BR8.2).
    """

    id: str
    label: str
    definition: dict[str, Any]
    size: int
    share: float
    features: tuple[dict[str, Any], ...]
    evidence_ids: tuple[str, ...]
    overlaps: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        SegmentDefinitionKind(str(self.definition.get("kind")))
        if self.size < 1:
            raise ValueError("a segment has at least one row")
        if not 0.0 <= self.share <= 1.0:
            raise ValueError("share must be within 0..1")
        if not self.evidence_ids:
            raise ValueError("a segment rests on at least one evidence")

    def update(self, **changes: Any) -> "Segment":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "definition": dict(self.definition),
            "size": int(self.size),
            "share": float(self.share),
            "features": [dict(f) for f in self.features],
            "evidence_ids": list(self.evidence_ids),
            "overlaps": bool(self.overlaps),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Segment":
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", "")),
            definition=dict(data["definition"]),
            size=int(data["size"]),
            share=float(data["share"]),
            features=tuple(dict(f) for f in data.get("features", [])),
            evidence_ids=tuple(str(e) for e in data.get("evidence_ids", [])),
            overlaps=bool(data.get("overlaps", False)),
        )


@dataclass(frozen=True)
class Evidence:
    """A piece of evidence (contract C8): a table aggregate or a document quote.

    Attributes:
        id: Evidence ID (the Manager renumbers document evidence, U4 NFR4.4).
        source_id: The source file.
        location: For a table, the ``tool_call_id`` of the aggregate; for a
            document, the page and the heading path (``p.12 / 第3章``).
        kind: One of :class:`EvidenceKind`.
        claim: What it shows.
        statistic: The aggregate's values (value, population) for display.
        quote: The quoted text of a document (required there, BR27.1).
        speaker_attributes: What the document says of the speaker (BR28.1).
        segment_id: The table segment the quote was assigned to (BR28.1);
            none means it stands for the whole (BR28.2).
        assignment_reason: Why it was assigned to that segment.
    """

    id: str
    source_id: str
    location: str
    kind: EvidenceKind
    claim: str
    statistic: dict[str, Any] | None = None
    quote: str | None = None
    speaker_attributes: dict[str, str] | None = None
    segment_id: str | None = None
    assignment_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))

    @classmethod
    def for_document(
        cls,
        *,
        id: str,
        source_id: str,
        location: str,
        kind: EvidenceKind | str,
        claim: str,
        quote: str,
        speaker_attributes: Mapping[str, str] | None = None,
        segment_id: str | None = None,
        assignment_reason: str | None = None,
    ) -> "Evidence":
        """Evidence quoted from a document; the quote is required (BR27.1).

        Raises:
            ValueError: The quote is empty.
        """
        if not quote or not quote.strip():
            raise ValueError("document evidence needs a quote")
        return cls(
            id=id,
            source_id=source_id,
            location=location,
            kind=EvidenceKind(kind),
            claim=claim,
            quote=quote,
            speaker_attributes=(
                {str(k): str(v) for k, v in speaker_attributes.items()}
                if speaker_attributes
                else None
            ),
            segment_id=segment_id,
            assignment_reason=assignment_reason,
        )

    @property
    def is_document(self) -> bool:
        """Quoted from a document rather than aggregated from a table."""
        return self.quote is not None

    def update(self, **changes: Any) -> "Evidence":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "id": self.id,
                "source_id": self.source_id,
                "location": self.location,
                "kind": self.kind.value,
                "claim": self.claim,
                "statistic": dict(self.statistic) if self.statistic else None,
                "quote": self.quote,
                "speaker_attributes": (
                    dict(self.speaker_attributes) if self.speaker_attributes else None
                ),
                "segment_id": self.segment_id,
                "assignment_reason": self.assignment_reason,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        statistic = data.get("statistic")
        speaker = data.get("speaker_attributes")
        return cls(
            id=str(data["id"]),
            source_id=str(data["source_id"]),
            location=str(data["location"]),
            kind=EvidenceKind(str(data["kind"])),
            claim=str(data.get("claim", "")),
            statistic=dict(statistic) if statistic else None,
            quote=data.get("quote"),
            speaker_attributes=(
                {str(k): str(v) for k, v in dict(speaker).items()} if speaker else None
            ),
            segment_id=data.get("segment_id"),
            assignment_reason=data.get("assignment_reason"),
        )


@dataclass(frozen=True)
class AnalysisRun:
    """Summary of what ran, for the analysis record (contract C8).

    Attributes:
        method: The method that ran.
        plan_ref: Reference to the plan.
        code_sha256: Hashes of the fixed code that ran.
        params: Parameters of the run.
        source_checksums: Checksums of the source files.
        prepare_version: Version of the preparation code.
        tool_call_ids: Tool calls of the analysis.
        seed: Random seed (42 unless stated).
        artifacts_s3_keys: Keys of artifacts (U6; empty in U1).
    """

    method: str
    plan_ref: str
    code_sha256: tuple[str, ...]
    params: dict[str, Any]
    source_checksums: tuple[str, ...]
    prepare_version: str
    tool_call_ids: tuple[str, ...]
    seed: int = DEFAULT_SEED
    artifacts_s3_keys: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        for name in (
            "code_sha256",
            "source_checksums",
            "tool_call_ids",
            "artifacts_s3_keys",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def update(self, **changes: Any) -> "AnalysisRun":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "method": self.method,
            "plan_ref": self.plan_ref,
            "code_sha256": list(self.code_sha256),
            "seed": int(self.seed),
            "params": dict(self.params),
            "source_checksums": list(self.source_checksums),
            "prepare_version": self.prepare_version,
            "tool_call_ids": list(self.tool_call_ids),
        }
        if self.artifacts_s3_keys:
            data["artifacts_s3_keys"] = list(self.artifacts_s3_keys)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisRun":
        return cls(
            method=str(data["method"]),
            plan_ref=str(data.get("plan_ref", "")),
            code_sha256=tuple(str(h) for h in data.get("code_sha256", [])),
            params=dict(data.get("params", {})),
            source_checksums=tuple(str(c) for c in data.get("source_checksums", [])),
            prepare_version=str(data.get("prepare_version", "")),
            tool_call_ids=tuple(str(t) for t in data.get("tool_call_ids", [])),
            seed=int(data.get("seed", DEFAULT_SEED)),
            artifacts_s3_keys=tuple(str(k) for k in data.get("artifacts_s3_keys", [])),
        )


@dataclass(frozen=True)
class AnalysisToolPolicy:
    """Tools and limits the Manager grants an analysis (contract C8, ADR-012).

    Attributes:
        tools: Allowed tools (a subset of :data:`U1_ANALYSIS_TOOLS` in U1).
        limits: Call count, total time and output characters (NFR5.8〜NFR5.10).
    """

    tools: tuple[AnalysisTool, ...]
    limits: dict[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tools", tuple(AnalysisTool(str(t)) for t in self.tools)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": [t.value for t in self.tools],
            "limits": {k: int(v) for k, v in self.limits.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisToolPolicy":
        return cls(
            tools=tuple(AnalysisTool(str(t)) for t in data.get("tools", [])),
            limits={str(k): int(v) for k, v in dict(data.get("limits", {})).items()},
        )


@dataclass(frozen=True)
class AnalysisRecord:
    """The full analysis record kept in S3 (entities.md AnalysisRecord, contract C7).

    It never holds a presigned URL or row data (BR9.1).

    Attributes:
        record_id: The generation ID while pending; a saved persona's copy
            lives at its persona ID (the body keeps this value).
        owner_sub: The user who ran the generation.
        generation_id: Kept as a value after the generation item expires.
        plan: The analysis plan (``AnalysisPlan.to_dict()``).
        code_blocks: ``[{kind: fixed | ai_written, code, sha256}]``.
        seed: Random seed of the run.
        prepare_version: Version of the preparation code.
        source_checksums: Checksums of the source files.
        created_at: When the record was written.
        tool_calls: ``[{tool_call_id, tool, ok, error_code}]`` (no output).
        source_file_ids: The files used, in ``source_checksums`` order (U2,
            BR14.3). Empty in U1 records.
        source_display_names: Their display names, kept so the record can
            name a deleted file (U2, BR14.3). Empty in U1 records.
    """

    record_id: str
    owner_sub: str
    generation_id: str
    plan: dict[str, Any]
    code_blocks: tuple[dict[str, Any], ...]
    seed: int
    prepare_version: str
    source_checksums: tuple[str, ...]
    created_at: datetime
    tool_calls: tuple[dict[str, Any], ...] = field(default=())
    source_file_ids: tuple[str, ...] = field(default=())
    source_display_names: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        for name in (
            "code_blocks",
            "source_checksums",
            "tool_calls",
            "source_file_ids",
            "source_display_names",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @classmethod
    def create_new(
        cls,
        *,
        record_id: str,
        owner_sub: str,
        generation_id: str,
        plan: Mapping[str, Any],
        code_blocks: Sequence[Mapping[str, Any]],
        seed: int,
        prepare_version: str,
        source_checksums: Sequence[str],
        now: datetime,
        tool_calls: Sequence[Mapping[str, Any]] = (),
        source_file_ids: Sequence[str] = (),
        source_display_names: Sequence[str] = (),
    ) -> "AnalysisRecord":
        """A new record written at the end of a generation (BR9.1, BR14.3)."""
        return cls(
            record_id=record_id,
            owner_sub=owner_sub,
            generation_id=generation_id,
            plan=dict(plan),
            code_blocks=tuple(dict(b) for b in code_blocks),
            seed=int(seed),
            prepare_version=prepare_version,
            source_checksums=tuple(source_checksums),
            created_at=now,
            tool_calls=tuple(dict(c) for c in tool_calls),
            source_file_ids=tuple(source_file_ids),
            source_display_names=tuple(source_display_names),
        )

    def update(self, **changes: Any) -> "AnalysisRecord":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "record_id": self.record_id,
            "owner_sub": self.owner_sub,
            "generation_id": self.generation_id,
            "plan": dict(self.plan),
            "code_blocks": [dict(b) for b in self.code_blocks],
            "seed": int(self.seed),
            "prepare_version": self.prepare_version,
            "source_checksums": list(self.source_checksums),
            "created_at": iso(self.created_at),
        }
        if self.tool_calls:
            data["tool_calls"] = [dict(c) for c in self.tool_calls]
        if self.source_file_ids:
            data["source_file_ids"] = list(self.source_file_ids)
        if self.source_display_names:
            data["source_display_names"] = list(self.source_display_names)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisRecord":
        return cls(
            record_id=str(data["record_id"]),
            owner_sub=str(data["owner_sub"]),
            generation_id=str(data["generation_id"]),
            plan=dict(data.get("plan", {})),
            code_blocks=tuple(dict(b) for b in data.get("code_blocks", [])),
            seed=int(data.get("seed", DEFAULT_SEED)),
            prepare_version=str(data.get("prepare_version", "")),
            source_checksums=tuple(str(c) for c in data.get("source_checksums", [])),
            created_at=parse_dt(data["created_at"]),
            tool_calls=tuple(dict(c) for c in data.get("tool_calls", [])),
            source_file_ids=tuple(str(i) for i in data.get("source_file_ids", [])),
            source_display_names=tuple(
                str(n) for n in data.get("source_display_names", [])
            ),
        )
