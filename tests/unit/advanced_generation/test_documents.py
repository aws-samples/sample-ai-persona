"""AdvancedPersonaGenerationManager: documents, their mode, the evidence and its checks."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from src.config import config
from src.models.advanced_generation import GenerationStatus, ResultPartName
from src.models.errors import ErrorCode
from src.models.source_file import SourceFile
from tests.log_helpers import LogCapture, assert_not_leaked
from tests.unit.advanced_generation.helpers import (
    ADMIN,
    _ended_cleanly_all,
    _Env,
    _failed,
    _interview,
    _progress,
    _start_with,
)

pytestmark = pytest.mark.unit


class TestDocumentPreparation:
    """Document preparation."""

    def test_every_document_is_refreshed_brought_in_and_prepared(
        self, env: _Env, monkeypatch
    ) -> None:
        refreshed: list[str] = []
        original = env.s3.refresh_object

        def spy(key: str) -> Any:
            refreshed.append(key)
            return original(key)

        monkeypatch.setattr(env.s3, "refresh_object", spy)
        a = env.ready_document(_interview(2), filename="a.md")
        b = env.ready_document(_interview(3), filename="b.txt")
        manager = env.manager()
        generation_id = _start_with(manager, a, b)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert refreshed == [a.s3_key, b.s3_key]
        assert len(env.sandbox.download_urls) == 2
        prepared = [
            args for fn, args in env.sandbox.started if fn == "prepare_document"
        ]
        assert [p["source_index"] for p in prepared] == [0, 1]
        assert [p["format"] for p in prepared] == ["md", "txt"]
        assert prepared[0]["chunk_chars"] == config.ADVANCED_DOCUMENT_CHUNK_CHARS
        assert (
            prepared[0]["overlap_chars"] == config.ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS
        )
        messages = _progress(env, generation_id)
        assert any("1 件目の文書" in m and "分割 1 個" in m for m in messages)
        assert any("2 件目の文書" in m for m in messages)
        _ended_cleanly_all(env, a, b)

    def test_pages_and_pages_without_text_are_told(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        pages = [
            "Value: family time matters most to me every single day.",
            "",
            "Pain: waiting at the counter for thirty minutes is too long.",
        ]
        doc = env.ready_document(pdf_bytes(pages), filename="a.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        messages = _progress(env, generation_id)
        assert any("3 ページ" in m for m in messages)
        assert any("文字がないため除外" in m and "2" in m for m in messages)

    def test_the_table_is_prepared_before_the_documents(self, env: _Env) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(2))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        assert [fn for fn, _ in env.sandbox.started] == ["prepare", "prepare_document"]
        assert env.generations.get(generation_id).status is GenerationStatus.DONE

    def test_a_document_without_text_stops_before_any_model(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        table = env.ready_file()
        doc = env.ready_document(pdf_bytes(["", ""]), filename="blank.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_NO_TEXT.value
        assert failed.error_context == {"source_index": 1}
        assert env.agents.calls == []
        _ended_cleanly_all(env, table, doc)

    def test_an_encrypted_document_stops(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        data = pdf_bytes(["secret"], encrypted=True)
        # passed the upload check (say, the trailer was outside the tail read)
        doc = env.ready_document(data, filename="locked.pdf", inspect=False)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_ENCRYPTED.value
        assert failed.error_context == {"source_index": 0}
        _ended_cleanly_all(env, doc)

    def test_over_the_total_limit_stops_before_any_model(
        self, env: _Env, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_DOCUMENT_MAX_TOTAL_CHARS", 100)
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_SOURCE_TOO_LARGE.value
        assert failed.error_context["max_chars"] == 100
        assert failed.error_context["total_chars"] > 100
        assert env.agents.calls == []

    def test_only_integers_of_a_failure_are_kept(self, env: _Env) -> None:
        env.sandbox.fake_document_result = {
            "ok": False,
            "error": {
                "code": "DOCUMENT_NO_TEXT",
                "context": {"source_index": "本文の一部", "detail": "secret"},
            },
        }
        doc = env.ready_document(_interview(1))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.DOCUMENT_NO_TEXT.value
        assert not failed.error_context

    def test_the_twenty_minutes_count_the_table_and_the_documents(
        self, env: _Env, monkeypatch
    ) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(1))
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=15)
        polls = []
        original = env.sandbox.poll_command

        def held(session, command_id):
            if env.sandbox.functions.get(command_id) == "prepare_document":
                polls.append(command_id)
                return {"done": False, "status": "running", "progress": None}
            return original(session, command_id)

        monkeypatch.setattr(env.sandbox, "poll_command", held)
        manager = env.manager(sleep=lambda s: env.clock.advance(minutes=2))
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert failed.error_context["limit"] == "prepare_minutes"
        # about 5 minutes were left after the table, not a fresh 20
        assert len(polls) <= 4


class TestDocumentMode:
    """Document mode."""

    def _chars(self, env: _Env, text: str) -> int:
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)
        return int(
            env.generations.read_result_parts(
                generation_id, ResultPartName.DOCUMENT_REPORTS
            )[0]["chars"]
        )

    def test_exactly_the_limit_uses_the_full_text(
        self, env: _Env, monkeypatch, small_chunks
    ) -> None:
        text = _interview(3)
        chars = self._chars(env, text)
        monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", chars)
        env.agents.calls.clear()
        env.agents.documents_seen.clear()
        env.agents.data_seen.clear()
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "full_text"
        assert "extraction" not in env.agents.calls
        # the planning may be asked twice (the guide is filter for documents)
        planning_docs, synthesis_docs = (
            env.agents.documents_seen[0],
            env.agents.documents_seen[-1],
        )
        assert len(planning_docs) == 1 and planning_docs == synthesis_docs
        # the chunks are joined without the overlap: each line once
        assert planning_docs[0].count("価値: ") == 3
        assert len(planning_docs[0]) == chars
        assert any("全文を使います" in m for m in _progress(env, generation_id))

    def test_one_character_over_extracts_evidence(
        self, env: _Env, monkeypatch, small_chunks
    ) -> None:
        text = _interview(3)
        chars = self._chars(env, text)
        monkeypatch.setattr(config, "PERSONA_SOURCE_MAX_CHARS", chars - 1)
        env.agents.calls.clear()
        env.agents.documents_seen.clear()
        env.agents.data_seen.clear()
        doc = env.ready_document(text)
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "extraction"
        assert "extraction" in env.agents.calls
        # planning and synthesis get the evidence list, not the text
        assert env.agents.documents_seen
        assert all(docs == [] for docs in env.agents.documents_seen)
        assert "document_evidence" in env.agents.data_seen[0]
        assert any("根拠を取り出します" in m for m in _progress(env, generation_id))

    def test_a_cut_transfer_fails_the_generation(self, env: _Env, small_chunks) -> None:
        env.sandbox.truncate_chunks = True
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_OPERATION_FAILED.value


class TestEvidenceExtraction:
    """Evidence extraction."""

    def _run(self, env: _Env, *docs: SourceFile, count: int = 2) -> str:
        manager = env.manager()
        generation_id = _start_with(manager, *docs, count=count)
        manager.run(generation_id)
        return generation_id

    def _chunks(self, env: _Env, generation_id: str) -> int:
        return sum(
            r["chunks"]
            for r in env.generations.read_result_parts(
                generation_id, ResultPartName.DOCUMENT_REPORTS
            )
        )

    def test_one_call_per_chunk_and_never_more_than_the_limit_at_once(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 2)
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        chunks = self._chunks(env, generation_id)
        assert chunks >= 4
        assert env.agents.calls.count("extraction") == chunks
        assert env.agents.max_running <= 2

    @pytest.mark.parametrize("purpose", ["30 代の不満を優先", ""])
    def test_every_chunk_call_gets_the_users_purpose(
        self, env: _Env, extraction, purpose: str
    ) -> None:
        """The prompt asks to favour the instruction, so it must be sent."""
        doc = env.ready_document(_interview(8))
        manager = env.manager()
        generation_id = _start_with(manager, doc, purpose=purpose)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert env.agents.extraction_purposes
        assert set(env.agents.extraction_purposes) == {purpose}

    def test_progress_says_n_of_m_up_to_the_end(self, env: _Env, extraction) -> None:
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        chunks = self._chunks(env, generation_id)
        counts = [m for m in _progress(env, generation_id) if "根拠の抽出" in m]
        assert counts[-1].endswith(f"{chunks} / {chunks}")
        assert len(counts) <= 11

    def test_items_and_quotes_are_cut_to_the_limits(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_PER_CHUNK_MAX", 3)
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_QUOTE_MAX_CHARS", 12)
        doc = env.ready_document(_interview(6))

        generation_id = self._run(env, doc, count=1)

        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        by_chunk: dict[str, int] = {}
        for item in evidence:
            assert len(item["quote"]) <= 12
            chunk = item["id"].rsplit("-", 1)[0]
            by_chunk[chunk] = by_chunk.get(chunk, 0) + 1
        assert by_chunk and max(by_chunk.values()) <= 3

    def test_ids_are_renumbered_and_the_source_comes_from_the_chunk(
        self, env: _Env, extraction
    ) -> None:
        table_free = env.ready_document(_interview(2), filename="a.md")
        other = env.ready_document(_interview(2, prefix="B"), filename="b.md")

        generation_id = self._run(env, table_free, other)

        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert evidence
        for item in evidence:
            assert re.fullmatch(r"ev-d[01]-\d+-\d+", item["id"])
            expected = table_free if item["id"].startswith("ev-d0") else other
            assert item["source_id"] == expected.source_file_id
            assert item["location"]
        assert len({e["id"] for e in evidence}) == len(evidence)

    def test_a_failure_stops_the_rest_and_fails_the_generation(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 1)
        env.agents.extraction_mode = "fail_at:3"
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        assert failed.error_context == {"cause": "model"}
        assert env.agents.calls.count("extraction") == 3
        assert "planning" not in env.agents.calls
        _ended_cleanly_all(env, doc)

    def test_a_running_call_may_retry_until_another_one_fails(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 1)
        doc = env.ready_document(_interview(4))

        self._run(env, doc)

        assert env.agents.may_retry and all(callable(f) for f in env.agents.may_retry)
        # The run ended well: nothing stopped the retries of later calls
        assert all(f() for f in env.agents.may_retry)

    def test_after_a_failure_no_call_retries_or_starts(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_MAX_CONCURRENT", 2)
        env.agents.extraction_mode = "fail_at:1"
        doc = env.ready_document(_interview(8))

        generation_id = self._run(env, doc)

        assert _failed(env, generation_id).error_code == (
            ErrorCode.GENERATION_SANDBOX_UNAVAILABLE.value
        )
        # Only the two calls already running when the first one failed
        assert env.agents.calls.count("extraction") <= 2
        assert not any(f() for f in env.agents.may_retry)

    def test_the_time_left_is_checked_before_each_call(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(
            config, "ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS", 3601
        )
        doc = env.ready_document(_interview(4))

        generation_id = self._run(env, doc)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_TIMEOUT.value
        assert "extraction" not in env.agents.calls

    def test_an_answer_out_of_shape_gives_no_evidence(
        self, env: _Env, extraction
    ) -> None:
        env.agents.extraction_mode = "none"
        doc = env.ready_document(_interview(3))

        generation_id = self._run(env, doc)

        # nothing to rest the personas on: the verification fails
        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value


class TestQuoteVerification:
    """Quote verification."""

    def test_extracted_quotes_not_in_the_text_are_dropped_and_counted(
        self, env: _Env, monkeypatch, extraction
    ) -> None:
        monkeypatch.setattr(config, "ADVANCED_EVIDENCE_VERIFY_BATCH", 2)
        env.agents.extraction_mode = "fabricate"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.status is GenerationStatus.DONE
        stats = generation.evidence_stats
        assert stats["discarded_unverified"] == 1
        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert stats["extracted"] == len(evidence) + 1
        assert all("作り話" not in e["quote"] for e in evidence)
        batches = [a["quotes"] for fn, a in env.sandbox.called if fn == "verify_quotes"]
        assert batches and max(len(b) for b in batches) <= 2

    def test_full_text_quotes_are_checked_after_the_synthesis(self, env: _Env) -> None:
        env.agents.full_text_mode = "fabricate"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.document_mode.value == "full_text"
        assert generation.evidence_stats["discarded_unverified"] == 1
        evidence = env.generations.read_result_parts(
            generation_id, ResultPartName.EVIDENCE
        )
        assert all(re.fullmatch(r"ev-d0-0-\d+", e["id"]) for e in evidence)
        assert all("作り話" not in e["quote"] for e in evidence)


class TestDocumentVerificationAndResult:
    """Document verification and the result written."""

    def test_a_persona_without_goal_evidence_fails_the_verification(
        self, env: _Env
    ) -> None:
        env.agents.full_text_mode = "no_goal"
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        failed = _failed(env, generation_id)
        assert failed.error_code == ErrorCode.GENERATION_VERIFICATION_FAILED.value

    def test_unknown_evidence_ids_are_dropped_from_the_personas(
        self, env: _Env
    ) -> None:
        doc = env.ready_document(_interview(3))
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        generation = env.generations.get(generation_id)
        assert generation.status is GenerationStatus.DONE
        known = {
            e["id"]
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
        }
        for candidate_id in generation.candidate_ids or ():
            context = env.candidates.get(candidate_id).generation_context
            assert context["segment_id"] is None
            assert context["evidence_ids"]
            assert set(context["evidence_ids"]) <= known

    def test_extraction_assigns_evidence_to_segments_before_the_synthesis(
        self, env: _Env, extraction
    ) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(4))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)
        env.agents.assignments = {"ev-d1-0-0": "seg-0", "ev-d1-0-1": "seg-nope"}

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        calls = env.agents.calls
        assert calls.index("assignment") < calls.index("synthesis")
        evidence = {
            e["id"]: e
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
        }
        assert evidence["ev-d1-0-0"]["segment_id"] == "seg-0"
        assert evidence["ev-d1-0-0"]["assignment_reason"] == "30代の発言"
        assert "segment_id" not in evidence["ev-d1-0-1"]  # unknown segment
        assert "ev-unknown" not in evidence
        # the evidence without a segment goes to every persona
        synthesis = env.agents.data_seen[-1]
        given = {e["id"] for e in json.loads(synthesis["evidence"])}
        assert {"ev-d1-0-0", "ev-d1-0-1"} <= given

    def test_full_text_synthesis_returns_the_segments_itself(self, env: _Env) -> None:
        table = env.ready_file()
        doc = env.ready_document(_interview(2))
        manager = env.manager()
        generation_id = _start_with(manager, table, doc)

        manager.run(generation_id)

        assert env.generations.get(generation_id).status is GenerationStatus.DONE
        assert "assignment" not in env.agents.calls
        evidence = [
            e
            for e in env.generations.read_result_parts(
                generation_id, ResultPartName.EVIDENCE
            )
            if e.get("quote")
        ]
        assert any(e.get("segment_id") == "seg-0" for e in evidence)
        assert any("segment_id" not in e for e in evidence)

    def test_the_result_lists_the_documents_used(self, env: _Env) -> None:
        doc = env.ready_document(_interview(2), filename="面談A.md")
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)

        result = manager.get_result(ADMIN, generation_id)

        (report,) = result.document_reports
        assert report.source_id == doc.source_file_id
        assert result.source_names[doc.source_file_id] == "面談A.md"
        assert any(e.quote for e in result.evidence)

    def test_the_failure_names_the_users_file(self, env: _Env) -> None:
        from tests.document_fixtures import pdf_bytes

        doc = env.ready_document(pdf_bytes([""]), filename="白紙.pdf")
        manager = env.manager()
        generation_id = _start_with(manager, doc)
        manager.run(generation_id)

        progress = manager.stream_events(ADMIN, generation_id)

        assert progress.failure_context == {"source_index": 0, "filename": "白紙.pdf"}


class TestDocumentLogs:
    """Counts, time and tokens only."""

    def test_preparation_mode_and_extraction_are_logged_without_text(
        self, env: _Env, capsys, extraction
    ) -> None:
        logs = LogCapture(capsys)
        doc = env.ready_document(_interview(4), filename="秘密の面談.md")
        manager = env.manager()
        generation_id = _start_with(manager, doc)

        manager.run(generation_id)

        (prepared,) = logs.events("advanced_generation.document_prepared")
        assert prepared["document_index"] == 0 and prepared["format"] == "md"
        assert prepared["chars"] > 0 and prepared["chunks"] >= 2
        assert prepared["no_text_pages"] == 0
        assert isinstance(prepared["elapsed_ms"], int)
        (mode,) = logs.events("advanced_generation.document_mode")
        assert mode["mode"] == "extraction" and mode["chunks"] == prepared["chunks"]
        (extracted,) = logs.events("advanced_generation.evidence_extracted")
        assert extracted["chunks"] == prepared["chunks"]
        assert extracted["evidence_input_tokens"] == 100 * prepared["chunks"]
        assert extracted["evidence_output_tokens"] == 10 * prepared["chunks"]
        assert extracted["discarded_unverified"] == 0
        assert_not_leaked("秘密", logs.records())
        assert_not_leaked("家族と過ごす", logs.records())
        assert env.generations.get(generation_id).status is GenerationStatus.DONE
