"""What a finished advanced generation leaves behind (W5-6, BR9.1, contract C7/C8).

Pure builders of the run summary, the analysis record and the candidates, so
the Manager only orders the writes. Nothing here reads a Service.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Sequence

from ...config import config
from ...models.advanced_generation import AdvancedGeneration
from ...models.analysis import (
    DEFAULT_SEED,
    AnalysisPlan,
    AnalysisRecord,
    AnalysisRun,
    PrepareReport,
    Segment,
)
from ...models.demographics import VALID_GENDERS
from ...models.persona import Persona
from ...models.persona_candidate import PersonaCandidate
from ...models.source_file import SourceFile
from .prompts import AdvancedPersonaOutput

#: ``generation_context.mode`` of an advanced generation's persona (C7).
ADVANCED_MODE = "advanced"
#: Version of the ``generation_context`` it carries (C7).
CONTEXT_SCHEMA_VERSION = 1


def build_analysis_run(
    *,
    generation: AdvancedGeneration,
    plan: AnalysisPlan,
    report: PrepareReport,
    source: SourceFile,
    tool_call_ids: Sequence[str],
    library_sha256: str,
    unverified_segment_ids: Sequence[str],
) -> AnalysisRun:
    """The summary kept on the generation and each candidate (contract C8)."""
    return AnalysisRun(
        method=plan.method.value,
        plan_ref=generation.generation_id,
        code_sha256=(library_sha256,),
        params={
            "persona_count": generation.persona_count,
            "split_axis": plan.split_axis,
            "unverified_segment_ids": list(unverified_segment_ids),
        },
        source_checksums=(source.checksum or "",),
        prepare_version=report.prepare_version,
        tool_call_ids=tuple(tool_call_ids),
        seed=DEFAULT_SEED,
    )


def build_record(
    *,
    generation: AdvancedGeneration,
    plan: AnalysisPlan,
    report: PrepareReport,
    source: SourceFile,
    calls: Sequence[Any],
    library_sha256: str,
    now: datetime,
) -> AnalysisRecord:
    """The analysis record: the fixed functions that ran and their hash (BR9.1).

    ``calls`` are the tool calls (``tool_call_id``, ``tool``, ``ok``,
    ``error_code``); their outputs are not kept. No URL goes in.
    """
    metadata = source.metadata
    prepare_args = (
        {"encoding": metadata.encoding.value, "delimiter": metadata.delimiter}
        if metadata
        else {}
    )
    blocks = [
        {
            "kind": "fixed",
            "code": f"prepare({json.dumps(prepare_args, ensure_ascii=False)})",
            "sha256": library_sha256,
        },
        {"kind": "fixed", "code": "profile_dataset()", "sha256": library_sha256},
    ]
    blocks += [
        {
            "kind": "fixed",
            "code": f"{c.tool}()  # {c.tool_call_id}",
            "sha256": library_sha256,
        }
        for c in calls
    ]
    return AnalysisRecord.create_new(
        record_id=generation.generation_id,
        owner_sub=generation.owner_sub,
        generation_id=generation.generation_id,
        plan=plan.to_dict(),
        code_blocks=blocks,
        seed=DEFAULT_SEED,
        prepare_version=report.prepare_version,
        source_checksums=[source.checksum or ""],
        now=now,
        # U2 BR14.3: name the file even after it is deleted
        source_file_ids=[source.source_file_id],
        source_display_names=[source.display_name],
        tool_calls=[
            {
                "tool_call_id": c.tool_call_id,
                "tool": c.tool,
                "ok": c.ok,
                **({"error_code": c.error_code} if c.error_code else {}),
            }
            for c in calls
        ],
    )


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
