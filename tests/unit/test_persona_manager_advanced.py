"""PersonaManager with candidates of the advanced generation (US10.2, US10.3, BR9.4).

Saving an advanced candidate copies its analysis record to the new persona's
ID first, then saves the persona with ``generation_context`` pointing at that
copy; a failed save removes the copy. Deleting the persona removes its copy.
The record is read through the persona's read check. Ordinary candidates are
saved as before.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest

from src.managers.components.analysis_record_archive import AnalysisRecordArchive
from src.managers.persona_manager import PersonaManagerError
from src.models.analysis import AnalysisRecord
from src.models.errors import CodedError, ErrorCode
from src.models.persona_candidate import CandidateClaim, PersonaCandidate
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.persona_u5_world import NOW, A, B, World, make_persona

pytestmark = pytest.mark.unit

ADVANCED_CONTEXT = {
    "mode": "advanced",
    "schema_version": 1,
    "plan_summary": {"method": "cluster", "interpreted_purpose": "全体の層"},
    "segment_id": "seg-0",
    "analysis_record_id": "gen-1",
}


def _candidate(context: dict | None = ADVANCED_CONTEXT, name: str = "佐藤") -> Any:
    return PersonaCandidate.from_persona(
        make_persona(None, name=name),
        owner_sub=A.sub,
        generation_id="gen-1",
        ttl_seconds=60,
        now=NOW,
        generation_context=dict(context) if context is not None else None,
    )


def _claim(*candidates: PersonaCandidate) -> CandidateClaim:
    return CandidateClaim(token="tok", owner_sub=A.sub, candidates=tuple(candidates))


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def archive() -> Mock:
    found = Mock(spec=AnalysisRecordArchive)
    found.commit_copy.side_effect = lambda owner, record, persona: persona
    return found


class TestSave:
    def test_copies_the_record_then_saves_with_the_copy(self, world, archive) -> None:
        candidate = _candidate()

        result = world.manager(analysis_records=archive).save_generated_personas(
            A, _claim(candidate)
        )

        (persona_id,) = result.saved_persona_ids
        archive.commit_copy.assert_called_once_with(A.sub, "gen-1", persona_id)
        context = world.personas[persona_id].generation_context or {}
        assert context["mode"] == "advanced"
        assert context["analysis_record_id"] == persona_id
        assert context["plan_summary"]["method"] == "cluster"

    def test_failed_save_removes_the_copy(self, world, archive) -> None:
        world.persona_store.put_new.side_effect = RuntimeError("down")

        result = world.manager(analysis_records=archive).save_generated_personas(
            A, _claim(_candidate())
        )

        assert result.saved_ids == ()
        ((owner, persona_id),) = [c.args for c in archive.discard_copy.call_args_list]
        assert owner == A.sub
        archive.commit_copy.assert_called_once()
        assert archive.commit_copy.call_args.args[2] == persona_id

    def test_failed_copy_fails_the_save(self, world, archive) -> None:
        archive.commit_copy.side_effect = CodedError(
            "copy failed", code=ErrorCode.GENERATION_OPERATION_FAILED
        )

        result = world.manager(analysis_records=archive).save_generated_personas(
            A, _claim(_candidate())
        )

        assert result.saved_ids == () and world.personas == {}

    def test_failed_copy_of_one_by_one_save_raises(self, world, archive) -> None:
        archive.commit_copy.side_effect = CodedError(
            "copy failed", code=ErrorCode.GENERATION_OPERATION_FAILED
        )
        candidate = _candidate()

        with raises_code(PersonaManagerError, ErrorCode.GENERATION_OPERATION_FAILED):
            world.manager(analysis_records=archive).save_generated_personas(
                A, _claim(candidate), edited=candidate.to_persona()
            )

    def test_failed_removal_of_the_copy_is_logged(self, world, archive, capsys) -> None:
        logs = LogCapture(capsys)
        world.persona_store.put_new.side_effect = RuntimeError("down")
        archive.discard_copy.side_effect = CodedError(
            "gone", code=ErrorCode.GENERATION_OPERATION_FAILED
        )

        world.manager(analysis_records=archive).save_generated_personas(
            A, _claim(_candidate())
        )

        (line,) = logs.events("persona.cleanup_failed")
        assert line["step"] == "analysis_record"

    def test_ordinary_candidates_do_not_touch_records(self, world, archive) -> None:
        result = world.manager(analysis_records=archive).save_generated_personas(
            A, _claim(_candidate(context={"data_type": "csv"}))
        )

        assert len(result.saved_ids) == 1
        archive.commit_copy.assert_not_called()


class TestDeleteAndRead:
    def _saved(self, world: World) -> Any:
        persona = make_persona(
            A.sub,
            generation_context={**ADVANCED_CONTEXT, "analysis_record_id": "p-rec"},
        )
        persona = replace(persona, id="p-rec")
        return world.add_persona(persona)

    def test_delete_removes_the_persona_copy(self, world, archive) -> None:
        persona = self._saved(world)
        world.sharing.delete_with_shares.side_effect = lambda principal, kind, pid: {
            "id": pid,
            "owner_sub": persona.owner_sub,
            "generation_context": persona.generation_context,
        }

        world.manager(analysis_records=archive).delete_persona(A, persona.id)

        archive.discard_copy.assert_called_once_with(A.sub, "p-rec")

    def test_reads_the_record_after_the_read_check(self, world, archive) -> None:
        persona = self._saved(world)
        record = Mock(spec=AnalysisRecord)
        archive.read_committed.return_value = record

        found = world.manager(analysis_records=archive).get_analysis_record(
            A, persona.id
        )

        assert found is record
        archive.read_committed.assert_called_once_with(A.sub, "p-rec")

    def test_unreadable_persona_is_not_found(self, world, archive) -> None:
        persona = self._saved(world)

        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager(analysis_records=archive).get_analysis_record(B, persona.id)
        archive.read_committed.assert_not_called()

    def test_ordinary_persona_has_no_record(self, world, archive) -> None:
        persona = world.add_persona(make_persona(A.sub))

        assert (
            world.manager(analysis_records=archive).get_analysis_record(A, persona.id)
            is None
        )
        archive.read_committed.assert_not_called()
