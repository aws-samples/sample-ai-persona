"""Sample values the advanced generation (beta) screen tests render .

The router tests (``tests/api/``) hand them to mocked Managers and the
component tests (``tests/ui/``) render the real partials with them. Column
names and cells carry HTML on purpose so that both check the escaping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.managers.advanced_generation.manager import AdvancedGenerationResult
from src.models.advanced_generation import (
    AdvancedGeneration,
    EventType,
    GenerationEvent,
    GenerationStatus,
)
from src.models.analysis import (
    AnalysisMethod,
    AnalysisPlan,
    AnalysisRecord,
    AnalysisRun,
    DocumentMode,
    DocumentReport,
    Evidence,
    EvidenceKind,
    PrepareReport,
    ReportColumn,
    Segment,
)
from src.models.persona import Persona
from src.models.persona_candidate import PersonaCandidate
from src.models.source_file import (
    DocumentMetadata,
    PartUrl,
    SourceColumn,
    SourceEncoding,
    SourceFile,
    SourceFileStatus,
    SourceFormat,
    SourceKind,
    SourceMetadata,
    UploadSession,
)

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
OWNER = "local:dev@example.com"
FILE_ID = "file-1"
GENERATION_ID = "gen-1"
#: A column name and a cell that must reach the screen as text.
SCRIPT_TEXT = "<script>alert(1)</script>"
PART_URL = "https://bucket.s3.us-east-1.amazonaws.com/sources/x?X-Amz-Signature=abc"


def uploading_file(**changes: Any) -> SourceFile:
    """A file whose parts are being sent."""
    draft = SourceFile.create_new(
        owner_sub=OWNER,
        filename="members.csv",
        size_bytes=40 * 1024 * 1024,
        part_count=3,
        now=NOW,
        multipart_upload_id="upload-1",
        source_file_id=FILE_ID,
    )
    return draft.update(**changes)


def ready_file(**changes: Any) -> SourceFile:
    """An inspected table with HTML in a column name and a cell."""
    metadata = SourceMetadata.create(
        format=SourceFormat.CSV,
        encoding=SourceEncoding.CP932,
        delimiter=",",
        columns=[
            SourceColumn(name="会員ID", inferred_type="string"),
            SourceColumn(name="年齢", inferred_type="integer"),
            SourceColumn(name=SCRIPT_TEXT, inferred_type="string"),
        ],
        sample_rows=[["A0001", "34", SCRIPT_TEXT], ["A0002", "41", "東京都"]],
        estimated_rows=1240000,
        inspected_bytes=4 * 1024 * 1024,
        inspected_at=NOW,
    )
    return uploading_file(
        status=SourceFileStatus.READY,
        multipart_upload_id=None,
        metadata=metadata,
        checksum="crc==",
    ).update(**changes)


def error_file(code: str = "UPLOAD_SOURCE_INVALID") -> SourceFile:
    """A file whose inspection failed."""
    return uploading_file(
        status=SourceFileStatus.ERROR, multipart_upload_id=None, error_code=code
    )


def upload_session(part_numbers: tuple[int, ...] = (1, 2, 3)) -> UploadSession:
    """Part URLs the start or a refresh hands out."""
    return UploadSession(
        source_file_id=FILE_ID,
        part_size_bytes=16 * 1024 * 1024,
        part_count=3,
        part_urls=tuple(
            PartUrl(part_number=n, url=f"{PART_URL}&partNumber={n}")
            for n in part_numbers
        ),
        urls_expire_at=NOW + timedelta(hours=1),
    )


def generation(
    status: GenerationStatus = GenerationStatus.RUNNING, **changes: Any
) -> AdvancedGeneration:
    """A generation of :data:`OWNER` in ``status``."""
    base = AdvancedGeneration.create_new(
        owner_sub=OWNER,
        source_file_ids=[FILE_ID],
        purpose="",
        persona_count=2,
        now=NOW,
        ttl_seconds=48 * 3600,
        generation_id=GENERATION_ID,
    )
    finished = {"finished_at": NOW + timedelta(minutes=8)} if status.is_terminal else {}
    return base.update(status=status, **finished, **changes)


def event(sequence: int, event_type: EventType, summary: str) -> GenerationEvent:
    """A stored progress event."""
    return GenerationEvent.create_new(
        generation_id=GENERATION_ID,
        sequence=sequence,
        event_type=event_type,
        now=NOW,
        summary=summary,
    )


PLAN = AnalysisPlan(
    interpreted_purpose="年代ごとの違いを知る",
    questions=("年代で収入はどう違うか",),
    method=AnalysisMethod.CLUSTER,
    rationale="指示が空欄のため",
    budget={"max_calls": 30},
)

SEGMENTS = (
    Segment(
        id="S1",
        label="若い学生",
        definition={"kind": "cluster", "spec": {"k": 2}},
        size=150,
        share=0.5,
        features=({"name": "年齢", "value": 26.0, "baseline": 44.5},),
        evidence_ids=("E1",),
    ),
    Segment(
        id="S2",
        label=SCRIPT_TEXT,
        definition={"kind": "cluster", "spec": {"k": 2}},
        size=150,
        share=0.5,
        features=({"name": "年齢", "value": 63.0, "baseline": 44.5},),
        evidence_ids=("E2",),
    ),
)

EVIDENCE = (
    Evidence(
        id="E1",
        source_id=FILE_ID,
        location="tc-1",
        kind=EvidenceKind.ATTRIBUTE,
        claim="平均年齢が全体より低い",
        statistic={"value": 26.0, "population": 300},
    ),
    Evidence(
        id="E2",
        source_id=FILE_ID,
        location="tc-2",
        kind=EvidenceKind.ATTRIBUTE,
        claim="平均年齢が全体より高い",
        statistic={"value": 63.0, "population": 300},
    ),
)

RUN = AnalysisRun(
    method="cluster",
    plan_ref=GENERATION_ID,
    code_sha256=("a" * 64,),
    params={"unverified_segment_ids": ["S2"]},
    source_checksums=("crc==",),
    prepare_version="prepare 1.0.0",
    tool_call_ids=("tc-1", "tc-2"),
)


def advanced_context(segment_id: str, *, unverified: tuple[str, ...] = ()) -> dict:
    """The ``generation_context`` of an advanced candidate or saved persona."""
    return {
        "mode": "advanced",
        "schema_version": 1,
        "plan_summary": {
            "interpreted_purpose": PLAN.interpreted_purpose,
            "method": PLAN.method.value,
            "rationale": PLAN.rationale,
        },
        "segments": [s.to_dict() for s in SEGMENTS],
        "segment_id": segment_id,
        "evidence_ids": ["E1" if segment_id == "S1" else "E2"],
        "analysis_run": RUN.to_dict(),
        "analysis_record_id": GENERATION_ID,
        "code_sha256": list(RUN.code_sha256),
        "unverified_segment_ids": list(unverified),
    }


def _candidate(candidate_id: str, name: str, segment_id: str) -> PersonaCandidate:
    persona = Persona.create_new(
        name=name,
        age=26 if segment_id == "S1" else 63,
        occupation="学生" if segment_id == "S1" else "会社員",
        background="背景",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
    )
    candidate = PersonaCandidate.from_persona(
        persona,
        owner_sub=OWNER,
        generation_id=GENERATION_ID,
        ttl_seconds=24 * 3600,
        now=NOW,
        generation_context=advanced_context(segment_id, unverified=("S2",)),
    )
    return PersonaCandidate(
        candidate_id=candidate_id,
        owner_sub=candidate.owner_sub,
        generation_id=candidate.generation_id,
        persona_payload=candidate.persona_payload,
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        generation_context=candidate.generation_context,
    )


def result() -> AdvancedGenerationResult:
    """A finished result; ``S2`` was dropped by the verification."""
    done = generation(
        GenerationStatus.DONE,
        plan=PLAN,
        analysis_run=RUN,
        analysis_record_id=GENERATION_ID,
        candidate_ids=("cand-1", "cand-2"),
    )
    return AdvancedGenerationResult(
        generation=done,
        prepare_reports=(
            PrepareReport(
                source_id=FILE_ID,
                rows_read=300,
                rows_excluded=12,
                columns=(ReportColumn(name="年齢", type="BIGINT"),),
                encoding=SourceEncoding.CP932,
                prepare_version="prepare 1.0.0",
            ),
        ),
        segments=SEGMENTS,
        evidence=EVIDENCE,
        candidates=(
            _candidate("cand-1", "佐藤 美咲", "S1"),
            _candidate("cand-2", "田中 健", "S2"),
        ),
        unverified_segment_ids=("S2",),
    )


#: A document and its quoted evidence (HTML in the quote on purpose).
DOCUMENT_ID = "doc-1"


def ready_document(**changes: Any) -> SourceFile:
    """An inspected PDF of 80MB with its estimate."""
    draft = SourceFile.create_new(
        owner_sub=OWNER,
        filename="面談記録.pdf",
        size_bytes=80_000_000,
        part_count=5,
        now=NOW,
        source_file_id=DOCUMENT_ID,
        kind=SourceKind.DOCUMENT,
    )
    return draft.update(
        status=SourceFileStatus.READY,
        metadata=DocumentMetadata(
            format=SourceFormat.PDF,
            estimated_chars=1_600_000,
            inspected_bytes=5 * 1024 * 1024,
            inspected_at=NOW,
        ),
        checksum="crc2==",
    ).update(**changes)


DOCUMENT_EVIDENCE = (
    Evidence.for_document(
        id="ev-d1-0-0",
        source_id=DOCUMENT_ID,
        location="p.12 / 第3章 > 不満",
        kind=EvidenceKind.PAIN,
        claim="待ち時間が長いと感じている",
        quote=f"受付で30分も待たされた {SCRIPT_TEXT}",
        speaker_attributes={"年代": "30代"},
        segment_id="S1",
        assignment_reason="30代の学生の発言",
    ),
    Evidence.for_document(
        id="ev-d1-0-1",
        source_id=DOCUMENT_ID,
        location="p.3",
        kind=EvidenceKind.VALUE,
        claim="家族との時間を大切にする",
        quote="週末は家族と過ごす時間を何より大切にしている",
    ),
)


def document_result() -> AdvancedGenerationResult:
    """A finished result of a table and a document."""
    base = result()
    candidates = tuple(
        PersonaCandidate(
            candidate_id=c.candidate_id,
            owner_sub=c.owner_sub,
            generation_id=c.generation_id,
            persona_payload=c.persona_payload,
            created_at=c.created_at,
            expires_at=c.expires_at,
            generation_context={
                **(c.generation_context or {}),
                "evidence_ids": [
                    *(c.generation_context or {}).get("evidence_ids", []),
                    "ev-d1-0-0",
                ],
            },
        )
        for c in base.candidates
    )
    return AdvancedGenerationResult(
        generation=base.generation.update(
            source_file_ids=(FILE_ID, DOCUMENT_ID),
            document_mode=DocumentMode.EXTRACTION,
            evidence_stats={"extracted": 3, "discarded_unverified": 1},
        ),
        prepare_reports=base.prepare_reports,
        segments=base.segments,
        evidence=base.evidence + DOCUMENT_EVIDENCE,
        candidates=candidates,
        unverified_segment_ids=base.unverified_segment_ids,
        document_reports=(
            DocumentReport(
                source_id=DOCUMENT_ID,
                chars=812_345,
                chunks=55,
                headings=12,
                no_text_pages=(4,),
                pages=120,
            ),
        ),
        source_names={FILE_ID: "members.csv", DOCUMENT_ID: f"面談{SCRIPT_TEXT}.pdf"},
    )


def record() -> AnalysisRecord:
    """The analysis record with HTML in a code block (shown as text)."""
    return AnalysisRecord.create_new(
        record_id=GENERATION_ID,
        owner_sub=OWNER,
        generation_id=GENERATION_ID,
        plan=PLAN.to_dict(),
        code_blocks=[
            {
                "kind": "fixed",
                "code": f"cluster_segments(columns=['{SCRIPT_TEXT}'], k=2)",
                "sha256": "b" * 64,
            }
        ],
        seed=42,
        prepare_version="prepare 1.0.0",
        source_checksums=["crc=="],
        now=NOW,
    )
