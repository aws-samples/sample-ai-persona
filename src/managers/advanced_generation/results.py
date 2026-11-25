"""What a finished advanced generation leaves behind (W5-6, BR9.1, contract C7/C8).

Pure builders of the run summary, the analysis record and the candidates, so
the Manager only orders the writes. Nothing here reads a Service.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ...config import config
from ...models.advanced_generation import AdvancedGeneration
from ...models.analysis import (
    DEFAULT_SEED,
    DOCUMENT_PREPARE_VERSION,
    PLAYBOOK_NAMES,
    AnalysisPlan,
    AnalysisRecord,
    AnalysisRun,
    DocumentMode,
    PrepareReport,
    Segment,
)
from ...models.demographics import VALID_GENDERS
from ...models.persona import Persona
from ...models.persona_candidate import PersonaCandidate
from ...models.source_file import SourceFile, SourceKind
from .prompts import AdvancedPersonaOutput

#: ``generation_context.mode`` of an advanced generation's persona (C7).
ADVANCED_MODE = "advanced"
#: Version of the ``generation_context`` it carries (C7).
CONTEXT_SCHEMA_VERSION = 1


def build_analysis_run(
    *,
    generation: AdvancedGeneration,
    plan: AnalysisPlan,
    report: Optional[PrepareReport],
    sources: Sequence[SourceFile],
    tool_call_ids: Sequence[str],
    library_sha256: str,
    unverified_segment_ids: Sequence[str],
    document_mode: Optional[DocumentMode] = None,
    ai_code_sha256: Sequence[str] = (),
    extra_params: Optional[Mapping[str, Any]] = None,
) -> AnalysisRun:
    """The summary kept on the generation and each candidate (contract C8).

    U4: ``report`` is ``None`` for documents alone; every source's checksum is
    kept in ``source_file_ids`` order, and the document mode is a parameter.
    U6: the hashes of the successful AI-written code follow the library's,
    and ``extra_params`` adds ``detailed_analysis`` and ``join`` (BR37.4).
    """
    params: dict[str, Any] = {
        "persona_count": generation.persona_count,
        "split_axis": plan.split_axis,
        "unverified_segment_ids": list(unverified_segment_ids),
    }
    if document_mode is not None:
        params["document_mode"] = document_mode.value
    params.update(extra_params or {})
    return AnalysisRun(
        method=plan.method.value,
        plan_ref=generation.generation_id,
        code_sha256=(library_sha256, *ai_code_sha256),
        params=params,
        source_checksums=tuple(s.checksum or "" for s in sources),
        prepare_version=(
            report.prepare_version if report else DOCUMENT_PREPARE_VERSION
        ),
        tool_call_ids=tuple(tool_call_ids),
        seed=DEFAULT_SEED,
    )


def build_record(
    *,
    generation: AdvancedGeneration,
    plan: AnalysisPlan,
    report: Optional[PrepareReport],
    sources: Sequence[SourceFile],
    calls: Sequence[Any],
    library_sha256: str,
    now: datetime,
    document_mode: Optional[DocumentMode] = None,
    ai_code: Sequence[tuple[str, str, str, Optional[str]]] = (),
) -> AnalysisRecord:
    """The analysis record: the code that ran and its hash (BR9.1, BR38.2).

    ``calls`` are the tool calls (``tool_call_id``, ``tool``, ``ok``,
    ``error_code``); their outputs are not kept. No URL goes in. U4: one
    ``prepare_document`` block per document, then the quote check. U6: one
    ``prepare`` per table, and ``ai_code`` (``tool_call_id``, code, sha256 of
    the successful ``run_code`` calls only) as ``ai_written`` blocks in the
    order they ran; failed calls stay in ``tool_calls`` with their code only.
    """
    blocks: list[dict[str, Any]] = []
    tables = [s for s in sources if s.kind is SourceKind.TABLE]
    for table in tables:
        metadata = table.table_metadata
        prepare_args = (
            {"encoding": metadata.encoding.value, "delimiter": metadata.delimiter}
            if metadata
            else {}
        )
        blocks += [
            {
                "kind": "fixed",
                "code": f"prepare({json.dumps(prepare_args, ensure_ascii=False)})",
                "sha256": library_sha256,
            },
            {"kind": "fixed", "code": "profile_dataset()", "sha256": library_sha256},
        ]
    for source in sources:
        if source.kind is SourceKind.DOCUMENT:
            fmt = source.source_format.value if source.source_format else ""
            blocks.append(
                {
                    "kind": "fixed",
                    "code": f'prepare_document({{"format": "{fmt}"}})',
                    "sha256": library_sha256,
                }
            )
    if document_mode is not None:
        blocks.append(
            {
                "kind": "fixed",
                "code": f"verify_quotes()  # {document_mode.value}",
                "sha256": library_sha256,
            }
        )
    written = {call_id: (code, sha, title) for call_id, code, sha, title in ai_code}
    for c in calls:
        if c.tool == "run_code":
            if c.tool_call_id in written:
                code, sha, title = written[c.tool_call_id]
                block: dict[str, Any] = {
                    "kind": "ai_written",
                    "code": code,
                    "sha256": sha,
                }
                if title:
                    # BR37.4: the joined columns and the match rates (S9)
                    block["title"] = title
                blocks.append(block)
            continue
        if c.tool == "load_playbook":
            continue
        blocks.append(
            {
                "kind": "fixed",
                "code": f"{c.tool}()  # {c.tool_call_id}",
                "sha256": library_sha256,
            }
        )
    return AnalysisRecord.create_new(
        record_id=generation.generation_id,
        owner_sub=generation.owner_sub,
        generation_id=generation.generation_id,
        plan=plan.to_dict(),
        code_blocks=blocks,
        seed=DEFAULT_SEED,
        prepare_version=report.prepare_version if report else DOCUMENT_PREPARE_VERSION,
        source_checksums=[s.checksum or "" for s in sources],
        now=now,
        # U2 BR14.3: name the files even after they are deleted
        source_file_ids=[s.source_file_id for s in sources],
        source_display_names=[s.display_name for s in sources],
        tool_calls=[
            {
                "tool_call_id": c.tool_call_id,
                "tool": c.tool,
                "ok": c.ok,
                **({"error_code": c.error_code} if c.error_code else {}),
                **_playbook_of(c),
            }
            for c in calls
        ],
    )


def _playbook_of(call: Any) -> dict[str, str]:
    """U6 addendum: the playbook a ``load_playbook`` read (a fixed name only)."""
    name = getattr(call, "playbook", None)
    if call.tool == "load_playbook" and name in PLAYBOOK_NAMES:
        return {"playbook": str(name)}
    return {}


def build_candidates(
    *,
    generation: AdvancedGeneration,
    plan: AnalysisPlan,
    segments: Sequence[Segment],
    personas: Sequence[AdvancedPersonaOutput],
    run: AnalysisRun,
    unverified_segment_ids: Sequence[str],
    now: datetime,
) -> list[PersonaCandidate]:
    """One candidate per persona, readable as long as the result (24 hours).

    ``generation_context`` (C7) points at the pending record (the generation
    ID); ``PersonaManager`` re-points it at the persona's copy on save (BR9.4).
    """
    shared = {
        "mode": ADVANCED_MODE,
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "plan_summary": {
            "interpreted_purpose": plan.interpreted_purpose,
            "method": plan.method.value,
            "rationale": plan.rationale,
        },
        "segments": [s.to_dict() for s in segments],
        "analysis_run": run.to_dict(),
        "analysis_record_id": generation.generation_id,
        "code_sha256": list(run.code_sha256),
        "unverified_segment_ids": list(unverified_segment_ids),
    }
    return [
        PersonaCandidate.from_persona(
            _persona_of(output),
            owner_sub=generation.owner_sub,
            generation_id=generation.generation_id,
            ttl_seconds=config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS,
            now=now,
            generation_context={
                **shared,
                "segment_id": output.segment_id,
                "evidence_ids": list(output.evidence_ids),
            },
        )
        for output in personas
    ]


def _persona_of(output: AdvancedPersonaOutput) -> Persona:
    return Persona.create_new(
        name=output.name,
        age=int(output.age),
        occupation=output.occupation,
        background=output.background,
        values=list(output.values),
        pain_points=list(output.pain_points),
        goals=list(output.goals),
        gender=output.gender if output.gender in VALID_GENDERS else None,
        country=output.country,
        city=output.city,
    )
