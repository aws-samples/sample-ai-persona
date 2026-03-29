"""
REST API ルーター（api.py）のテスト

/api/personas, /api/discussions, /api/health エンドポイントをテストします。
"""

from unittest.mock import Mock, patch


class TestHealthEndpoint:
    """ヘルスチェックエンドポイントのテスト"""

    def test_health_check_returns_ok(self, client):
        """ヘルスチェックが正常に応答することを確認"""
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestPersonasAPIEndpoint:
    """ペルソナ一覧APIエンドポイントのテスト"""

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_empty(self, mock_get_manager, client):
        """ペルソナが存在しない場合、空のリストを返す"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        assert response.json() == []

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_with_data(self, mock_get_manager, client, sample_persona):
        """ペルソナが存在する場合、正しいデータを返す"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = [sample_persona]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "田中花子"
        assert data[0]["age"] == 35
        assert data[0]["occupation"] == "マーケティング担当"
        assert "values" in data[0]
        assert "pain_points" in data[0]
        assert "goals" in data[0]

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_with_search(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """検索パラメータでフィルタリングできることを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = [sample_persona, sample_persona_2]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas?search=田中")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "田中花子"

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_search_by_occupation(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """職業での検索が機能することを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = [sample_persona, sample_persona_2]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas?search=商品開発")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "佐藤太郎"

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_error_handling(self, mock_get_manager, client):
        """エラー発生時に500エラーを返す"""
        mock_manager = Mock()
        mock_manager.get_all_personas.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 500
        assert "ペルソナ一覧の取得に失敗しました" in response.json()["detail"]


class TestPersonaDetailAPIEndpoint:
    """ペルソナ詳細APIエンドポイントのテスト"""

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_success(self, mock_get_manager, client, sample_persona):
        """ペルソナ詳細を正常に取得できることを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = sample_persona
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/api/personas/{sample_persona.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_persona.id
        assert data["name"] == "田中花子"
        assert data["age"] == 35

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_not_found(self, mock_get_manager, client):
        """存在しないペルソナIDで404エラーを返す"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas/non-existent-id")

        assert response.status_code == 404
        assert "ペルソナが見つかりません" in response.json()["detail"]

    @patch("web.routers.api.get_persona_manager")
    def test_get_persona_error_handling(self, mock_get_manager, client):
        """エラー発生時に500エラーを返す"""
        mock_manager = Mock()
        mock_manager.get_persona.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas/test-id")

        assert response.status_code == 500
        assert "ペルソナの取得に失敗しました" in response.json()["detail"]


class TestDiscussionsAPIEndpoint:
    """議論一覧APIエンドポイントのテスト"""

    @patch("web.routers.api.get_discussion_manager")
    def test_list_discussions_empty(self, mock_get_manager, client):
        """議論が存在しない場合、空のリストを返す"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/discussions")

        assert response.status_code == 200
        assert response.json() == []

    @patch("web.routers.api.get_discussion_manager")
    def test_list_discussions_with_data(
        self, mock_get_manager, client, sample_discussion
    ):
        """議論が存在する場合、正しいデータを返す"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = [sample_discussion]
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/discussions")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == sample_discussion.id
        assert data[0]["topic"] == "新商品のマーケティング戦略について"
        assert "mode" in data[0]
        assert "created_at" in data[0]

    @patch("web.routers.api.get_discussion_manager")
    def test_list_discussions_error_handling(self, mock_get_manager, client):
        """エラー発生時に500エラーを返す"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/discussions")

        assert response.status_code == 500
        assert "議論一覧の取得に失敗しました" in response.json()["detail"]
