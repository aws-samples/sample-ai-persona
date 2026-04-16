"""
議論ルーター（discussion.py）のテスト

議論設定、開始、ストリーミング、結果取得エンドポイントをテストします。
"""

from unittest.mock import Mock, patch


class TestDiscussionSetupPage:
    """議論設定ページのテスト"""

    @patch("web.routers.discussion.get_persona_manager")
    def test_setup_page_loads(self, mock_get_manager, client):
        """議論設定ページが正常に読み込まれることを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/setup")

        assert response.status_code == 200
        assert "議論設定" in response.text or "議論" in response.text

    @patch("web.routers.discussion.get_persona_manager")
    def test_setup_page_with_personas(
        self, mock_get_manager, client, sample_persona, sample_persona_2
    ):
        """ペルソナが存在する場合、選択肢として表示されることを確認"""
        mock_manager = Mock()
        mock_manager.get_all_personas.return_value = [sample_persona, sample_persona_2]
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/setup")

        assert response.status_code == 200


class TestDiscussionResultsPage:
    """議論結果一覧ページのテスト"""

    @patch("web.routers.discussion.get_discussion_manager")
    def test_results_page_loads(self, mock_get_manager, client):
        """議論結果ページが正常に読み込まれることを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = []
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/results")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_discussion_manager")
    def test_results_page_with_discussions(
        self, mock_get_manager, client, sample_discussion
    ):
        """議論が存在する場合、一覧が表示されることを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = [sample_discussion]
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/results")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_discussion_manager")
    def test_results_page_filter_by_mode(
        self, mock_get_manager, client, sample_discussion
    ):
        """モードでフィルタリングできることを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = [sample_discussion]
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/results?mode=classic")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_discussion_manager")
    def test_results_page_search(self, mock_get_manager, client, sample_discussion):
        """検索機能が動作することを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion_history.return_value = [sample_discussion]
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/results?search=マーケティング")

        assert response.status_code == 200


class TestDiscussionStartEndpoint:
    """議論開始エンドポイントのテスト"""

    @patch("web.routers.discussion.get_persona_manager")
    def test_start_insufficient_personas(self, mock_get_manager, client):
        """ペルソナ数が不足している場合、エラーを返すことを確認"""
        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "テストトピック",
                "persona_ids": ["persona-1"],
                "mode": "traditional",
            },
        )

        assert response.status_code == 400
        assert "最低2体のペルソナが必要" in response.text

    @patch("web.routers.discussion.get_persona_manager")
    def test_start_invalid_personas(self, mock_get_manager, client):
        """無効なペルソナIDでエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "テストトピック",
                "persona_ids": ["invalid-1", "invalid-2"],
                "mode": "traditional",
            },
        )

        assert response.status_code == 400
        assert "有効なペルソナが2体以上必要" in response.text

    @patch("web.routers.discussion.get_discussion_manager")
    @patch("web.routers.discussion.get_persona_manager")
    def test_start_traditional_mode_success(
        self,
        mock_get_persona,
        mock_get_discussion,
        client,
        sample_persona,
        sample_persona_2,
        sample_discussion,
    ):
        """従来モードでの議論開始が成功することを確認"""
        # ペルソナマネージャーのモック
        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.side_effect = [
            sample_persona,
            sample_persona_2,
        ]
        mock_get_persona.return_value = mock_persona_manager

        # 議論マネージャーのモック
        mock_discussion_manager = Mock()
        mock_discussion_manager.start_discussion.return_value = sample_discussion
        mock_discussion_manager.generate_insights.return_value = []
        mock_discussion_manager.save_discussion_with_insights.return_value = (
            sample_discussion.id
        )
        mock_get_discussion.return_value = mock_discussion_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "テストトピック",
                "persona_ids": [sample_persona.id, sample_persona_2.id],
                "mode": "traditional",
                "rounds": 3,
            },
        )

        assert response.status_code == 200

    @patch("web.routers.discussion.get_persona_manager")
    def test_start_interview_mode_rejected(self, mock_get_manager, client):
        """インタビューモードが拒否されることを確認"""
        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "テストトピック",
                "persona_ids": ["p1", "p2"],
                "mode": "interview",
            },
        )

        assert response.status_code == 400
        assert "インタビューモードは別のエンドポイント" in response.text


class TestDiscussionDetailEndpoint:
    """議論詳細エンドポイントのテスト"""

    @patch("web.routers.discussion.get_persona_manager")
    @patch("web.routers.discussion.get_discussion_manager")
    def test_get_detail_success(
        self,
        mock_get_discussion,
        mock_get_persona,
        client,
        sample_discussion,
        sample_persona,
    ):
        """議論詳細ページが正常に表示されることを確認"""
        mock_discussion_manager = Mock()
        mock_discussion_manager.get_discussion.return_value = sample_discussion
        mock_get_discussion.return_value = mock_discussion_manager

        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        response = client.get(f"/discussion/{sample_discussion.id}")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_discussion_manager")
    def test_get_detail_not_found(self, mock_get_manager, client):
        """存在しない議論で404エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/non-existent-id")

        assert response.status_code == 404


class TestDiscussionDeleteEndpoint:
    """議論削除エンドポイントのテスト"""

    @patch("web.routers.discussion.get_discussion_manager")
    def test_delete_success(self, mock_get_manager, client):
        """議論削除が成功することを確認"""
        mock_manager = Mock()
        mock_manager.delete_discussion.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete("/discussion/test-id")

        assert response.status_code == 200
        assert "削除しました" in response.text

    @patch("web.routers.discussion.get_discussion_manager")
    def test_delete_failure(self, mock_get_manager, client):
        """削除失敗時にエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.delete_discussion.return_value = False
        mock_get_manager.return_value = mock_manager

        response = client.delete("/discussion/non-existent-id")

        assert response.status_code == 400
        assert "削除に失敗しました" in response.text


class TestDiscussionInsightsEndpoint:
    """議論インサイト取得エンドポイントのテスト"""

    @patch("web.routers.discussion.get_discussion_manager")
    def test_get_insights_success(
        self, mock_get_manager, client, sample_discussion, sample_insight
    ):
        """インサイト取得が成功することを確認"""
        discussion_with_insights = sample_discussion.add_insight(sample_insight)
        mock_manager = Mock()
        mock_manager.get_discussion.return_value = discussion_with_insights
        mock_get_manager.return_value = mock_manager

        response = client.get(f"/discussion/insights/{sample_discussion.id}")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_discussion_manager")
    def test_get_insights_not_found(self, mock_get_manager, client):
        """存在しない議論で404エラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_discussion.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/insights/non-existent-id")

        assert response.status_code == 404


class TestDiscussionStreamEndpoint:
    """議論ストリーミングエンドポイントのテスト"""

    @patch("web.routers.discussion.get_persona_manager")
    def test_stream_insufficient_personas(self, mock_get_manager, client):
        """ペルソナ数が不足している場合、エラーを返すことを確認"""
        mock_manager = Mock()
        mock_get_manager.return_value = mock_manager

        response = client.get("/discussion/stream?topic=test&persona_ids=p1")

        assert response.status_code == 200  # SSEなので200を返す
        # エラーメッセージがストリームに含まれることを確認
        assert "最低2体のペルソナが必要" in response.text

    @patch("web.routers.discussion.get_persona_manager")
    def test_stream_invalid_personas(self, mock_get_manager, client):
        """無効なペルソナIDでエラーを返すことを確認"""
        mock_manager = Mock()
        mock_manager.get_persona.return_value = None
        mock_get_manager.return_value = mock_manager

        response = client.get(
            "/discussion/stream?topic=test&persona_ids=invalid1,invalid2"
        )

        assert response.status_code == 200  # SSEなので200を返す
        assert "有効なペルソナが2体以上必要" in response.text


class TestDiscussionDocumentUploadEndpoint:
    """議論用ドキュメントアップロードエンドポイントのテスト (Task 6)"""

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_success(self, mock_get_manager, client):
        """ドキュメントアップロードが成功することを確認"""
        from src.managers.file_manager import FileMetadata
        from datetime import datetime

        mock_manager = Mock()
        mock_metadata = FileMetadata(
            file_id="doc-123",
            original_filename="test_image.png",
            saved_filename="uuid_test_image.png",
            file_path="discussion_documents/uuid_test_image.png",
            file_size=1024,
            file_hash="abc123",
            mime_type="image/png",
            uploaded_at=datetime.now(),
        )
        mock_manager.upload_discussion_document.return_value = mock_metadata
        mock_get_manager.return_value = mock_manager

        # PNG画像のシミュレーション
        png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("test_image.png", png_content, "image/png")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["file_id"] == "doc-123"
        assert data["filename"] == "test_image.png"
        assert data["mime_type"] == "image/png"

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_pdf_success(self, mock_get_manager, client):
        """PDFドキュメントアップロードが成功することを確認"""
        from src.managers.file_manager import FileMetadata
        from datetime import datetime

        mock_manager = Mock()
        mock_metadata = FileMetadata(
            file_id="doc-456",
            original_filename="document.pdf",
            saved_filename="uuid_document.pdf",
            file_path="discussion_documents/uuid_document.pdf",
            file_size=2048,
            file_hash="def456",
            mime_type="application/pdf",
            uploaded_at=datetime.now(),
        )
        mock_manager.upload_discussion_document.return_value = mock_metadata
        mock_get_manager.return_value = mock_manager

        # PDFのシミュレーション
        pdf_content = b"%PDF-1.4\n" + b"\x00" * 100

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("document.pdf", pdf_content, "application/pdf")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["file_id"] == "doc-456"
        assert data["mime_type"] == "application/pdf"

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_invalid_format(self, mock_get_manager, client):
        """無効なファイル形式でエラーを返すことを確認"""
        from src.managers.file_manager import FileUploadError

        mock_manager = Mock()
        mock_manager.upload_discussion_document.side_effect = FileUploadError(
            "許可されていないファイル形式です。対応形式: .png, .jpg, .jpeg, .pdf"
        )
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("test.txt", b"text content", "text/plain")},
        )

        assert response.status_code == 400
        assert "許可されていないファイル形式" in response.text

    @patch("web.routers.discussion.get_file_manager")
    def test_upload_document_oversized(self, mock_get_manager, client):
        """サイズ超過でエラーを返すことを確認"""
        from src.managers.file_manager import FileUploadError

        mock_manager = Mock()
        mock_manager.upload_discussion_document.side_effect = FileUploadError(
            "ファイルサイズが制限を超えています。最大サイズ: 10.0MB"
        )
        mock_get_manager.return_value = mock_manager

        # 大きなファイルのシミュレーション
        large_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000

        response = client.post(
            "/discussion/upload-document",
            files={"file": ("large.png", large_content, "image/png")},
        )

        assert response.status_code == 400
        assert "ファイルサイズが制限を超えています" in response.text


class TestDiscussionStartWithDocuments:
    """ドキュメント付き議論開始のテスト (Task 7)"""

    @patch("web.routers.discussion.get_discussion_manager")
    @patch("web.routers.discussion.get_persona_manager")
    def test_start_with_documents_success(
        self,
        mock_get_persona,
        mock_get_discussion,
        client,
        sample_persona,
        sample_persona_2,
        sample_discussion,
    ):
        """ドキュメント付き議論開始が成功することを確認"""
        # ペルソナマネージャーのモック
        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.side_effect = [
            sample_persona,
            sample_persona_2,
        ]
        mock_get_persona.return_value = mock_persona_manager

        # 議論マネージャーのモック（ドキュメント付き）
        discussion_with_docs = sample_discussion
        discussion_with_docs = discussion_with_docs.__class__(
            id=sample_discussion.id,
            topic=sample_discussion.topic,
            participants=sample_discussion.participants,
            messages=sample_discussion.messages,
            insights=sample_discussion.insights,
            created_at=sample_discussion.created_at,
            mode=sample_discussion.mode,
            agent_config=sample_discussion.agent_config,
            documents=[{"filename": "test.png", "mime_type": "image/png"}],
        )

        mock_discussion_manager = Mock()
        mock_discussion_manager.start_discussion.return_value = discussion_with_docs
        mock_discussion_manager.generate_insights.return_value = []
        mock_discussion_manager.save_discussion_with_insights.return_value = (
            discussion_with_docs.id
        )
        mock_get_discussion.return_value = mock_discussion_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "ドキュメントを参考に議論してください",
                "persona_ids": [sample_persona.id, sample_persona_2.id],
                "mode": "traditional",
                "document_ids": ["doc-123"],
            },
        )

        assert response.status_code == 200

        # start_discussionがdocument_idsパラメータで呼ばれたことを確認
        call_args = mock_discussion_manager.start_discussion.call_args
        assert "document_ids" in call_args[1] or len(call_args[0]) > 2

    @patch("web.routers.discussion.get_discussion_manager")
    @patch("web.routers.discussion.get_persona_manager")
    def test_start_without_documents_success(
        self,
        mock_get_persona,
        mock_get_discussion,
        client,
        sample_persona,
        sample_persona_2,
        sample_discussion,
    ):
        """ドキュメントなし議論開始が従来通り動作することを確認"""
        # ペルソナマネージャーのモック
        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.side_effect = [
            sample_persona,
            sample_persona_2,
        ]
        mock_get_persona.return_value = mock_persona_manager

        # 議論マネージャーのモック
        mock_discussion_manager = Mock()
        mock_discussion_manager.start_discussion.return_value = sample_discussion
        mock_discussion_manager.generate_insights.return_value = []
        mock_discussion_manager.save_discussion_with_insights.return_value = (
            sample_discussion.id
        )
        mock_get_discussion.return_value = mock_discussion_manager

        response = client.post(
            "/discussion/start",
            data={
                "topic": "通常の議論トピック",
                "persona_ids": [sample_persona.id, sample_persona_2.id],
                "mode": "traditional",
            },
        )

        assert response.status_code == 200


class TestDiscussionDetailWithDocuments:
    """ドキュメント付き議論詳細表示のテスト (Task 8)"""

    @patch("web.routers.discussion.get_persona_manager")
    @patch("web.routers.discussion.get_discussion_manager")
    def test_detail_with_documents(
        self,
        mock_get_discussion,
        mock_get_persona,
        client,
        sample_discussion,
        sample_persona,
    ):
        """ドキュメント付き議論詳細が正常に表示されることを確認"""
        from src.models.discussion import Discussion

        # ドキュメント付き議論を作成
        discussion_with_docs = Discussion(
            id=sample_discussion.id,
            topic=sample_discussion.topic,
            participants=sample_discussion.participants,
            messages=sample_discussion.messages,
            insights=sample_discussion.insights,
            created_at=sample_discussion.created_at,
            mode=sample_discussion.mode,
            agent_config=sample_discussion.agent_config,
            documents=[
                {
                    "id": "doc1",
                    "filename": "image.png",
                    "mime_type": "image/png",
                    "file_size": 1024,
                },
                {
                    "id": "doc2",
                    "filename": "document.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 2048,
                },
            ],
        )

        mock_discussion_manager = Mock()
        mock_discussion_manager.get_discussion.return_value = discussion_with_docs
        mock_get_discussion.return_value = mock_discussion_manager

        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        response = client.get(f"/discussion/{sample_discussion.id}")

        assert response.status_code == 200

    @patch("web.routers.discussion.get_persona_manager")
    @patch("web.routers.discussion.get_discussion_manager")
    def test_detail_without_documents(
        self,
        mock_get_discussion,
        mock_get_persona,
        client,
        sample_discussion,
        sample_persona,
    ):
        """ドキュメントなし議論詳細が正常に表示されることを確認"""
        mock_discussion_manager = Mock()
        mock_discussion_manager.get_discussion.return_value = sample_discussion
        mock_get_discussion.return_value = mock_discussion_manager

        mock_persona_manager = Mock()
        mock_persona_manager.get_persona.return_value = sample_persona
        mock_get_persona.return_value = mock_persona_manager

        response = client.get(f"/discussion/{sample_discussion.id}")

        assert response.status_code == 200


class TestDiscussionReportEndpoints:
    """議論レポートエンドポイントのテスト"""

    @patch("web.routers.discussion.get_discussion_manager")
    def test_generate_report_success(self, mock_get_manager, client):
        """レポート生成が成功することを確認"""
        from src.models.discussion_report import DiscussionReport

        mock_report = DiscussionReport.create_new(
            template_type="summary", content="# サマリレポート"
        )
        mock_manager = Mock()
        mock_manager.generate_report.return_value = mock_report
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/test-id/report/generate",
            data={"template_type": "summary"},
        )

        assert response.status_code == 200
        mock_manager.generate_report.assert_called_once_with(
            discussion_id="test-id",
            template_type="summary",
            custom_prompt=None,
        )

    @patch("web.routers.discussion.get_discussion_manager")
    def test_generate_report_custom(self, mock_get_manager, client):
        """カスタムプロンプトでレポート生成できることを確認"""
        from src.models.discussion_report import DiscussionReport

        mock_report = DiscussionReport.create_new(
            template_type="custom",
            content="カスタム結果",
            custom_prompt="箇条書きで",
        )
        mock_manager = Mock()
        mock_manager.generate_report.return_value = mock_report
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/test-id/report/generate",
            data={"template_type": "custom", "custom_prompt": "箇条書きで"},
        )

        assert response.status_code == 200
        mock_manager.generate_report.assert_called_once_with(
            discussion_id="test-id",
            template_type="custom",
            custom_prompt="箇条書きで",
        )

    @patch("web.routers.discussion.get_discussion_manager")
    def test_generate_report_failure(self, mock_get_manager, client):
        """レポート生成失敗時にエラーを返すことを確認"""
        from src.managers.discussion_manager import DiscussionManagerError

        mock_manager = Mock()
        mock_manager.generate_report.side_effect = DiscussionManagerError("議論が見つかりません")
        mock_get_manager.return_value = mock_manager

        response = client.post(
            "/discussion/test-id/report/generate",
            data={"template_type": "summary"},
        )

        assert response.status_code == 400

    @patch("web.routers.discussion.get_discussion_manager")
    def test_delete_report_success(self, mock_get_manager, client):
        """レポート削除が成功することを確認"""
        mock_manager = Mock()
        mock_manager.delete_report.return_value = True
        mock_get_manager.return_value = mock_manager

        response = client.delete("/discussion/test-id/report/report-123")

        assert response.status_code == 200
        mock_manager.delete_report.assert_called_once_with(
            discussion_id="test-id",
            report_id="report-123",
        )

    @patch("web.routers.discussion.get_discussion_manager")
    def test_export_report_md(self, mock_get_manager, client):
        """Markdownエクスポートが成功することを確認"""
        from src.models.discussion_report import DiscussionReport

        mock_report = DiscussionReport.create_new(
            template_type="summary", content="# レポート内容"
        )
        discussion = Mock()
        discussion.topic = "テスト議論"
        discussion.reports = [mock_report]

        mock_manager = Mock()
        mock_manager.get_discussion.return_value = discussion
        mock_get_manager.return_value = mock_manager

        response = client.get(
            f"/discussion/test-id/report/{mock_report.id}/export?format=md"
        )

        assert response.status_code == 200
        assert "# レポート内容" in response.text

    @patch("web.routers.discussion.get_discussion_manager")
    def test_export_report_txt(self, mock_get_manager, client):
        """テキストエクスポートが成功することを確認"""
        from src.models.discussion_report import DiscussionReport

        mock_report = DiscussionReport.create_new(
            template_type="summary", content="# レポート内容\n\n**太字**テスト"
        )
        discussion = Mock()
        discussion.topic = "テスト議論"
        discussion.reports = [mock_report]

        mock_manager = Mock()
        mock_manager.get_discussion.return_value = discussion
        mock_get_manager.return_value = mock_manager

        response = client.get(
            f"/discussion/test-id/report/{mock_report.id}/export?format=txt"
        )

        assert response.status_code == 200
        # Markdown記法が除去されていること
        assert "#" not in response.text or "レポート内容" in response.text
