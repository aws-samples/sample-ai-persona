"""
データセットプレビューエンドポイントのAPIテスト

U5: プレビューは PersonaManager が判定してから出す（BR2.6）。URL のパラメータは
紐付けの ID からデータセットの ID に変わった。
"""

from unittest.mock import patch

from src.managers.persona_manager import PersonaManagerError
from src.models.errors import CodedError, ErrorCode

URL = "/persona/persona-001/dataset-bindings/ds-001/preview"


class TestDatasetPreviewEndpoint:
    """GET /persona/{persona_id}/dataset-bindings/{dataset_id}/preview"""

    @patch("web.routers.persona.get_persona_manager")
    def test_preview_success(self, mock_get_manager, client):
        """プレビューが正常に表示される"""
        mock_get_manager.return_value.preview_binding_data.return_value = {
            "columns": ["user_id", "product", "amount"],
            "rows": [["U123", "商品A", 1000]],
            "total_count": 1,
        }

        response = client.get(URL)

        assert response.status_code == 200
        assert "user_id" in response.text
        assert "商品A" in response.text
        assert "1件中" in response.text
        principal, persona_id, dataset_id = (
            mock_get_manager.return_value.preview_binding_data.call_args.args
        )
        assert principal.sub == "local:dev@example.com"
        assert (persona_id, dataset_id) == ("persona-001", "ds-001")

    @patch("web.routers.persona.get_persona_manager")
    def test_preview_error(self, mock_get_manager, client):
        """エラー時にエラーメッセージが表示される"""
        mock_get_manager.return_value.preview_binding_data.side_effect = (
            PersonaManagerError(
                "binding not found", code=ErrorCode.DATASET_BINDING_NOT_FOUND
            )
        )

        response = client.get(URL)

        assert response.status_code == 200
        assert "紐付け情報が見つかりません" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_unexpected_error_shows_the_generic_message(self, mock_get_manager, client):
        mock_get_manager.return_value.preview_binding_data.side_effect = ValueError(
            "s3://secret"
        )

        response = client.get(URL)

        assert response.status_code == 200
        assert "データの取得に失敗しました" in response.text
        assert "s3://secret" not in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_unreadable_persona_is_not_found(self, mock_get_manager, client):
        mock_get_manager.return_value.preview_binding_data.side_effect = CodedError(
            "not readable", code=ErrorCode.PERSONA_NOT_FOUND
        )

        response = client.get(URL)

        assert response.status_code == 404

    @patch("web.routers.persona.get_persona_manager")
    def test_preview_empty_data(self, mock_get_manager, client):
        """データが0件の場合にメッセージが表示される"""
        mock_get_manager.return_value.preview_binding_data.return_value = {
            "columns": ["user_id"],
            "rows": [],
            "total_count": 0,
        }

        response = client.get(URL)

        assert response.status_code == 200
        assert "該当するデータがありません" in response.text

    def test_preview_no_longer_uses_dataset_manager(self):
        import inspect

        from web.routers import persona

        assert "DatasetManager" not in inspect.getsource(persona)
