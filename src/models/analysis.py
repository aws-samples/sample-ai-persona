"""Result contract of advanced generation (beta) analysis.

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
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ._advanced_common import drop_none, iso, parse_dt
from .source_file import SourceEncoding

#: Version of the analysis record body.
RECORD_SCHEMA_VERSION = 1

#: Seed used when a run does not say otherwise.
DEFAULT_SEED = 42

#: Keys of ``AnalysisToolPolicy.limits``.
LIMIT_MAX_CALLS = "max_calls"
LIMIT_MAX_TOTAL_SECONDS = "max_total_seconds"
LIMIT_MAX_OUTPUT_CHARS = "max_output_chars"
#: Keys of the AI-written code limits.
LIMIT_RUN_CODE_MAX_FAILURES = "run_code_max_failures"
LIMIT_RUN_CODE_TIMEOUT_SECONDS = "run_code_timeout_seconds"
LIMIT_RUN_CODE_MAX_CHARS = "run_code_max_chars"

#: Names of the tables of one generation, in the order chosen.
TABLE_ALIASES: tuple[str, ...] = ("source_1", "source_2", "source_3")
#: ``source_n``, ``join:<axis>+<table>[+<table>]`` or ``people:source_n`` (the
#: distinct people of a history table). Kept in step with the sandbox library.
_POPULATION_SOURCE = re.compile(
    r"^(source_[1-3])$|^join:source_[1-3](\+source_[1-3])+$|^people:(source_[1-3])$"
)
_PEOPLE_PREFIX = "people:"

# ``<data``, ``</data``, ``< / DATA`` and the like inside the data.
_DATA_MARKER = re.compile(r"<(\s*/?\s*data)", re.IGNORECASE)
_ZERO_WIDTH_SPACE = chr(0x200B)
_SAFE_ATTR = re.compile(r"[^A-Za-z0-9_.-]")


def wrap_untrusted_data(text: str, source: str, **attributes: str) -> str:
    """Put file values or tool output between data markers.

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
    """Methods that can run (time series, text and others are not supported yet)."""

    FILTER = "filter"
    COMPARE = "compare"
    CLUSTER = "cluster"


class AnalysisStepKind(StrEnum):
    """Kinds of one step of a plan (the plan is a sequence of steps).

    The first three run with the fixed tools; the last three need the AI
    detailed analysis (``run_code`` and the playbooks).
    """

    FILTER = "filter"
    COMPARE = "compare"
    CLUSTER = "cluster"
    JOIN = "join"
    RFM = "rfm"
    COMPARE_GROUPS = "compare_groups"
    #: A history table made one row per person (what they did and when).
    PERSON_PROFILE = "person_profile"


#: Step kinds that need the AI detailed analysis.
DETAILED_STEP_KINDS: frozenset[AnalysisStepKind] = frozenset(
    {
        AnalysisStepKind.JOIN,
        AnalysisStepKind.RFM,
        AnalysisStepKind.COMPARE_GROUPS,
        AnalysisStepKind.PERSON_PROFILE,
    }
)
#: Steps one plan may hold.
MAX_PLAN_STEPS = 4
#: The playbooks ``load_playbook`` reads; the only names recorded.
PLAYBOOK_NAMES: tuple[str, ...] = ("join", "compare_groups", "rfm", "person_profile")


def step_method(kind: AnalysisStepKind | str) -> Optional["AnalysisMethod"]:
    """The method a step stands for when it makes the segments.

    ``rfm`` makes its segments as a filter (matched by ``segment_key``)
    and ``compare_groups`` as a comparison; ``join`` and
    ``person_profile`` make none.
    """
    kind = AnalysisStepKind(kind)
    if kind in (AnalysisStepKind.JOIN, AnalysisStepKind.PERSON_PROFILE):
        return None
    if kind is AnalysisStepKind.RFM:
        return AnalysisMethod.FILTER
    if kind is AnalysisStepKind.COMPARE_GROUPS:
        return AnalysisMethod.COMPARE
    return AnalysisMethod(kind.value)


@dataclass(frozen=True)
class AnalysisStep:
    """One step of a plan.

    Attributes:
        kind: One of :class:`AnalysisStepKind`.
        reason: Why the step is taken (the planning agent's words).
        table: The table it works on (``source_1``..), when named.
        column: The profiled column it works on, when named.
    """

    kind: AnalysisStepKind
    reason: str = ""
    table: str | None = None
    column: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", AnalysisStepKind(self.kind))

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "kind": self.kind.value,
                "reason": self.reason,
                "table": self.table,
                "column": self.column,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisStep":
        table = data.get("table")
        column = data.get("column")
        return cls(
            kind=AnalysisStepKind(str(data["kind"])),
            reason=str(data.get("reason") or ""),
            table=str(table) if table is not None else None,
            column=str(column) if column is not None else None,
        )


class SegmentDefinitionKind(StrEnum):
    """How a segment can be reproduced on the same data."""

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
    """How a generation uses its documents."""

    FULL_TEXT = "full_text"
    EXTRACTION = "extraction"


#: Version of the fixed document preparation (recorded in the analysis run).
DOCUMENT_PREPARE_VERSION = "doc-1"


class AnalysisTool(StrEnum):
    """Tools an analysis agent may use (the last two need the AI detailed analysis)."""

    PROFILE_DATASET = "profile_dataset"
    ANALYZE_DATASET = "analyze_dataset"
    CLUSTER_SEGMENTS = "cluster_segments"
    #: Count the features of the layers a fixed tool made
    PROFILE_SEGMENTS = "profile_segments"
    #: Run AI-written code in the generation's session
    RUN_CODE = "run_code"
    #: Read one playbook by name
    LOAD_PLAYBOOK = "load_playbook"


#: The fixed tools, in the order they are offered (``profile_segments`` is
#: the last).
U1_ANALYSIS_TOOLS: tuple[AnalysisTool, ...] = (
    AnalysisTool.PROFILE_DATASET,
    AnalysisTool.ANALYZE_DATASET,
    AnalysisTool.CLUSTER_SEGMENTS,
    AnalysisTool.PROFILE_SEGMENTS,
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
    """Result of the preparation of one table in the sandbox.

    Attributes:
        source_id: The source file.
        rows_read: Rows kept (excluded rows not counted); the population.
        rows_excluded: Rows that could not be read.
        columns: Columns of the prepared data.
        encoding: The settled encoding.
        prepare_version: Version of the fixed preparation code.
        encoding_retried: Read as UTF-8 first, then again as CP932.
        excluded_reasons: Counts per reason and up to five example lines.
        parquet_sha256: SHA-256 of the prepared Parquet, counted right
            after it was written and before any AI-written code ran.
            Kept by the run only, never stored on the generation.
    """

    source_id: str
    rows_read: int
    rows_excluded: int
    columns: tuple[ReportColumn, ...]
    encoding: SourceEncoding
    prepare_version: str
    encoding_retried: bool = False
    excluded_reasons: dict[str, Any] | None = None
    parquet_sha256: str | None = None

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
                "parquet_sha256": self.parquet_sha256,
            }
        )

    def without_hash(self) -> "PrepareReport":
        """The report as it is stored (the hash stays with the run)."""
        return replace(self, parquet_sha256=None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PrepareReport":
        reasons = data.get("excluded_reasons")
        digest = data.get("parquet_sha256")
        return cls(
            source_id=str(data["source_id"]),
            rows_read=int(data["rows_read"]),
            rows_excluded=int(data.get("rows_excluded", 0)),
            columns=tuple(ReportColumn.from_dict(c) for c in data.get("columns", [])),
            encoding=SourceEncoding(str(data["encoding"])),
            prepare_version=str(data["prepare_version"]),
            encoding_retried=bool(data.get("encoding_retried", False)),
            excluded_reasons=dict(reasons) if reasons is not None else None,
            parquet_sha256=str(digest) if digest else None,
        )


@dataclass(frozen=True)
class DocumentReport:
    """Result of the preparation of one document in the sandbox.

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
    """The plan an agent makes before the analysis.

    Attributes:
        interpreted_purpose: How the agent read the user's instruction.
        questions: What the analysis answers (at least one).
        method: One of :class:`AnalysisMethod`.
        rationale: Why the method was chosen.
        budget: Upper bounds the plan works within (tool calls, time).
        split_axis: Column to split a filtered group by.
        deviation_reason: Required when the method departs from the guide.
        steps: The steps in order (up to four). ``method`` stays
            the method of the step that makes the segments. ``None`` (older
            records) reads as one step of ``method``.
    """

    interpreted_purpose: str
    questions: tuple[str, ...]
    method: AnalysisMethod
    rationale: str
    budget: dict[str, Any]
    split_axis: str | None = None
    deviation_reason: str | None = None
    steps: tuple[AnalysisStep, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", AnalysisMethod(self.method))
        object.__setattr__(self, "questions", tuple(self.questions))
        if not self.questions:
            raise ValueError("a plan has at least one question")
        if self.steps is not None:
            steps = tuple(self.steps)
            if len(steps) > MAX_PLAN_STEPS:
                raise ValueError("a plan has four steps at most")
            object.__setattr__(self, "steps", steps or None)

    def plan_steps(self) -> tuple[AnalysisStep, ...]:
        """The steps to run; one step of ``method`` when none were planned."""
        if self.steps:
            return self.steps
        return (AnalysisStep(AnalysisStepKind(self.method.value), self.rationale),)

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
                "steps": [s.to_dict() for s in self.steps] if self.steps else None,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisPlan":
        steps = data.get("steps")
        return cls(
            steps=tuple(AnalysisStep.from_dict(s) for s in steps) if steps else None,
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
    """A group of rows a persona stands for.

    Attributes:
        id: Segment ID.
        label: Display name.
        definition: ``{kind: filter | cluster | derived, spec}``.
        size: Rows in the group, a value seen in a tool output.
        share: ``size`` divided by the population (0 to 1).
        features: ``[{name, value, baseline}]``.
        evidence_ids: Evidence the segment rests on (at least one).
        overlaps: Whether it may overlap other segments; only non-overlapping
            segments are summed against the population.
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

    @property
    def source(self) -> str | None:
        """The table (``source_n``) or joined table it rests on."""
        value = self.definition.get("source")
        return str(value) if value else None

    def _spec_value(self, key: str) -> str | None:
        spec = self.definition.get("spec")
        value = spec.get(key) if isinstance(spec, Mapping) else None
        return str(value) if value not in (None, "") else None

    @property
    def tool_call_id(self) -> str | None:
        """The tool call the segment was made from (``spec.tool_call_id``)."""
        return self._spec_value("tool_call_id")

    @property
    def segment_key(self) -> str | None:
        """The key of an ``emit_result`` segment."""
        return self._spec_value("segment_key")

    @property
    def profile_tool_call_id(self) -> str | None:
        """The ``profile_segments`` call that counted the features."""
        return self._spec_value("profile_tool_call_id")

    @property
    def profile_key(self) -> str | None:
        """The key of the segment's layer in that call."""
        return self._spec_value("profile_key")

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
    """A piece of evidence: a table aggregate or a document quote.

    Attributes:
        id: Evidence ID (the Manager renumbers document evidence).
        source_id: The source file.
        location: For a table, the ``tool_call_id`` of the aggregate; for a
            document, the page and the heading path (``p.12 / 第3章``).
        kind: One of :class:`EvidenceKind`.
        claim: What it shows.
        statistic: The aggregate's values (value, population) for display.
        quote: The quoted text of a document (required there).
        speaker_attributes: What the document says of the speaker.
        segment_id: The table segment the quote was assigned to;
            none means it stands for the whole.
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
        """Evidence quoted from a document; the quote is required.

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
    """Summary of what ran, for the analysis record.

    Attributes:
        method: The method that ran.
        plan_ref: Reference to the plan.
        code_sha256: Hashes of the fixed code that ran.
        params: Parameters of the run.
        source_checksums: Checksums of the source files.
        prepare_version: Version of the preparation code.
        tool_call_ids: Tool calls of the analysis.
        seed: Random seed (42 unless stated).
        artifacts_s3_keys: Keys of artifacts (empty for fixed tools).
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
    """Tools and limits the Manager grants an analysis.

    Attributes:
        tools: Allowed tools (a subset of :data:`U1_ANALYSIS_TOOLS` for fixed
            tools; ``run_code`` and ``load_playbook`` may be added).
        limits: Call count, total time and output characters;
            plus the failures and the time of one ``run_code``.
        sources: The table aliases the tools may choose. Empty
            means the single table case.
    """

    tools: tuple[AnalysisTool, ...]
    limits: dict[str, int]
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tools", tuple(AnalysisTool(str(t)) for t in self.tools)
        )
        object.__setattr__(self, "sources", tuple(str(s) for s in self.sources))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tools": [t.value for t in self.tools],
            "limits": {k: int(v) for k, v in self.limits.items()},
        }
        if self.sources:
            data["sources"] = list(self.sources)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalysisToolPolicy":
        return cls(
            tools=tuple(AnalysisTool(str(t)) for t in data.get("tools", [])),
            limits={str(k): int(v) for k, v in dict(data.get("limits", {})).items()},
            sources=tuple(str(s) for s in data.get("sources", [])),
        )


@dataclass(frozen=True)
class JoinPlan:
    """The join key found between the axis table and one other.

    Attributes:
        axis: The alias of the axis table (one row per person).
        table: The alias of the table joined onto the axis.
        key_column: The same-named column of matching type in both tables;
            its values all differ in the axis table.
        match_rate: Share of the smaller table's values found in the other.
    """

    axis: str
    table: str
    key_column: str
    match_rate: float

    def __post_init__(self) -> None:
        if self.axis == self.table:
            raise ValueError("a table is not joined to itself")
        if not 0.0 <= float(self.match_rate) <= 1.0:
            raise ValueError("match_rate must be within 0..1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "table": self.table,
            "key_column": self.key_column,
            "match_rate": float(self.match_rate),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JoinPlan":
        return cls(
            axis=str(data["axis"]),
            table=str(data["table"]),
            key_column=str(data["key_column"]),
            match_rate=float(data["match_rate"]),
        )


@dataclass(frozen=True)
class EmittedResult:
    """What ``emit_result`` leaves, partly recounted by the library.

    ``finalize_ai_result`` counts ``population`` and each segment's ``size``
    again from the data; those (and the shares made of them,
    :meth:`recounted`) are the only numbers that may back a segment or an
    evidence. ``statistics`` and each segment's ``features`` are what the
    AI-written code passed to ``emit_result``: they are shown back to the
    analysis agent but are NOT recounted, so they never validate anything.

    Attributes:
        population: People of ``population_source`` (the axis table's people
            for a joined table, the distinct IDs for a history). Recounted.
        population_source: ``source_n``, ``join:<axis>+<table>[+<table>]`` or
            ``people:source_n`` (the people of a history table, by the person
            column the user confirmed).
        segments: ``{segment_key, size, features}`` per segment. ``size`` is
            recounted; ``features`` is the code's own (not recounted).
        statistics: ``{name, value, p_value?, effect_size?}`` of tests, as the
            code passed them (not recounted).
        joined_columns: The key and the columns used, for a joined table.
        tables_read: The table aliases the code read (``read_table`` /
            ``join_tables``), whether or not it joined them with join_tables.
        source_rows: Rows of the history (``people:`` only). Counted.
        rows_without_id: Rows of the history with no ID, so of no one in the
            population (``people:`` only). Counted.
        largest_id_rows: The most rows one ID has (``people:`` only); a value
            that stands for "no one" shows here. Counted.
    """

    population: int
    population_source: str
    segments: tuple[dict[str, Any], ...]
    statistics: tuple[dict[str, Any], ...] = ()
    joined_columns: tuple[str, ...] = ()
    tables_read: tuple[str, ...] = ()
    source_rows: Optional[int] = None
    rows_without_id: Optional[int] = None
    largest_id_rows: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(dict(s) for s in self.segments))
        object.__setattr__(self, "statistics", tuple(dict(s) for s in self.statistics))
        object.__setattr__(
            self, "joined_columns", tuple(str(c) for c in self.joined_columns)
        )
        object.__setattr__(self, "tables_read", tuple(str(t) for t in self.tables_read))
        if not _POPULATION_SOURCE.match(str(self.population_source)):
            raise ValueError("unknown population source")
        if int(self.population) < 0:
            raise ValueError("population must not be negative")
        for count in (self.source_rows, self.rows_without_id, self.largest_id_rows):
            if count is not None and int(count) < 0:
                raise ValueError("row counts must not be negative")
        for segment in self.segments:
            if int(segment.get("size", -1)) < 0 or not segment.get("segment_key"):
                raise ValueError("a segment has a key and a size of 0 or more")

    @property
    def from_history(self) -> bool:
        """Whether the population is the people of a history table."""
        return self.population_source.startswith(_PEOPLE_PREFIX)

    def size_of(self, segment_key: str) -> int | None:
        """The recounted size of ``segment_key``, or ``None`` when absent."""
        for segment in self.segments:
            if str(segment.get("segment_key")) == segment_key:
                return int(segment["size"])
        return None

    def recounted(self) -> dict[str, Any]:
        """Only the numbers the library counted: the population and the sizes."""
        return {
            "population": int(self.population),
            "population_source": self.population_source,
            "segments": [
                {"segment_key": str(s["segment_key"]), "size": int(s["size"])}
                for s in self.segments
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "population": int(self.population),
            "population_source": self.population_source,
            "segments": [dict(s) for s in self.segments],
        }
        if self.statistics:
            data["statistics"] = [dict(s) for s in self.statistics]
        if self.joined_columns:
            data["joined_columns"] = list(self.joined_columns)
        if self.tables_read:
            data["tables_read"] = list(self.tables_read)
        for key in ("source_rows", "rows_without_id", "largest_id_rows"):
            value = getattr(self, key)
            if value is not None:
                data[key] = int(value)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EmittedResult":
        return cls(
            population=int(data["population"]),
            population_source=str(data["population_source"]),
            segments=tuple(dict(s) for s in data.get("segments") or []),
            statistics=tuple(dict(s) for s in data.get("statistics") or []),
            joined_columns=tuple(str(c) for c in data.get("joined_columns") or []),
            tables_read=tuple(str(t) for t in data.get("tables_read") or []),
            source_rows=_count_or_none(data.get("source_rows")),
            rows_without_id=_count_or_none(data.get("rows_without_id")),
            largest_id_rows=_count_or_none(data.get("largest_id_rows")),
        )


def _count_or_none(value: Any) -> Optional[int]:
    return None if value is None else int(value)


#: ``RunCodeOutcome.changed`` when the check found processes the code left.
CHANGED_PROCESS = "process"
_PROCESS_SCAN_OK = "ok"


def processes_left(value: Mapping[str, Any]) -> Optional[bool]:
    """Whether the isolated check found processes AI-written code left.

    Security review: the check lists the session's processes and stops those
    the code left (``process_scan``, ``leftover_processes``). Any left is a
    change. ``None`` when the scan did not run or its answer cannot be read:
    the caller fails closed, but that is no tamper.
    """
    if value.get("process_scan") != _PROCESS_SCAN_OK:
        return None
    count = value.get("leftover_processes")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return count > 0


# The library's error codes; anything else is never written to a log.
_CHECK_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def check_failure_reason(checked: Mapping[str, Any]) -> str:
    """Why an isolated check could not be read, as a fixed word for the log.

    The library's error code when it gave one (``OUTPUT_TOO_LARGE`` ...),
    otherwise ``not_ok``, ``unreadable``, ``no_hashes`` or
    ``scan_unavailable``. Free text from the answer is never returned.
    """
    if not checked.get("ok"):
        error = checked.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if isinstance(code, str) and _CHECK_ERROR_CODE.match(code):
            return code
        return "not_ok"
    value = checked.get("value")
    if not isinstance(value, Mapping):
        return "unreadable"
    if not isinstance(value.get("hashes"), Mapping):
        return "no_hashes"
    return "scan_unavailable"


@dataclass(frozen=True)
class RunCodeOutcome:
    """How one ``run_code`` call ended, as the Service tells the gate.

    Attributes:
        ok: The code ran and ``emit_result`` was recounted.
        error_code: Fixed code of a failure (never the error text).
        timed_out: The run passed its time and was cancelled.
        cancel_failed: The cancel raised or the stop could not be read.
        tampered: The library or a prepared Parquet changed, or a
            hash to compare was missing (fail closed).
        changed: What changed: ``library``, ``parquet`` and / or ``process``
            (processes the code left).
        verify_failed: The isolated check itself failed or its answer could
            not be read. The tools halt (fail closed) but it is no tamper.
        no_result: The code ended normally without calling
            ``emit_result`` (a look around). It is a call but no failure.
    """

    ok: bool
    error_code: Optional[str] = None
    timed_out: bool = False
    cancel_failed: bool = False
    tampered: bool = False
    changed: tuple[str, ...] = ()
    verify_failed: bool = False
    no_result: bool = False


@runtime_checkable
class RunCodeGate(Protocol):
    """What ``AnalysisToolService`` asks before and tells after ``run_code``.

    The Manager's budget implements it, so the Service never imports the
    Manager.
    """

    def allows_run_code(self) -> bool:
        """Whether ``run_code`` may still run (not taken off)."""
        ...

    def halted(self) -> bool:
        """Whether every tool must stop (a change was found)."""
        ...

    def record(self, outcome: RunCodeOutcome) -> None:
        """Count the outcome of one call."""
        ...


@dataclass(frozen=True)
class AnalysisRecord:
    """The full analysis record kept in S3.

    It never holds a presigned URL or row data.

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
        tool_calls: ``[{tool_call_id, tool, ok, error_code, playbook}]`` (no
            output; ``playbook`` is the fixed name ``load_playbook`` read).
        source_file_ids: The files used, in ``source_checksums`` order.
            Empty in older records.
        source_display_names: Their display names, kept so the record can
            name a deleted file. Empty in older records.
        table_summaries: How each table was read (rows, columns, their kinds
            and the missing values; never a value of a row). Empty in older
            records.
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
    table_summaries: tuple[dict[str, Any], ...] = field(default=())

    def __post_init__(self) -> None:
        for name in (
            "code_blocks",
            "source_checksums",
            "tool_calls",
            "source_file_ids",
            "source_display_names",
            "table_summaries",
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
        table_summaries: Sequence[Mapping[str, Any]] = (),
    ) -> "AnalysisRecord":
        """A new record written at the end of a generation."""
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
            table_summaries=tuple(dict(t) for t in table_summaries),
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
        if self.table_summaries:
            data["table_summaries"] = [dict(t) for t in self.table_summaries]
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
            table_summaries=tuple(dict(t) for t in data.get("table_summaries", [])),
        )
