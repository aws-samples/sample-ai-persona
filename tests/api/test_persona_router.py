"""
ペルソナルーター（persona.py）のテスト

ファイルアップロード、ペルソナ生成・保存・編集・削除エンドポイントをテストします。
"""

from unittest.mock import Mock, patch
from io import BytesIO

from src.managers.file_manager import FileUploadError, FileSecurityError, FileMetadata


class TestPersonaGenerationPage:
    """ペルソナ生成ページのテスト"""

    def test_generation_page_loads(self, client):
        """ペルソナ生成ページが正常に読み込まれることを確認"""
        response = client.get("/persona/generation")

        assert response.status_code == 200
        assert "AIペルソナ生成" in response.text or "ペルソナ" in response.text


class TestPersonaManagementPage:
    """ペルソナ管理ページのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_management_page_loads(self, mock_get_manager, client):
        """ペルソナ管理ページが正常に読み込まれることを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/management")

        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_management_page_with_personas(
        self, mock_get_manager, client, sample_persona
    ):
        """ペルソナ管理ページが正常に読み込まれることを確認（一覧は htmx 遅延ロード）"""
        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/management")

        assert response.status_code == 200
        # ペルソナ一覧は htmx で遅延ロードされるため、初期HTMLには含まれない
        assert "hx-get" in response.text


class TestFileUploadEndpoint:
    """ファイルアップロードエンドポイントのテスト"""

    @patch("web.routers.persona.get_file_manager")
    def test_upload_success(self, mock_get_manager, client):
        """ファイルアップロードが成功することを確認"""
        mock_manager = Mock()
        mock_metadata = FileMetadata(
            file_id="test-file-id",
            original_filename="interview.txt",
            saved_filename="uuid_interview.txt",
            file_path="/uploads/uuid_interview.txt",
            file_size=1024,
            file_hash="abc123",
            mime_type="text/plain",
            uploaded_at=None,
        )
        mock_manager.upload_interview_file.return_value = (
            "/uploads/uuid_interview.txt",
            "インタビュー内容のテキスト",
            mock_metadata,
        )
        mock_get_manager.return_value = mock_manager

        file_content = "これはテスト用のインタビューファイルです。十分な長さのテキストを含んでいます。"
        files = {
            "file": (
                "interview.txt",
                BytesIO(file_content.encode("utf-8")),
                "text/plain",
            )
        }

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 200

    @patch("web.routers.persona.get_file_manager")
    def test_upload_invalid_extension(self, mock_get_manager, client):
        """無効なファイル拡張子でエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.upload_interview_file.side_effect = FileUploadError(
            "許可されていないファイル形式です"
        )
        mock_get_manager.return_value = mock_manager

        files = {"file": ("interview.pdf", BytesIO(b"test content"), "application/pdf")}

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 400
        assert "アップロードエラー" in response.text

    @patch("web.routers.persona.get_file_manager")
    def test_upload_security_error(self, mock_get_manager, client):
        """セキュリティエラーが適切に処理されることを確認"""
        mock_manager = Mock()
        mock_manager.upload_interview_file.side_effect = FileSecurityError(
            "ファイル名に不正な文字が含まれています"
        )
        mock_get_manager.return_value = mock_manager

        files = {"file": ("../../../etc/passwd", BytesIO(b"test"), "text/plain")}

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 400
        assert "セキュリティエラー" in response.text


class TestPersonaGenerateEndpoint:
    """ペルソナ生成エンドポイントのテスト"""

    @patch("web.routers.persona._temp_personas_cache", {})
    @patch("web.routers.persona.get_persona_manager")
    def test_generate_success(self, mock_get_manager, client, sample_persona):
        """ペルソナ生成が成功することを確認（SSE）"""
        mock_manager = Mock()
        mock_manager.generate_personas.return_value = ([sample_persona], [])
        mock_get_manager.return_value = mock_manager

        file_content = "十分な長さのインタビューテキスト。これはテスト用のテキストです。" * 5
        files = [
            ("files", ("interview.txt", BytesIO(file_content.encode("utf-8")), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert "event: result" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_generate_empty_text(self, mock_get_manager, client):
        """空のファイルでエラーを返すことを確認（SSE）"""
        files = [
            ("files", ("empty.txt", BytesIO(b""), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_generate_invalid_count(self, mock_get_manager, client):
        """無効なペルソナ数でエラーを返すことを確認（SSE）"""
        file_content = "テスト用テキスト。" * 10
        files = [
            ("files", ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 0,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text

    @patch("web.routers.persona._temp_personas_cache", {})
    @patch("web.routers.persona.get_persona_manager")
    def test_generate_multiple_success(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """複数ペルソナ生成が成功することを確認（SSE）"""
        mock_manager = Mock()
        mock_manager.generate_personas.return_value = (
            [sample_persona, sample_persona_2],
            [{"type": "thinking", "content": "分析中..."}],
        )
        mock_get_manager.return_value = mock_manager

        file_content = "これは市場調査レポートです。" * 50
        files = [
            ("files", ("report.txt", BytesIO(file_content.encode("utf-8")), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "market_report",
                "persona_count": 2,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: result" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_generate_manager_error(self, mock_get_manager, client):
        """PersonaManagerErrorが適切に処理されることを確認（SSE）"""
        from src.managers.persona_manager import PersonaManagerError

        mock_manager = Mock()
        mock_manager.generate_personas.side_effect = PersonaManagerError(
            "生成エラー"
        )
        mock_get_manager.return_value = mock_manager

        file_content = "テスト用テキスト。" * 10
        files = [
            ("files", ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain")),
        ]

        response = client.post(
            "/persona/generate",
            files=files,
            data={
                "data_type": "interview",
                "persona_count": 1,
                "data_description": "",
                "custom_prompt": "",
            },
        )

        assert response.status_code == 200
        assert "event: error" in response.text


class TestPersonaSaveEndpoint:
    """ペルソナ保存エンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_save_success(self, mock_get_manager, client):
        """ペルソナ保存が成功することを確認"""
        mock_manager = Mock()
        mock_manager.save_persona.return_value = "new-persona-id"
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save",
            data={
                "persona_id": "test-id",
                "name": "テストペルソナ",
                "age": 30,
                "occupation": "エンジニア",
                "background": "テスト背景",
                "values": "価値観1\n価値観2",
                "pain_points": "課題1\n課題2",
                "goals": "目標1\n目標2",
            },
        )

        assert response.status_code == 200
        assert "保存しました" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_save_error(self, mock_get_manager, client):
        """保存エラーが適切に処理されることを確認"""
        mock_manager = Mock()
        mock_manager.save_persona.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save",
            data={
                "persona_id": "test-id",
                "name": "テストペルソナ",
                "age": 30,
                "occupation": "エンジニア",
                "background": "テスト背景",
                "values": "価値観1",
                "pain_points": "課題1",
                "goals": "目標1",
            },
        )

        assert response.status_code == 500
        assert "保存エラー" in response.text


class TestPersonaDetailEndpoint:
    """ペルソナ詳細エンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_get_detail_success(self, mock_get_manager, client, sample_persona):
        """ペルソナ詳細ページが正常に表示されることを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = sample_persona
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/persona/{sample_persona.id}")

        assert response.status_code == 200
        assert "田中花子" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_get_detail_not_found(self, mock_get_manager, client):
        """存在しないペルソナで404エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/non-existent-id")

        assert response.status_code == 404


class TestPersonaUpdateEndpoint:
    """ペルソナ更新エンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_success(self, mock_get_manager, client, sample_persona):
        """ペルソナ更新が成功することを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = sample_persona
        updated_persona = sample_persona.update(name="更新された名前")
        mock_manager.edit_persona.return_value = updated_persona
        mock_get_manager.return_value = mock_manager

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "更新された名前",
                "age": 36,
                "occupation": "シニアマーケター",
                "background": "更新された背景",
                "values": "新しい価値観",
                "pain_points": "新しい課題",
                "goals": "新しい目標",
            },
        )

        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_update_not_found(self, mock_get_manager, client):
        """存在しないペルソナの更新で404エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.put(
            "/persona/non-existent-id",
            data={
                "name": "テスト",
                "age": 30,
                "occupation": "テスト",
                "background": "テスト",
                "values": "テスト",
                "pain_points": "テスト",
                "goals": "テスト",
            },
        )

        assert response.status_code == 404


class TestPersonaDeleteEndpoint:
    """ペルソナ削除エンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_delete_success(self, mock_get_manager, client):
        """ペルソナ削除が成功することを確認"""
        mock_manager = Mock()
        mock_manager.delete_persona.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete("/persona/test-id")

        assert response.status_code == 200
        assert "削除しました" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_delete_failure(self, mock_get_manager, client):
        """削除失敗時にエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.delete_persona.return_value = False
        mock_get_manager.return_value = mock_manager

        response = client.delete("/persona/non-existent-id")

        assert response.status_code == 400
        assert "削除に失敗しました" in response.text


class TestPersonaListPartialEndpoint:
    """ペルソナ一覧パーシャルエンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_list_partial_success(self, mock_get_manager, client, sample_persona):
        """ペルソナ一覧パーシャルが正常に返されることを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = ([sample_persona], None)
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial")

        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_list_partial_with_search(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """検索フィルタが機能することを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = ([sample_persona, sample_persona_2], None)
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial?search=田中")

        assert response.status_code == 200


class TestSaveSelectedPersonasEndpoint:
    """選択ペルソナ保存エンドポイントのテスト"""

    @patch("web.routers.persona._temp_personas_cache")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_success(
        self, mock_get_manager, mock_cache, client, sample_persona
    ):
        """選択ペルソナ保存が成功することを確認"""
        mock_cache.get.return_value = sample_persona
        mock_cache.pop.return_value = sample_persona

        mock_manager = Mock()
        mock_manager.save_persona.return_value = sample_persona.id
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": sample_persona.id}
        )

        assert response.status_code == 200

    @patch("web.routers.persona._temp_personas_cache")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_empty_ids(self, mock_get_manager, mock_cache, client):
        """空のIDリストでエラーを返すことを確認"""
        response = client.post("/persona/save-selected", data={"persona_ids": ""})

        # FastAPIは空文字列を422で拒否する場合がある
        assert response.status_code in [400, 422]

    @patch("web.routers.persona._temp_personas_cache")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_not_found_in_cache(
        self, mock_get_manager, mock_cache, client
    ):
        """キャッシュにないペルソナでエラーを返すことを確認"""
        mock_cache.get.return_value = None
        mock_cache.pop.return_value = None

        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": "non-existent-id"}
        )

        assert response.status_code == 500
        assert "保存に失敗しました" in response.text
