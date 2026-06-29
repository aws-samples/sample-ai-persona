"""行動データ自動紐付け機能のユニットテスト"""

import pytest
from unittest.mock import Mock, patch

from src.models.persona import Persona


@pytest.fixture
def mock_persona():
    return Persona.create_new(
        name="テスト太郎",
        age=30,
        occupation="エンジニア",
        background="背景テスト",
        values=["v1"],
        pain_points=["p1"],
        goals=["g1"],
    )


class TestAutoLinkBehaviorFlag:
    """auto_link_behavior フラグのバリデーション（PersonaGenerationManager）"""

    def _make_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        mock_agent_service = Mock()
        mgr = PersonaGenerationManager(
            agent_service=mock_agent_service, database_service=Mock()
        )
        mgr._determine_tools = Mock(return_value=[])
        return mgr, mock_agent_service

    def test_auto_link_forces_persona_count_to_one(self, mock_persona):
        """auto_link_behavior=True の場合、persona_count が 1 に強制される"""
        from src.managers.persona_generation_manager import _PersonaListOutput

        mgr, mock_as = self._make_manager()
        mock_as.create_generation_agent.return_value = Mock()
        mock_as.run_persona_generation.return_value = (
            _PersonaListOutput(
                personas=[
                    {
                        "name": "テスト太郎",
                        "age": 30,
                        "occupation": "エンジニア",
                        "background": "背景",
                        "values": ["v1"],
                        "pain_points": ["p1"],
                        "goals": ["g1"],
                    }
                ]
            ),
            [],
        )

        mgr.generate_and_cache(
            file_contents=[],
            data_type="dwh",
            persona_count=5,
            data_description="テスト条件",
            auto_link_behavior=True,
        )

        call_kwargs = mock_as.run_persona_generation.call_args[1]
        assert "1個" in call_kwargs["prompt"]

    def test_auto_link_appends_behavior_extraction_instruction(self, mock_persona):
        """auto_link_behavior=True の場合、行動データ抽出指示がpromptに追記される"""
        from src.managers.persona_generation_manager import _PersonaListOutput

        mgr, mock_as = self._make_manager()
        mock_as.create_generation_agent.return_value = Mock()
        mock_as.run_persona_generation.return_value = (
            _PersonaListOutput(
                personas=[
                    {
                        "name": "テスト",
                        "age": 25,
                        "occupation": "営業",
                        "background": "bg",
                        "values": ["v"],
                        "pain_points": ["p"],
                        "goals": ["g"],
                    }
                ]
            ),
            [],
        )

        mgr.generate_and_cache(
            file_contents=[],
            data_type="dwh",
            persona_count=1,
            data_description="リピーター層",
            auto_link_behavior=True,
        )

        call_kwargs = mock_as.run_persona_generation.call_args[1]
        prompt = call_kwargs["prompt"]
        assert "特定1名の深掘り分析" in prompt
        assert "行動データCSVエクスポート" in prompt

    def test_without_auto_link_no_behavior_instruction(self, mock_persona):
        """auto_link_behavior=False の場合、行動データ抽出指示は追記されない"""
        from src.managers.persona_generation_manager import _PersonaListOutput

        mgr, mock_as = self._make_manager()
        mock_as.create_generation_agent.return_value = Mock()
        mock_as.run_persona_generation.return_value = (
            _PersonaListOutput(
                personas=[
                    {
                        "name": "テスト",
                        "age": 25,
                        "occupation": "営業",
                        "background": "bg",
                        "values": ["v"],
                        "pain_points": ["p"],
                        "goals": ["g"],
                    }
                ]
            ),
            [],
        )

        mgr.generate_and_cache(
            file_contents=[],
            data_type="dwh",
            persona_count=3,
            data_description="新規顧客",
            auto_link_behavior=False,
        )

        call_kwargs = mock_as.run_persona_generation.call_args[1]
        prompt = call_kwargs["prompt"]
        assert "行動データCSVエクスポート" not in prompt


class TestInferBehaviorDataType:
    """infer_behavior_data_type のテスト"""

    def test_purchase_columns(self):
        from src.managers.shared.file_utils import infer_behavior_data_type

        assert (
            infer_behavior_data_type(["user_id", "purchase_date", "amount"])
            == "購買履歴"
        )

    def test_web_access_columns(self):
        from src.managers.shared.file_utils import infer_behavior_data_type

        assert (
            infer_behavior_data_type(["user_id", "page_url", "timestamp"])
            == "Web行動ログ"
        )

    def test_inquiry_columns(self):
        from src.managers.shared.file_utils import infer_behavior_data_type

        assert (
            infer_behavior_data_type(["customer_id", "inquiry_date", "content"])
            == "問い合わせ履歴"
        )

    def test_unknown_columns(self):
        from src.managers.shared.file_utils import infer_behavior_data_type

        assert infer_behavior_data_type(["col_a", "col_b", "col_c"]) == ""


class TestDetectBindingKey:
    """detect_binding_key のテスト"""

    def test_detects_user_id(self):
        from src.managers.shared.file_utils import detect_binding_key

        csv_bytes = b"user_id,name,amount\nU12345,test,1000\nU12345,test2,2000\n"
        col, val = detect_binding_key(["user_id", "name", "amount"], csv_bytes)
        assert col == "user_id"
        assert val == "U12345"

    def test_detects_customer_id(self):
        from src.managers.shared.file_utils import detect_binding_key

        csv_bytes = b"customer_id,product,qty\nC999,item1,5\n"
        col, val = detect_binding_key(["customer_id", "product", "qty"], csv_bytes)
        assert col == "customer_id"
        assert val == "C999"

    def test_no_id_column(self):
        from src.managers.shared.file_utils import detect_binding_key

        csv_bytes = b"name,score\nAlice,100\n"
        col, val = detect_binding_key(["name", "score"], csv_bytes)
        assert col == ""
        assert val == ""


class TestBuildBehaviorDatasetCandidates:
    """_build_behavior_dataset_candidates のテスト"""

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_builds_candidates_from_csv_urls(self, mock_dm_cls):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        csv_content = b"user_id,purchase_date,amount\nU001,2024-01-01,5000\nU001,2024-01-15,3000\n"

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.return_value = csv_content

        mgr = PersonaGenerationManager(agent_service=Mock(), database_service=Mock())
        candidates = mgr._build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/file.csv"],
            persona_name="田中太郎",
            thinking_log=[],
            csv_url_labels=[],
        )

        assert len(candidates) == 1
        assert candidates[0]["name"] == "田中太郎_購買履歴"
        assert candidates[0]["row_count"] == 2
        assert candidates[0]["binding_key_column"] == "user_id"
        assert candidates[0]["binding_key_value"] == "U001"

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_skips_empty_csv(self, mock_dm_cls):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.return_value = b"col1\n"

        mgr = PersonaGenerationManager(agent_service=Mock(), database_service=Mock())
        candidates = mgr._build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/empty.csv"],
            persona_name="テスト",
            thinking_log=[],
            csv_url_labels=[],
        )

        assert len(candidates) == 0

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_handles_download_error_gracefully(self, mock_dm_cls):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.side_effect = Exception("Download failed")

        mgr = PersonaGenerationManager(agent_service=Mock(), database_service=Mock())
        candidates = mgr._build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/fail.csv"],
            persona_name="テスト",
            thinking_log=[],
            csv_url_labels=[],
        )

        assert len(candidates) == 0


class TestExtractUserIdFromLog:
    """_extract_user_id_from_log のテスト"""

    def _make_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        return PersonaGenerationManager(agent_service=Mock(), database_service=Mock())

    def test_extracts_customer_id_from_thinking_log(self):
        mgr = self._make_manager()
        log = [
            {
                "type": "tool_result",
                "content": "customer_id='920ef9bc-fd7e-405c-927c-6c0911b530c6' の注文データ",
            },
        ]
        col, val = mgr._extract_user_id_from_log(log)
        assert col == "customer_id"
        assert val == "920ef9bc-fd7e-405c-927c-6c0911b530c6"

    def test_extracts_user_id(self):
        mgr = self._make_manager()
        log = [
            {"type": "thinking", "content": "user_id=U12345 のデータを取得します"},
        ]
        col, val = mgr._extract_user_id_from_log(log)
        assert col == "user_id"
        assert val == "U12345"

    def test_returns_empty_when_no_id_found(self):
        mgr = self._make_manager()
        log = [{"type": "thinking", "content": "テーブル一覧を確認します"}]
        col, val = mgr._extract_user_id_from_log(log)
        assert col == ""
        assert val == ""

    def test_extracts_from_detail_field(self):
        mgr = self._make_manager()
        log = [
            {
                "type": "tool_call",
                "content": "問い合わせ中",
                "detail": "customer_id = abc-123 の購買データ",
            },
        ]
        col, val = mgr._extract_user_id_from_log(log)
        assert col == "customer_id"
        assert val == "abc-123"


class TestExtractLabelFromToolCall:
    """_extract_label_from_tool_call のテスト"""

    def _make_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        return PersonaGenerationManager(agent_service=Mock(), database_service=Mock())

    def test_extracts_label_from_csv_export_request(self):
        mgr = self._make_manager()
        detail = "customer_id='920ef9bc' の注文履歴をCSVで出力してください"
        assert mgr._extract_label_from_tool_call(detail) == "注文履歴"

    def test_extracts_label_with_join_description(self):
        mgr = self._make_manager()
        detail = "customer_id='ABC' の購買履歴をCSVで出力してください。ordersとorder_itemsをJOINして"
        assert mgr._extract_label_from_tool_call(detail) == "購買履歴"

    def test_extracts_label_page_views(self):
        mgr = self._make_manager()
        detail = "customer_id='ABC' のページ閲覧履歴をCSVで出力してください"
        assert mgr._extract_label_from_tool_call(detail) == "ページ閲覧履歴"

    def test_extracts_label_support_tickets(self):
        mgr = self._make_manager()
        detail = "customer_id='XYZ' のサポートチケット履歴をCSVで出力してください"
        assert mgr._extract_label_from_tool_call(detail) == "サポートチケット履歴"

    def test_returns_empty_for_non_csv_request(self):
        mgr = self._make_manager()
        detail = "利用可能なテーブル一覧を教えてください"
        assert mgr._extract_label_from_tool_call(detail) == ""

    def test_truncates_long_label(self):
        mgr = self._make_manager()
        detail = "customer_id='ABC' の注文明細と商品情報を結合した購買明細データをCSVで出力してください"
        label = mgr._extract_label_from_tool_call(detail)
        assert len(label) <= 20
