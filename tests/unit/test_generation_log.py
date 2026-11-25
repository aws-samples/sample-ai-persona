"""Tests for persona generation log persistence."""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.models.persona import Persona


SAMPLE_LOG = [
    {"type": "thinking", "content": "# データ分析\nCSVを確認します"},
    {"type": "tool_call", "content": "query: SELECT * FROM data LIMIT 5"},
    {
        "type": "tool_result",
        "tool_name": "query",
        "content": "| col1 | col2 |\n|---|---|\n| a | b |",
    },
]

SAMPLE_CONTEXT = {
    "data_type": "interview",
    "data_description": "活動ログ",
    "custom_prompt": "30代のエンジニアを生成",
    "source_files": ["interview.pdf"],
    "persona_count": 1,
    "generated_at": "2026-04-27T17:00:00",
}


class TestPersonaGenerationLogModel:
    """Persona モデルの generation_log / generation_context テスト"""

    def test_create_new_defaults_to_none(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_to_dict_omits_none_fields(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        d = persona.to_dict()
        assert "generation_log" not in d
        assert "generation_context" not in d

    def test_to_dict_includes_when_set(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        d = persona.to_dict()
        assert d["generation_log"] == SAMPLE_LOG
        assert d["generation_context"] == SAMPLE_CONTEXT

    def test_from_dict_without_log_fields(self):
        """既存ペルソナ（フィールドなし）からの復元"""
        d = {
            "id": "test-id",
            "name": "テスト",
            "age": 30,
            "occupation": "エンジニア",
            "background": "bg",
            "values": ["v"],
            "pain_points": ["p"],
            "goals": ["g"],
            "created_at": "2026-04-27T12:00:00",
            "updated_at": "2026-04-27T12:00:00",
        }
        persona = Persona.from_dict(d)
        assert persona.generation_log is None
        assert persona.generation_context is None

    def test_roundtrip_with_log(self):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="エンジニア",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        persona.generation_log = SAMPLE_LOG
        persona.generation_context = SAMPLE_CONTEXT
        restored = Persona.from_dict(persona.to_dict())
        assert restored.generation_log == SAMPLE_LOG
        assert restored.generation_context == SAMPLE_CONTEXT


class TestPersonaStoreGenerationLog:
    """PersonaStore が書く項目・読み戻す値での generation_log のテスト"""

    @staticmethod
    def _written(persona: Persona) -> dict:
        from src.services.stores.persona_store import PersonaStore

        client = Mock()
        PersonaStore(region="us-east-1", table_name="T", client=client).put_new(persona)
        return client.put_item.call_args.kwargs["Item"]

    @staticmethod
    def _persona(**kwargs) -> Persona:
        return Persona(
            id="test-id",
            name="Test",
            age=30,
            occupation="Eng",
            background="bg",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
            created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
            **kwargs,
        )

    def test_written_with_generation_log(self):
        item = self._written(
            self._persona(generation_log=SAMPLE_LOG, generation_context=SAMPLE_CONTEXT)
        )
        assert "generation_log" in item
        assert "generation_context" in item

    def test_written_without_generation_log(self):
        item = self._written(self._persona())
        assert "generation_log" not in item
        assert "generation_context" not in item

    def test_read_back_with_generation_log(self):
        from src.services.stores.persona_store import PersonaStore

        store = PersonaStore(region="us-east-1", table_name="T", client=Mock())
        persona = self._persona(
            generation_log=SAMPLE_LOG, generation_context=SAMPLE_CONTEXT
        )
        restored = store._persona(self._written(persona))
        assert restored.generation_log == SAMPLE_LOG
        assert restored.generation_context == SAMPLE_CONTEXT

    def test_read_back_without_generation_log(self):
        from src.services.stores.persona_store import PersonaStore

        store = PersonaStore(region="us-east-1", table_name="T", client=Mock())
        restored = store._persona(self._written(self._persona()))
        assert restored.generation_log is None
        assert restored.generation_context is None


class TestAdvancedDocumentLogFields:
    """U4 NFR6.13, NFR6.14, NFR7.3: the document events keep counts only."""

    def test_document_events_drop_names_and_text(self, capsys) -> None:
        from src.managers.shared.advanced_generation_log import log_advanced_event
        from tests.log_helpers import LogCapture

        logs = LogCapture(capsys)
        log_advanced_event(
            "advanced_generation.document_prepared",
            generation_id="g",
            document_index=0,
            format="pdf",
            pages=3,
            chars=1200,
            chunks=1,
            no_text_pages=1,
            elapsed_ms=5,
            filename="面談.pdf",
            heading="第1章",
        )
        log_advanced_event(
            "advanced_generation.document_mode",
            generation_id="g",
            mode="extraction",
            total_chars=300_000,
            chunks=21,
            body="本文",
        )
        log_advanced_event(
            "advanced_generation.evidence_extracted",
            generation_id="g",
            chunks=21,
            extracted=120,
            discarded_unverified=4,
            elapsed_ms=90_000,
            evidence_input_tokens=210_000,
            evidence_output_tokens=12_000,
            quote="引用",
        )

        prepared, mode, extracted = logs.records()
        assert "filename" not in prepared and "heading" not in prepared
        assert prepared["pages"] == 3 and prepared["no_text_pages"] == 1
        assert "body" not in mode and mode["total_chars"] == 300_000
        assert "quote" not in extracted
        assert extracted["evidence_input_tokens"] == 210_000


class TestAdvancedControlLogFields:
    """U5 NFR6.16〜NFR6.18, NFR3.29, NFR3.30: no instruction text, no session ID."""

    def test_control_events_keep_their_fields_only(self, capsys) -> None:
        from src.managers.shared.advanced_generation_log import log_advanced_event
        from tests.log_helpers import LogCapture

        logs = LogCapture(capsys)
        secret = {"revised_purpose": "社外秘の指示", "session_id": "s-1"}
        log_advanced_event(
            "advanced_generation.control_requested",
            generation_id="g",
            action="replan",
            **secret,
        )
        log_advanced_event(
            "advanced_generation.control_taken",
            generation_id="g",
            action="cancel",
            stage="running",
            replan_count=2,
            waited_seconds=120,
            latency_ms=3400,
            **secret,
        )
        log_advanced_event(
            "advanced_generation.ended_by_reader",
            generation_id="g",
            stage="running",
            error_code="generation_stalled",
            status="failed",
            judged_by="reader",
            **secret,
        )
        log_advanced_event(
            "advanced_generation.plan_wait_expired",
            generation_id="g",
            limit="plan_wait_minutes",
            waited_seconds=901,
            **secret,
        )
        log_advanced_event(
            "advanced_generation.finished",
            generation_id="g",
            status="cancelled",
            replan_count=1,
            waited_seconds=60,
            **secret,
        )

        requested, taken, reader, expired, finished = logs.records()
        for record in (requested, taken, reader, expired, finished):
            assert "revised_purpose" not in record and "session_id" not in record
        assert requested["action"] == "replan"
        assert taken["latency_ms"] == 3400 and taken["waited_seconds"] == 120
        assert reader["judged_by"] == "reader"
        assert reader["error_code"] == "generation_stalled"
        assert expired["limit"] == "plan_wait_minutes"
        assert finished["status"] == "cancelled" and finished["replan_count"] == 1


class TestAdvancedDetailedAnalysisLogFields:
    """U6 NFR6.19〜NFR6.21, NFR3.36: counts and codes, never code or output."""

    _SECRET = {"code": "CODE-SECRET", "stdout": "OUT-SECRET", "error": "ERR-SECRET"}

    def test_run_code_disabled_and_tamper_detected(self, capsys) -> None:
        from src.managers.shared.advanced_generation_log import log_advanced_event
        from tests.log_helpers import LogCapture

        logs = LogCapture(capsys)
        log_advanced_event(
            "advanced_generation.run_code_disabled",
            generation_id="g",
            status="disabled",
            reason="max_failures",
            failures=5,
            **self._SECRET,
        )
        log_advanced_event(
            "advanced_generation.tamper_detected",
            generation_id="g",
            status="detected",
            changed=["parquet"],
            **self._SECRET,
        )

        disabled, tamper = logs.records()
        assert disabled["status"] == "disabled" and disabled["reason"] == "max_failures"
        assert disabled["failures"] == 5
        assert tamper["status"] == "detected" and tamper["changed"] == ["parquet"]
        for record in (disabled, tamper):
            assert not set(self._SECRET) & set(record)

    @pytest.mark.parametrize("reason", ["verify_failed", "missing_hash"])
    def test_a_failed_check_and_a_missing_hash_are_run_code_disabled(
        self, capsys, reason: str
    ) -> None:
        """review-01 R-01 / R-03: told apart from a tamper by ``reason``."""
        from src.managers.advanced_generation.detailed_analysis import (
            REASON_MISSING_HASH,
            REASON_VERIFY_FAILED,
        )
        from src.managers.shared.advanced_generation_log import log_advanced_event
        from tests.log_helpers import LogCapture

        assert {REASON_VERIFY_FAILED, REASON_MISSING_HASH} == {
            "verify_failed",
            "missing_hash",
        }
        logs = LogCapture(capsys)
        log_advanced_event(
            "advanced_generation.run_code_disabled",
            generation_id="g",
            status="disabled",
            reason=reason,
            failures=0,
            **self._SECRET,
        )

        (record,) = logs.records()
        assert record["event"] == "advanced_generation.run_code_disabled"
        assert record["reason"] == reason and record["status"] == "disabled"
        assert not set(self._SECRET) & set(record)

    def test_finished_carries_the_new_counts(self, capsys) -> None:
        from src.managers.shared.advanced_generation_log import log_advanced_event
        from tests.log_helpers import LogCapture

        logs = LogCapture(capsys)
        log_advanced_event(
            "advanced_generation.finished",
            generation_id="g",
            status="done",
            detailed_analysis_used=True,
            run_code_calls=4,
            run_code_failures=3,
            run_code_disabled=False,
            table_count=2,
            joined_tables=1,
            join_match_rate=0.67,
            **self._SECRET,
        )

        (finished,) = logs.records()
        assert finished["detailed_analysis_used"] is True
        assert finished["run_code_calls"] == 4 and finished["run_code_failures"] == 3
        assert finished["run_code_disabled"] is False
        assert finished["table_count"] == 2 and finished["joined_tables"] == 1
        assert finished["join_match_rate"] == 0.67
        assert not set(self._SECRET) & set(finished)

    def test_the_new_events_are_listed(self) -> None:
        from src.managers.shared.advanced_generation_log import ALLOWED_FIELDS

        assert "changed" in ALLOWED_FIELDS["advanced_generation.tamper_detected"]
        assert ALLOWED_FIELDS["advanced_generation.run_code_disabled"] >= {
            "status",
            "reason",
            "failures",
        }
