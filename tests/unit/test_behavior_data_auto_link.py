"""行動データ自動紐付け機能のユニットテスト"""

import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager
from src.models.dataset import DatasetColumn


@pytest.fixture
def manager():
    return PersonaManager(ai_service=Mock(), database_service=Mock())


class TestAutoLinkBehaviorFlag:
    """auto_link_behavior フラグのバリデーション"""

    def test_auto_link_forces_persona_count_to_one(self, manager):
        """auto_link_behavior=True の場合、persona_count が 1 に強制される"""
        with patch("src.services.agent_service.AgentService") as mock_agent_cls:
            mock_agent = mock_agent_cls.return_value
            mock_persona = Mock()
            mock_persona.name = "テスト太郎"
            mock_persona.age = 30
            mock_persona.occupation = "エンジニア"
            mock_persona.background = "背景"
            mock_persona.values = ["v1"]
            mock_persona.pain_points = ["p1"]
            mock_persona.goals = ["g1"]
            mock_persona.gender = None
            mock_persona.country = None
            mock_persona.city = None
            mock_persona.tags = []
            mock_agent.generate_personas_with_agent.return_value = (
                [mock_persona],
                [],
            )

            personas, _ = manager.generate_personas(
                file_contents=[],
                data_type="dwh",
                persona_count=5,
                data_description="テスト条件",
                auto_link_behavior=True,
            )

            call_kwargs = mock_agent.generate_personas_with_agent.call_args[1]
            assert call_kwargs["persona_count"] == 1

    def test_auto_link_appends_behavior_extraction_instruction(self, manager):
        """auto_link_behavior=True の場合、行動データ抽出指示がdata_textに追記される"""
        with patch("src.services.agent_service.AgentService") as mock_agent_cls:
            mock_agent = mock_agent_cls.return_value
            mock_persona = Mock()
            mock_persona.name = "テスト"
            mock_persona.age = 25
            mock_persona.occupation = "営業"
            mock_persona.background = "bg"
            mock_persona.values = ["v"]
            mock_persona.pain_points = ["p"]
            mock_persona.goals = ["g"]
            mock_persona.gender = None
            mock_persona.country = None
            mock_persona.city = None
            mock_persona.tags = []
            mock_agent.generate_personas_with_agent.return_value = (
                [mock_persona],
                [],
            )

            manager.generate_personas(
                file_contents=[],
                data_type="dwh",
                persona_count=1,
                data_description="リピーター層",
                auto_link_behavior=True,
            )

            call_kwargs = mock_agent.generate_personas_with_agent.call_args[1]
            data_text = call_kwargs["data_text"]
            assert "特定1名の深掘り分析" in data_text
            assert "行動データCSVエクスポート" in data_text
            assert "CSVで出力してください" in data_text

    def test_without_auto_link_no_behavior_instruction(self, manager):
        """auto_link_behavior=False の場合、行動データ抽出指示は追記されない"""
        with patch("src.services.agent_service.AgentService") as mock_agent_cls:
            mock_agent = mock_agent_cls.return_value
            mock_persona = Mock()
            mock_persona.name = "テスト"
            mock_persona.age = 25
            mock_persona.occupation = "営業"
            mock_persona.background = "bg"
            mock_persona.values = ["v"]
            mock_persona.pain_points = ["p"]
            mock_persona.goals = ["g"]
            mock_persona.gender = None
            mock_persona.country = None
            mock_persona.city = None
            mock_persona.tags = []
            mock_agent.generate_personas_with_agent.return_value = (
                [mock_persona],
                [],
            )

            manager.generate_personas(
                file_contents=[],
                data_type="dwh",
                persona_count=3,
                data_description="新規顧客",
                auto_link_behavior=False,
            )

            call_kwargs = mock_agent.generate_personas_with_agent.call_args[1]
            data_text = call_kwargs["data_text"]
            assert "行動データ抽出" not in data_text


class TestInferBehaviorDataType:
    """_infer_behavior_data_type のテスト"""

    def test_purchase_columns(self):
        from web.routers.persona import _infer_behavior_data_type

        assert (
            _infer_behavior_data_type(["user_id", "purchase_date", "amount"])
            == "購買履歴"
        )

    def test_web_access_columns(self):
        from web.routers.persona import _infer_behavior_data_type

        assert (
            _infer_behavior_data_type(["user_id", "page_url", "timestamp"])
            == "Web行動ログ"
        )

    def test_inquiry_columns(self):
        from web.routers.persona import _infer_behavior_data_type

        assert (
            _infer_behavior_data_type(["customer_id", "inquiry_date", "content"])
            == "問い合わせ履歴"
        )

    def test_unknown_columns(self):
        from web.routers.persona import _infer_behavior_data_type

        assert _infer_behavior_data_type(["col_a", "col_b", "col_c"]) == ""


class TestDetectBindingKey:
    """_detect_binding_key のテスト"""

    def test_detects_user_id(self):
        from web.routers.persona import _detect_binding_key

        csv_bytes = b"user_id,name,amount\nU12345,test,1000\nU12345,test2,2000\n"
        col, val = _detect_binding_key(["user_id", "name", "amount"], csv_bytes)
        assert col == "user_id"
        assert val == "U12345"

    def test_detects_customer_id(self):
        from web.routers.persona import _detect_binding_key

        csv_bytes = b"customer_id,product,qty\nC999,item1,5\n"
        col, val = _detect_binding_key(["customer_id", "product", "qty"], csv_bytes)
        assert col == "customer_id"
        assert val == "C999"

    def test_no_id_column(self):
        from web.routers.persona import _detect_binding_key

        csv_bytes = b"name,score\nAlice,100\n"
        col, val = _detect_binding_key(["name", "score"], csv_bytes)
        assert col == ""
        assert val == ""


class TestBuildBehaviorDatasetCandidates:
    """_build_behavior_dataset_candidates のテスト"""

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_builds_candidates_from_csv_urls(self, mock_dm_cls):
        from web.routers.persona import _build_behavior_dataset_candidates

        csv_content = b"user_id,purchase_date,amount\nU001,2024-01-01,5000\nU001,2024-01-15,3000\n"

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.return_value = csv_content
        mock_dm.analyze_schema.return_value = (
            [
                DatasetColumn(name="user_id", data_type="string"),
                DatasetColumn(name="purchase_date", data_type="date"),
                DatasetColumn(name="amount", data_type="integer"),
            ],
            2,
        )

        candidates = _build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/file.csv"],
            persona_name="田中太郎",
        )

        assert len(candidates) == 1
        assert candidates[0]["name"] == "田中太郎_購買履歴"
        assert candidates[0]["row_count"] == 2
        assert candidates[0]["binding_key_column"] == "user_id"
        assert candidates[0]["binding_key_value"] == "U001"

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_skips_empty_csv(self, mock_dm_cls):
        from web.routers.persona import _build_behavior_dataset_candidates

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.return_value = b"col1,col2\n"
        mock_dm.analyze_schema.return_value = (
            [DatasetColumn(name="col1", data_type="string")],
            0,
        )

        candidates = _build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/empty.csv"],
            persona_name="テスト",
        )

        assert len(candidates) == 0

    @patch("src.managers.dataset_manager.DatasetManager")
    def test_handles_download_error_gracefully(self, mock_dm_cls):
        from web.routers.persona import _build_behavior_dataset_candidates

        mock_dm = mock_dm_cls.return_value
        mock_dm.download_csv_from_url.side_effect = Exception("Download failed")

        candidates = _build_behavior_dataset_candidates(
            csv_urls=["https://s3.amazonaws.com/bucket/fail.csv"],
            persona_name="テスト",
        )

        assert len(candidates) == 0


class TestExtractUserIdFromLog:
    """_extract_user_id_from_log のテスト"""

    def test_extracts_customer_id_from_thinking_log(self):
        from web.routers.persona import _extract_user_id_from_log

        log = [
            {
                "type": "tool_result",
                "content": "customer_id='920ef9bc-fd7e-405c-927c-6c0911b530c6' の注文データ",
            },
        ]
        col, val = _extract_user_id_from_log(log)
        assert col == "customer_id"
        assert val == "920ef9bc-fd7e-405c-927c-6c0911b530c6"

    def test_extracts_user_id(self):
        from web.routers.persona import _extract_user_id_from_log

        log = [
            {"type": "thinking", "content": "user_id=U12345 のデータを取得します"},
        ]
        col, val = _extract_user_id_from_log(log)
        assert col == "user_id"
        assert val == "U12345"

    def test_returns_empty_when_no_id_found(self):
        from web.routers.persona import _extract_user_id_from_log

        log = [{"type": "thinking", "content": "テーブル一覧を確認します"}]
        col, val = _extract_user_id_from_log(log)
        assert col == ""
        assert val == ""

    def test_extracts_from_detail_field(self):
        from web.routers.persona import _extract_user_id_from_log

        log = [
            {
                "type": "tool_call",
                "content": "問い合わせ中",
                "detail": "customer_id = abc-123 の購買データ",
            },
        ]
        col, val = _extract_user_id_from_log(log)
        assert col == "customer_id"
        assert val == "abc-123"


class TestExtractLabelFromToolCall:
    """_extract_label_from_tool_call のテスト"""

    def test_extracts_label_from_csv_export_request(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "customer_id='920ef9bc' の注文履歴をCSVで出力してください"
        assert _extract_label_from_tool_call(detail) == "注文履歴"

    def test_extracts_label_with_join_description(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "customer_id='ABC' の購買履歴をCSVで出力してください。ordersとorder_itemsをJOINして"
        assert _extract_label_from_tool_call(detail) == "購買履歴"

    def test_extracts_label_page_views(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "customer_id='ABC' のページ閲覧履歴をCSVで出力してください"
        assert _extract_label_from_tool_call(detail) == "ページ閲覧履歴"

    def test_extracts_label_support_tickets(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "customer_id='XYZ' のサポートチケット履歴をCSVで出力してください"
        assert _extract_label_from_tool_call(detail) == "サポートチケット履歴"

    def test_returns_empty_for_non_csv_request(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "利用可能なテーブル一覧を教えてください"
        assert _extract_label_from_tool_call(detail) == ""

    def test_truncates_long_label(self):
        from web.routers.persona import _extract_label_from_tool_call

        detail = "customer_id='ABC' の注文明細と商品情報を結合した購買明細データをCSVで出力してください"
        label = _extract_label_from_tool_call(detail)
        assert len(label) <= 20
