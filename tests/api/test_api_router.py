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
        mock_manager.get_all_personas_full.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/personas")

        assert response.status_code == 200
        assert response.json() == []

    @patch("web.routers.api.get_persona_manager")
    def test_list_personas_with_data(self, mock_get_manager, client, sample_persona):
        """ペルソナが存在する場合、正しいデータを返す"""
        mock_manager = Mock()
        mock_manager.get_all_personas_full.return_value = [sample_persona]
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
        mock_manager.get_all_personas_full.return_value = [
            sample_persona,
            sample_persona_2,
        ]
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
        mock_manager.get_all_personas_full.return_value = [
            sample_persona,
            sample_persona_2,
        ]
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
        mock_manager.get_all_personas_full.side_effect = Exception("Database error")
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
        mock_manager.get_discussion_history.return_value = ([], None)
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
        mock_manager.get_discussion_history.return_value = ([sample_discussion], None)
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


class TestDiscussionDetailEndpoint:
    """議論詳細APIエンドポイントのテスト"""

    @patch("web.routers.api.get_discussion_manager")
    def test_get_discussion_success(self, mock_get_manager, client, sample_discussion):
        """議論詳細を正常に取得"""
        from src.models.message import Message
        from src.models.insight import Insight

        msg = Message.create_new("p1", "田中", "テストメッセージ")
        sample_discussion = sample_discussion.add_message(msg)
        sample_discussion.insights = [
            Insight.create_new("テスト", "テストインサイト説明", [], 0.8)
        ]

        mock_manager = Mock()
        mock_manager.get_discussion.return_value = sample_discussion
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/api/discussions/{sample_discussion.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_discussion.id
        assert data["topic"] == "新商品のマーケティング戦略について"
        assert len(data["messages"]) == 1
        assert len(data["insights"]) == 1

    @patch("web.routers.api.get_discussion_manager")
    def test_get_discussion_not_found(self, mock_get_manager, client):
        """存在しない議論IDで404"""
        mock_manager = Mock()
        mock_manager.get_discussion.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/discussions/non-existent")

        assert response.status_code == 404
        assert "議論が見つかりません" in response.json()["detail"]

    @patch("web.routers.api.get_discussion_manager")
    def test_get_discussion_error(self, mock_get_manager, client):
        """エラー発生時に500"""
        mock_manager = Mock()
        mock_manager.get_discussion.side_effect = Exception("DB error")
        mock_get_manager.return_value = mock_manager

        response = client.get("/api/discussions/test-id")

        assert response.status_code == 500


class TestJobEndpoint:
    """ジョブステータスAPIのテスト"""

    @patch("web.routers.api.get_job_manager")
    def test_get_job_not_found(self, mock_get_jm, client):
        """存在しないジョブIDで404"""
        mock_jm = Mock()
        mock_jm.get.return_value = None
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/non-existent")

        assert response.status_code == 404
        assert "ジョブが見つかりません" in response.json()["detail"]

    @patch("web.routers.api.get_job_manager")
    def test_get_job_completed(self, mock_get_jm, client):
        """完了ジョブの取得"""
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-1",
            status=JobStatus.COMPLETED,
            created_at=Mock(),
            result={"data": "test"},
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/job-1")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-1"
        assert data["status"] == "completed"
        assert data["result"] == {"data": "test"}
        assert data["error"] is None

    @patch("web.routers.api.get_job_manager")
    def test_get_job_failed(self, mock_get_jm, client):
        """失敗ジョブの取得"""
        from src.models.job import Job, JobStatus

        job = Job(
            id="job-2",
            status=JobStatus.FAILED,
            created_at=Mock(),
            error="something broke",
        )
        mock_jm = Mock()
        mock_jm.get.return_value = job
        mock_get_jm.return_value = mock_jm

        response = client.get("/api/jobs/job-2")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "処理中にエラーが発生しました"


class TestGeneratePersonasEndpoint:
    """ペルソナ生成APIのテスト"""

    @patch("web.routers.api.get_job_manager")
    def test_generate_personas_submits_job(self, mock_get_jm, client):
        """ペルソナ生成が202を返す"""
        mock_jm = Mock()
        mock_jm.submit.return_value = "job-123"
        mock_get_jm.return_value = mock_jm

        response = client.post(
            "/api/personas/generate",
            json={
                "data_type": "interview",
                "file_contents": ["テストインタビューデータ"],
                "count": 3,
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-123"
        assert data["status"] == "pending"

    @patch("web.routers.api.get_job_manager")
    def test_generate_personas_error(self, mock_get_jm, client):
        """ペルソナ生成ジョブ投入エラー"""
        mock_jm = Mock()
        mock_jm.submit.side_effect = Exception("job error")
        mock_get_jm.return_value = mock_jm

        response = client.post(
            "/api/personas/generate",
            json={"data_type": "interview", "file_contents": ["data"], "count": 1},
        )

        assert response.status_code == 500


class TestRunDiscussionEndpoint:
    """議論実行APIのテスト"""

    @patch("web.routers.api.get_job_manager")
    @patch("web.routers.api.get_persona_manager")
    def test_run_discussion_submits_job(
        self, mock_get_pm, mock_get_jm, client, sample_persona, sample_persona_2
    ):
        """議論実行が202を返す"""
        mock_pm = Mock()
        mock_pm.get_persona.side_effect = [sample_persona, sample_persona_2]
        mock_get_pm.return_value = mock_pm

        mock_jm = Mock()
        mock_jm.submit.return_value = "job-456"
        mock_get_jm.return_value = mock_jm

        response = client.post(
            "/api/discussions",
            json={
                "persona_ids": [sample_persona.id, sample_persona_2.id],
                "topic": "テスト議論トピック",
                "mode": "classic",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-456"
        assert data["status"] == "pending"

    @patch("web.routers.api.get_persona_manager")
    def test_run_discussion_persona_not_found(self, mock_get_pm, client):
        """存在しないペルソナIDで404"""
        mock_pm = Mock()
        mock_pm.get_persona.return_value = None
        mock_get_pm.return_value = mock_pm

        response = client.post(
            "/api/discussions",
            json={
                "persona_ids": ["bad-id-1", "bad-id-2"],
                "topic": "テスト議論",
                "mode": "classic",
            },
        )

        assert response.status_code == 404
