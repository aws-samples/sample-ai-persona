"""
データセット連携機能の統合テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.models.dataset import Dataset, DatasetColumn, PersonaDatasetBinding
from src.models.persona import Persona


class TestDatasetIntegrationFlow:
    """データセット連携フロー統合テスト"""

    @pytest.fixture
    def sample_dataset(self):
        """サンプルデータセット"""
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
        """サンプルペルソナ"""
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
        """サンプル紐付け"""
        return PersonaDatasetBinding(
            id="binding-integration-001",
            persona_id=sample_persona.id,
            dataset_id=sample_dataset.id,
            binding_keys={"user_id": "U12345"},
            created_at=datetime.now(),
        )

    def test_dataset_binding_flow(self, sample_dataset, sample_persona, sample_binding):
        """データセット紐付けフローテスト"""
        # 紐付け情報の検証
        assert sample_binding.persona_id == sample_persona.id
        assert sample_binding.dataset_id == sample_dataset.id
        assert sample_binding.binding_keys["user_id"] == "U12345"

    def test_dataset_to_dict_and_back(self, sample_dataset):
        """データセットのシリアライズ/デシリアライズテスト"""
        # 辞書に変換
        data = sample_dataset.to_dict()

        # 辞書から復元
        restored = Dataset.from_dict(data)

        # 検証
        assert restored.id == sample_dataset.id
        assert restored.name == sample_dataset.name
        assert len(restored.columns) == len(sample_dataset.columns)
        assert restored.row_count == sample_dataset.row_count

    def test_binding_to_dict_and_back(self, sample_binding):
        """紐付けのシリアライズ/デシリアライズテスト"""
        # 辞書に変換
        data = sample_binding.to_dict()

        # 辞書から復元
        restored = PersonaDatasetBinding.from_dict(data)

        # 検証
        assert restored.id == sample_binding.id
        assert restored.persona_id == sample_binding.persona_id
        assert restored.dataset_id == sample_binding.dataset_id
        assert restored.binding_keys == sample_binding.binding_keys


class TestInterviewDatasetIntegration:
    """インタビューモードデータセット連携統合テスト"""

    @pytest.fixture
    def sample_personas(self):
        """サンプルペルソナリスト"""
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
        """データセット連携有効のインタビューセッションテスト"""
        from src.managers.interview_manager import InterviewSession

        session = InterviewSession(
            id="interview-dataset-001",
            participants=[p.id for p in sample_personas],
            messages=[],
            created_at=datetime.now(),
            enable_memory=False,
            enable_dataset=True,
        )

        assert session.enable_dataset is True
        assert len(session.participants) == 2

    def test_interview_session_status_includes_dataset(self, sample_personas):
        """セッションステータスにenable_datasetが含まれるテスト"""
        from src.managers.interview_manager import InterviewManager, InterviewSession

        # InterviewManagerのモック
        with patch.object(
            InterviewManager, "__init__", lambda x, *args, **kwargs: None
        ):
            manager = InterviewManager()
            manager.logger = Mock()
            manager._active_sessions = {}
            manager._session_agents = {}

            # セッションを追加
            session = InterviewSession(
                id="interview-status-001",
                participants=["p1", "p2"],
                messages=[],
                created_at=datetime.now(),
                enable_dataset=True,
            )
            manager._active_sessions[session.id] = session
            manager._session_agents[session.id] = []

            # ステータス取得
            status = manager.get_session_status(session.id)

            assert status["enable_dataset"] is True


class TestDiscussionDatasetIntegration:
    """議論モードデータセット連携統合テスト"""

    def test_agent_discussion_manager_create_agents_with_dataset(self):
        """データセット連携付きエージェント作成テスト"""
        from src.managers.agent_discussion_manager import AgentDiscussionManager
        import inspect

        # create_persona_agentsメソッドのシグネチャを確認
        sig = inspect.signature(AgentDiscussionManager.create_persona_agents)
        params = list(sig.parameters.keys())

        # enable_datasetパラメータが存在することを確認
        assert "enable_dataset" in params

    def test_interview_manager_create_agents_with_dataset(self):
        """インタビュー用データセット連携付きエージェント作成テスト"""
        from src.managers.interview_manager import InterviewManager
        import inspect

        # _create_interview_persona_agentsメソッドのシグネチャを確認
        sig = inspect.signature(InterviewManager._create_interview_persona_agents)
        params = list(sig.parameters.keys())

        # enable_datasetパラメータが存在することを確認
        assert "enable_dataset" in params

    def test_start_interview_session_with_dataset(self):
        """データセット連携付きインタビューセッション開始テスト"""
        from src.managers.interview_manager import InterviewManager
        import inspect

        # start_interview_sessionメソッドのシグネチャを確認
        sig = inspect.signature(InterviewManager.start_interview_session)
        params = list(sig.parameters.keys())

        # enable_datasetパラメータが存在することを確認
        assert "enable_dataset" in params
