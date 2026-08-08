"""
ペルソナルーター（persona.py）のテスト

ファイルアップロード、ペルソナ生成・保存・編集・削除エンドポイントをテストします。
"""

import json
from unittest.mock import Mock, patch
from io import BytesIO

import pytest

from src.managers.file_manager import FileUploadError, FileSecurityError, FileMetadata
from src.managers.persona_manager import PersonaManagerError
from src.models.errors import ErrorCode


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
            "extension of 'interview.pdf' not in allowed extensions",
            code=ErrorCode.FILE_FORMAT_NOT_ALLOWED,
            context={"allowed_formats": ".txt, .md"},
        )
        mock_get_manager.return_value = mock_manager

        files = {"file": ("interview.pdf", BytesIO(b"test content"), "application/pdf")}

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 400
        assert "許可されていないファイル形式" in response.text
        assert ".txt, .md" in response.text

    @patch("web.routers.persona.get_file_manager")
    def test_upload_security_error(self, mock_get_manager, client):
        """セキュリティエラーが適切に処理されることを確認"""
        mock_manager = Mock()
        mock_manager.upload_interview_file.side_effect = FileSecurityError(
            "filename contains a path traversal or invalid character",
            code=ErrorCode.FILE_NAME_INVALID,
        )
        mock_get_manager.return_value = mock_manager

        files = {"file": ("../../../etc/passwd", BytesIO(b"test"), "text/plain")}

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 400
        assert "ファイル名に不正な文字が含まれています" in response.text

    @patch("web.routers.persona.get_file_manager")
    def test_upload_does_not_expose_internal_detail(self, mock_get_manager, client):
        """内部例外の詳細がレスポンスに出ないことを確認（#112）"""
        mock_manager = Mock()
        mock_manager.upload_interview_file.side_effect = FileUploadError(
            "interview file upload failed (ClientError): "
            "arn:aws:s3:::internal-bucket/secret",
            code=ErrorCode.FILE_OPERATION_FAILED,
        )
        mock_get_manager.return_value = mock_manager

        files = {"file": ("interview.txt", BytesIO(b"test content"), "text/plain")}

        response = client.post("/persona/upload", files=files)

        assert response.status_code == 400
        assert "arn:aws:s3" not in response.text
        assert "ClientError" not in response.text


class TestPersonaGenerateEndpoint:
    """ペルソナ生成エンドポイントのテスト"""

    @pytest.fixture(autouse=True)
    def mock_file_manager(self):
        """generate_persona は get_file_manager() で FileManager を生成し、その
        コンストラクタが実 DynamoDB 接続を試みる。認証情報のない CI で失敗するため、
        FileManager をモックに差し替える。既定の validate_persona_source_file は
        None を返す（＝検証パス）ので、正常系はそのまま通る。拒否系テストは
        返り値の mock に side_effect を仕込んで使う。"""
        with patch("web.routers.persona.get_file_manager") as mock_get_fm:
            mock_fm = Mock()
            mock_fm.validate_persona_source_file.return_value = None
            mock_get_fm.return_value = mock_fm
            yield mock_fm

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_success(self, mock_get_gen_manager, client, sample_persona):
        """ペルソナ生成が成功することを確認（SSE）"""
        mock_manager = Mock()
        mock_manager.generate_and_cache.return_value = ([sample_persona], [])
        mock_get_gen_manager.return_value = mock_manager

        file_content = (
            "十分な長さのインタビューテキスト。これはテスト用のテキストです。" * 5
        )
        files = [
            (
                "files",
                ("interview.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
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

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_rejects_unsupported_extension(
        self, mock_get_gen_manager, client, mock_file_manager
    ):
        """非対応拡張子(.exe)は抽出前に弾かれ、生成マネージャは呼ばれない（SSE）"""
        mock_manager = Mock()
        mock_get_gen_manager.return_value = mock_manager
        # Router が呼ぶ検証で FileUploadError を送出させ、拒否経路を再現する
        mock_file_manager.validate_persona_source_file.side_effect = FileUploadError(
            "extension not allowed", code=ErrorCode.FILE_FORMAT_NOT_ALLOWED
        )

        files = [
            ("files", ("malware.exe", BytesIO(b"anything at all here"), "text/plain")),
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
        mock_manager.generate_and_cache.assert_not_called()

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
            (
                "files",
                ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
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

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_multiple_success(
        self, mock_get_gen_manager, client, sample_persona, sample_persona_2
    ):
        """複数ペルソナ生成が成功することを確認（SSE）"""
        mock_manager = Mock()
        mock_manager.generate_and_cache.return_value = (
            [sample_persona, sample_persona_2],
            [{"type": "thinking", "content": "分析中..."}],
        )
        mock_get_gen_manager.return_value = mock_manager

        file_content = "これは市場調査レポートです。" * 50
        files = [
            (
                "files",
                ("report.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
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

    @patch("web.routers.persona.get_persona_generation_manager")
    def test_generate_manager_error(self, mock_get_gen_manager, client):
        """PersonaGenerationManagerErrorが適切に処理されることを確認（SSE）"""
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        mock_manager = Mock()
        mock_manager.generate_and_cache.side_effect = PersonaGenerationManagerError(
            "生成エラー"
        )
        mock_get_gen_manager.return_value = mock_manager

        file_content = "テスト用テキスト。" * 10
        files = [
            (
                "files",
                ("test.txt", BytesIO(file_content.encode("utf-8")), "text/plain"),
            ),
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

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_success(self, mock_get_manager, mock_get_gen_manager, client):
        """ペルソナ保存が成功することを確認"""
        mock_manager = Mock()
        mock_manager.save_persona.return_value = "new-persona-id"
        mock_get_manager.return_value = mock_manager
        mock_gen_manager = Mock()
        mock_gen_manager.pop_cached_persona.return_value = None
        mock_gen_manager.pop_cached_behavior_datasets.return_value = None
        mock_get_gen_manager.return_value = mock_gen_manager

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

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_error(self, mock_get_manager, mock_get_gen_manager, client):
        """保存エラーがトーストで通知され、生成結果が消えないことを確認。

        再試行で解決しうるエラー（TRANSIENT）は本文を返さず HX-Trigger で
        通知する。本文でエラーパーシャルを返すと生成済みペルソナが画面から
        消えて保存し直せなくなるため（Issue #117）。
        """
        mock_manager = Mock()
        mock_manager.save_persona.side_effect = Exception("Database error")
        mock_get_manager.return_value = mock_manager
        mock_gen_manager = Mock()
        mock_gen_manager.pop_cached_persona.return_value = None
        mock_gen_manager.pop_cached_behavior_datasets.return_value = None
        mock_get_gen_manager.return_value = mock_gen_manager

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
        # 画面を書き換えないため本文は空
        assert response.text == ""
        trigger = json.loads(response.headers["HX-Trigger"])
        assert (
            "ペルソナの保存中にエラーが発生しました" in trigger["showToast"]["message"]
        )
        # 例外の内容がユーザーに漏れていないこと
        assert "Database error" not in response.headers["HX-Trigger"]


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
        mock_manager.update_persona.return_value = updated_persona
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
        """存在しないペルソナの更新で400エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.update_persona.return_value = None
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
        assert 'role="alert"' in response.text


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
        mock_manager.get_all_personas.return_value = (
            [sample_persona, sample_persona_2],
            None,
        )
        mock_get_manager.return_value = mock_manager

        response = client.get("/persona/list/partial?search=田中")

        assert response.status_code == 200


class TestSaveSelectedPersonasEndpoint:
    """選択ペルソナ保存エンドポイントのテスト"""

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_success(
        self, mock_get_manager, mock_get_gen_manager, client, sample_persona
    ):
        """選択ペルソナ保存が成功することを確認"""
        mock_gen_manager = Mock()
        mock_gen_manager.get_cached_persona.return_value = sample_persona
        mock_gen_manager.pop_cached_persona.return_value = sample_persona
        mock_get_gen_manager.return_value = mock_gen_manager

        mock_manager = Mock()
        mock_manager.save_persona.return_value = sample_persona.id
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": sample_persona.id}
        )

        assert response.status_code == 200

    def test_save_selected_empty_ids(self, client):
        """空のIDリストでエラーを返すことを確認"""
        response = client.post("/persona/save-selected", data={"persona_ids": ""})

        # FastAPIは空文字列を422で拒否する場合がある
        assert response.status_code in [400, 422]

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_not_found_in_cache(
        self, mock_get_manager, mock_get_gen_manager, client
    ):
        """キャッシュ期限切れで専用エラーメッセージを返すことを確認"""
        mock_gen_manager = Mock()
        mock_gen_manager.get_cached_persona.return_value = None
        mock_gen_manager.pop_cached_persona.return_value = None
        mock_get_gen_manager.return_value = mock_gen_manager

        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": "non-existent-id"}
        )

        assert response.status_code == 500
        assert "一時データが期限切れ" in response.text

    @patch("web.routers.persona.get_persona_generation_manager")
    @patch("web.routers.persona.get_persona_manager")
    def test_save_selected_partial_cache_miss(
        self, mock_get_manager, mock_get_gen_manager, client, sample_persona
    ):
        """一部キャッシュ切れでも保存成功分はカウントされることを確認"""
        mock_gen_manager = Mock()
        mock_gen_manager.get_cached_persona.side_effect = [sample_persona, None]
        mock_gen_manager.pop_cached_persona.return_value = None
        mock_get_gen_manager.return_value = mock_gen_manager

        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/persona/save-selected", data={"persona_ids": "id1,id2"}
        )

        assert response.status_code == 200
        mock_manager.save_persona.assert_called_once_with(sample_persona)


class TestPersonaEditForm:
    """ペルソナ編集フォームのテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_loads(self, mock_get_mgr, client, sample_persona):
        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_get_mgr.return_value = mock_mgr

        response = client.get(f"/persona/{sample_persona.id}/edit")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_not_found(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = None
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/nonexistent/edit")
        assert response.status_code == 404

    @patch("web.routers.persona.get_persona_manager")
    def test_edit_form_error(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_persona.side_effect = Exception("DB error")
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/test-id/edit")
        assert response.status_code == 500


class TestPersonaUpdateValidationPreservesInput:
    """バリデーション失敗時に送信値が失われないこと（Issue #117 ステップ4）。

    編集フォームの hx-target は詳細画面本体（#persona-detail-container）なので、
    汎用エラーパーシャルを返すとフォームごと消えて11項目の入力がやり直しに
    なっていた。送信値を埋め戻した編集フォームを返すことで解決する。
    """

    #: 利用者が入力した値。エラー応答にそのまま残っていることを検証する
    _SUBMITTED = {
        "name": "編集した名前",
        "age": "41",
        "occupation": "編集した職業",
        "background": "編集した背景テキスト",
        "values": "価値観A\n価値観B",
        "pain_points": "課題A\n課題B",
        "goals": "目標A\n目標B",
        "gender": "female",
        "country": "JP",
        "city": "編集した都市",
        "tags": "タグA\nタグB",
    }

    @staticmethod
    def _manager(sample_persona, exc):
        mgr = Mock()
        mgr.update_persona.side_effect = exc
        # 再描画のヘッダー部（名前・戻るリンク）に使う元データ
        mgr.get_persona.return_value = sample_persona
        return mgr

    @patch("web.routers.persona.get_persona_manager")
    def test_submitted_values_survive_validation_error(
        self, mock_get_mgr, client, sample_persona
    ):
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "occupation exceeds max length",
                code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                context={"field": "occupation", "max_length": 100},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.status_code == 400
        # htmx が本文をDOMに反映できる印が付いていること
        assert response.headers.get("X-Render-Response") == "true"
        # 入力した全項目が残っていること（トーストや汎用パーシャルなら消える）
        for field, value in self._SUBMITTED.items():
            first_line = value.split("\n")[0]
            assert first_line in response.text, f"{field} の入力が失われた"
        # 該当フィールドの文言が出ていること
        assert "職業は100文字以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_capacity_error_also_preserves_input(
        self, mock_get_mgr, client, sample_persona
    ):
        """CAPACITY（量の超過）も入力を直せば解決するので同じ扱いにする。"""
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "too many tags",
                code=ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS,
                context={"field": "tags", "max_items": 20},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.status_code == 400
        assert "編集した背景テキスト" in response.text
        assert "タグは20項目以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_manager")
    def test_transient_error_still_uses_toast(
        self, mock_get_mgr, client, sample_persona
    ):
        """再試行で解決するものは再描画せずトースト（入力はDOM上に残る）。"""
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                "dynamodb unavailable",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert response.text == ""
        assert "HX-Trigger" in response.headers

    @patch("web.routers.persona.get_persona_manager")
    def test_falls_back_to_toast_when_persona_cannot_be_refetched(
        self, mock_get_mgr, client, sample_persona
    ):
        """再描画用のペルソナが取れない場合も入力を壊さない。"""
        mgr = Mock()
        mgr.update_persona.side_effect = PersonaManagerError(
            "name is blank",
            code=ErrorCode.PERSONA_FIELD_REQUIRED,
            context={"field": "name"},
        )
        mgr.get_persona.return_value = None  # 取得できない
        mock_get_mgr.return_value = mgr

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        # フォームを描画できないのでトーストに退避する（画面は書き換えない）
        assert response.text == ""
        assert "HX-Trigger" in response.headers

    @patch("web.routers.persona.get_persona_manager")
    def test_error_response_does_not_leak_exception_message(
        self, mock_get_mgr, client, sample_persona
    ):
        secret = "arn:aws:dynamodb:ap-northeast-1:123456789012:table/personas"
        mock_get_mgr.return_value = self._manager(
            sample_persona,
            PersonaManagerError(
                secret,
                code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                context={"field": "name", "max_length": 50},
            ),
        )

        response = client.put(f"/persona/{sample_persona.id}", data=self._SUBMITTED)

        assert secret not in response.text


class TestPersonaUpdate:
    """ペルソナ更新のテスト"""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_success(self, mock_get_mgr, client, sample_persona):
        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_mgr.update_persona.return_value = sample_persona
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "新名前",
                "age": "35",
                "occupation": "新職業",
                "background": "新背景",
                "values": "価値1\n価値2",
                "pain_points": "課題1\n課題2",
                "goals": "目標1",
            },
        )
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_manager")
    def test_update_clears_demographics_with_empty_form_values(
        self, mock_get_mgr, client, sample_persona
    ):
        """空のgender/country/cityフォーム値が update_persona に空文字で渡りクリアされる"""
        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_mgr.update_persona.return_value = sample_persona
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "新名前",
                "age": "35",
                "occupation": "新職業",
                "background": "新背景",
                "values": "価値1",
                "pain_points": "課題1",
                "goals": "目標1",
                "gender": "",
                "country": "",
                "city": "",
            },
        )
        assert response.status_code == 200
        # update() でクリアできるよう、空文字をそのまま渡している（or None にしない）
        call_kwargs = mock_mgr.update_persona.call_args[1]
        assert call_kwargs["gender"] == ""
        assert call_kwargs["country"] == ""
        assert call_kwargs["city"] == ""

    @patch("web.routers.persona.get_persona_manager")
    def test_update_not_found(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.update_persona.return_value = None
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            "/persona/nonexistent",
            data={
                "name": "X",
                "age": "30",
                "occupation": "Y",
                "background": "Z",
                "values": "v",
                "pain_points": "p",
                "goals": "g",
            },
        )
        assert response.status_code == 404

    @patch("web.routers.persona.get_persona_manager")
    def test_update_validation_error(self, mock_get_mgr, client, sample_persona):
        from src.managers.persona_manager import PersonaManagerError

        mock_mgr = Mock()
        mock_mgr.get_persona.return_value = sample_persona
        mock_mgr.update_persona.side_effect = PersonaManagerError("名前が空です")
        mock_get_mgr.return_value = mock_mgr

        response = client.put(
            f"/persona/{sample_persona.id}",
            data={
                "name": "valid",
                "age": "30",
                "occupation": "Y",
                "background": "Z",
                "values": "v",
                "pain_points": "p",
                "goals": "g",
            },
        )
        assert response.status_code == 400


class TestPersonaMemories:
    """ペルソナ記憶管理のテスト"""

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_memories_list_success(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_memories.return_value = ([], 1, 1)
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/p1/memories")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_memories_list_error(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.get_memories.side_effect = PersonaMemoryManagerError("見つかりません")
        mock_get_mgr.return_value = mock_mgr

        response = client.get("/persona/p1/memories")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_memory_success(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.delete_memory.return_value = True
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories/m1")
        assert response.status_code == 200

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_memory_config_error_replaces_item(self, mock_get_mgr, client):
        """機能無効（CONFIG）は入力修正で解決しないので、アイテムを
        エラーカードで置換してよい（記憶はそもそも削除できない状態）。"""
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_memory.side_effect = PersonaMemoryManagerError(
            "memory feature disabled", code=ErrorCode.MEMORY_FEATURE_DISABLED
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories/m1")

        assert response.status_code == 400
        assert response.headers.get("X-Render-Response") == "true"
        assert 'id="memory-item-m1"' in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_memory_transient_uses_toast(self, mock_get_mgr, client):
        """再試行で解決しうるエラーでは記憶項目を残す（#117）。

        削除の hx-swap は outerHTML。サービス障害・タイムアウトでは記憶は
        まだ存在するため、アイテムをエラーカードで置換すると一覧が実データと
        食い違い、再試行もできなくなる。トーストで通知する。
        """
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_memory.side_effect = PersonaMemoryManagerError(
            "memory service timeout", code=ErrorCode.MEMORY_SERVICE_UNAVAILABLE
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories/m1")

        # 記憶アイテムを置換しない（本文なし）
        assert response.text == ""
        assert "HX-Trigger" in response.headers
        # 記憶アイテムを差し替える id は出さない
        assert "memory-item-m1" not in response.headers.get("HX-Trigger", "")

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_delete_all_memories_disabled(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.delete_all_memories.side_effect = PersonaMemoryManagerError(
            "長期記憶機能が無効です"
        )
        mock_mgr.safe_get_memories.return_value = []
        mock_get_mgr.return_value = mock_mgr

        response = client.delete("/persona/p1/memories")
        assert response.status_code == 200


class TestAddMemoryValidationPreservesInput:
    """知識追加のバリデーション失敗時に入力が失われないこと（#117 ステップ4）。

    記憶一覧（#memory-list-container）は Alpine の `showForm: false` で
    入力欄を折りたたんでいる。エラー時にここを置換すると入力欄が閉じたうえで
    入力内容も失われるため、HX-Retarget でフォーム内の専用領域だけを
    差し替える。
    """

    _FORM = {
        "topic_name": "好きな食べ物",
        "topic_content": "ラーメンが好き",
        "strategy_type": "semantic",
    }

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_validation_error_retargets_to_form_error_area(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "topic name is blank",
            code=ErrorCode.MEMORY_TOPIC_NAME_REQUIRED,
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.status_code == 400
        # 画面に届く印が付いていること
        assert response.headers.get("X-Render-Response") == "true"
        # 一覧ではなく、送信元フォーム内の専用領域に差し替わること。
        # find 相対セレクタで、同じ領域を持つ別フォームと混同しないようにする
        assert response.headers.get("HX-Retarget") == "find .memory-form-error"
        assert response.headers.get("HX-Reswap") == "innerHTML"
        assert "トピック名を入力してください" in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_capacity_error_also_retargets(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "content too long",
            code=ErrorCode.MEMORY_CONTENT_TOO_LONG,
            context={"max_length": 10000},
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.status_code == 400
        assert response.headers.get("HX-Retarget") == "find .memory-form-error"
        assert "内容は10000文字以内で設定してください" in response.text

    @patch("web.routers.persona.get_persona_memory_manager")
    def test_transient_error_uses_toast_not_retarget(self, mock_get_mgr, client):
        from src.managers.persona_memory_manager import PersonaMemoryManagerError

        mock_mgr = Mock()
        mock_mgr.add_knowledge.side_effect = PersonaMemoryManagerError(
            "agentcore unavailable",
            code=ErrorCode.MEMORY_OPERATION_FAILED,
        )
        mock_get_mgr.return_value = mock_mgr

        response = client.post("/persona/p1/memories", data=self._FORM)

        assert response.text == ""
        assert "HX-Trigger" in response.headers
        assert "HX-Retarget" not in response.headers


class TestFormErrorTargetExists:
    """HX-Retarget の差し替え先がテンプレート側に存在すること（#117）。

    サーバーが `HX-Retarget: find .memory-form-error` を返しても、送信元
    フォーム内にその要素が無ければ htmx は差し替え先を見つけられず文言が
    出ない。find 相対セレクタなので **各 form の内側**にあることが必要。
    サーバーとテンプレートの対応をテストで固定する。
    """

    def test_forms_posting_memories_contain_the_error_area(self):
        import re
        from pathlib import Path

        templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
        offenders = []
        for path in templates_dir.rglob("*.html"):
            html = path.read_text(encoding="utf-8")
            # /memories へ POST する <form ...> ... </form> を取り出す
            for form in re.findall(r"<form\b.*?</form>", html, re.DOTALL):
                if 'hx-post="/persona/' not in form or "/memories" not in form:
                    continue
                # upload-preview（プレビュー生成）は記憶追加ではないので対象外
                if "/memories/upload-preview" in form:
                    continue
                if "memory-form-error" not in form:
                    offenders.append(path.name)
        assert offenders == [], (
            "記憶追加フォーム内に .memory-form-error が無い"
            f"（find 相対セレクタが解決できない）: {offenders}"
        )
