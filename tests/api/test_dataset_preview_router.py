"""
データセットプレビューエンドポイントのAPIテスト
"""

from unittest.mock import patch, Mock


class TestDatasetPreviewEndpoint:
    """GET /persona/{persona_id}/dataset-bindings/{binding_id}/preview"""

    def test_preview_success(self, client):
        """プレビューが正常に表示される"""
        mock_mgr = Mock()
        mock_mgr.preview_binding_data.return_value = {
            "columns": ["user_id", "product", "amount"],
            "rows": [["U123", "商品A", 1000]],
            "total_count": 1,
        }

        with patch(
            "src.managers.dataset_manager.DatasetManager", return_value=mock_mgr
        ):
            response = client.get(
                "/persona/persona-001/dataset-bindings/bind-001/preview"
            )

        assert response.status_code == 200
        assert "user_id" in response.text
        assert "商品A" in response.text
        assert "1件中" in response.text

    def test_preview_error(self, client):
        """エラー時にエラーメッセージが表示される"""
        mock_mgr = Mock()
        mock_mgr.preview_binding_data.side_effect = ValueError("Binding not found")

        with patch(
            "src.managers.dataset_manager.DatasetManager", return_value=mock_mgr
        ):
            response = client.get(
                "/persona/persona-001/dataset-bindings/nonexistent/preview"
            )

        assert response.status_code == 200
        assert "データの取得に失敗しました" in response.text

    def test_preview_empty_data(self, client):
        """データが0件の場合にメッセージが表示される"""
        mock_mgr = Mock()
        mock_mgr.preview_binding_data.return_value = {
            "columns": ["user_id"],
            "rows": [],
            "total_count": 0,
        }

        with patch(
            "src.managers.dataset_manager.DatasetManager", return_value=mock_mgr
        ):
            response = client.get(
                "/persona/persona-001/dataset-bindings/bind-001/preview"
            )

        assert response.status_code == 200
        assert "該当するデータがありません" in response.text
