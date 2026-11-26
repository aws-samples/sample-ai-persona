"""
Integration tests for the dataset binding feature.
"""

import pytest
from datetime import datetime

from src.models.dataset import Dataset, DatasetColumn
from src.models.persona import Persona
from src.models.persona_binding import PersonaBinding


class TestDatasetIntegrationFlow:
    """Dataset binding flow integration tests."""

    @pytest.fixture
    def sample_dataset(self):
        """Sample dataset."""
        now = datetime.now()
        return Dataset(
            id="dataset-integration-001",
            name="顧客購買履歴",
            description="顧客の購買履歴データ",
            s3_path="s3://test-bucket/purchase_history.csv",
            columns=[
                DatasetColumn(
                    name="user_id", data_type="string", description="ユーザーID"
                ),
                DatasetColumn(
                    name="product_name", data_type="string", description="商品名"
                ),
                DatasetColumn(
                    name="purchase_date", data_type="string", description="購入日"
                ),
                DatasetColumn(name="amount", data_type="integer", description="金額"),
            ],
            row_count=1000,
            created_at=now,
            updated_at=now,
        )

    @pytest.fixture
    def sample_persona(self):
        """Sample persona."""
        return Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="会社員",
            background="都内在住のサラリーマン",
            values=["効率性", "コスパ"],
            pain_points=["時間がない"],
            goals=["仕事の効率化"],
        )

    @pytest.fixture
    def sample_binding(self, sample_persona, sample_dataset):
        """Sample binding."""
        return PersonaBinding.for_dataset(
            sample_persona.id,
            sample_dataset.id,
            binding_keys={"user_id": "U12345"},
            now=datetime.now(),
        )

    def test_dataset_binding_flow(self, sample_dataset, sample_persona, sample_binding):
        """Dataset binding flow."""
        # Verify the binding
        assert sample_binding.persona_id == sample_persona.id
        assert sample_binding.target_id == sample_dataset.id
        assert sample_binding.target_key == f"DATASET#{sample_dataset.id}"
        assert sample_binding.binding_keys["user_id"] == "U12345"

    def test_dataset_to_dict_and_back(self, sample_dataset):
        """Dataset serialization/deserialization."""
        # Convert to dict
        data = sample_dataset.to_dict()

        # Restore from dict
        restored = Dataset.from_dict(data)

        # Verify
        assert restored.id == sample_dataset.id
        assert restored.name == sample_dataset.name
        assert len(restored.columns) == len(sample_dataset.columns)
        assert restored.row_count == sample_dataset.row_count

    def test_binding_to_dict_and_back(self, sample_binding):
        """Binding serialization/deserialization."""
        # Convert to dict
        data = sample_binding.to_dict()

        # Restore from dict
        restored = PersonaBinding.from_dict(data)

        # Verify
        assert restored == sample_binding


def _session(personas, **kwargs):
    """An interview session (kept outside the app) of ``personas``."""
    from datetime import timezone

    from src.models.interview_session import InterviewSession

    return InterviewSession.create_new(
        owner_sub="user-a",
        persona_ids=[p.id for p in personas],
        persona_names={p.id: p.name for p in personas},
        memory_mode="disabled",
        now=datetime(2026, 10, 15, tzinfo=timezone.utc),
        **kwargs,
    )


class TestInterviewDatasetIntegration:
    """Interview mode dataset integration tests."""

    @pytest.fixture
    def sample_personas(self):
        """Sample persona list."""
        return [
            Persona.create_new(
                name="山田一郎",
                age=40,
                occupation="経営者",
                background="中小企業経営",
                values=["成長", "効率"],
                pain_points=["人材不足"],
                goals=["事業拡大"],
            ),
            Persona.create_new(
                name="鈴木二郎",
                age=32,
                occupation="エンジニア",
                background="IT企業勤務",
                values=["技術", "品質"],
                pain_points=["レガシーシステム"],
                goals=["モダン化"],
            ),
        ]

    def test_interview_session_with_dataset_enabled(self, sample_personas):
        """Interview session with dataset binding enabled."""
        session = _session(sample_personas, enable_dataset=True)

        assert session.enable_dataset is True
        assert session.enable_memory is False
        assert len(session.persona_ids) == 2

    def test_interview_session_status_includes_dataset(self, sample_personas):
        """enable_dataset survives saving (same value whichever instance handles it)."""
        from src.models.interview_session import InterviewSession

        session = _session(sample_personas, enable_dataset=True)
        stored = session.to_dict()

        assert stored["enable_dataset"] is True
        assert InterviewSession.from_dict(stored).enable_dataset is True
