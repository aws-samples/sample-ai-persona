"""shared/ ユーティリティのテスト。

insight_utils, document_loader (public化メソッド) をテストする。
"""

import pytest
from unittest.mock import Mock

from src.models.discussion import Discussion
from src.models.insight_category import InsightCategory
from src.managers.shared.insight_utils import (
    attach_insights_to_discussion,
    normalize_and_deduplicate_insights,
    save_categories_to_config,
    get_default_insight_categories,
)
from src.managers.shared.document_loader import (
    build_content_block,
    is_supported_mime_type,
    is_image_type,
    SUPPORTED_IMAGE_TYPES,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_MIME_TYPES,
)


@pytest.mark.unit
class TestInsightUtils:
    """insight_utils のテスト"""

    def test_attach_insights_success(self):
        """インサイト付与が成功すること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        # メッセージを追加（generate_insightsが要求）
        from src.models.message import Message

        msg = Message.create_new(
            persona_id="p1",
            persona_name="太郎",
            content="テスト",
            message_type="statement",
        )
        discussion = discussion.add_message(msg)
        msg2 = Message.create_new(
            persona_id="p2",
            persona_name="花子",
            content="テスト2",
            message_type="statement",
        )
        discussion = discussion.add_message(msg2)

        mock_ai_service = Mock()
        mock_ai_service.extract_insights.return_value = [
            {
                "category": "ニーズ",
                "description": "ユーザーは価格よりも品質を重視する傾向がある",
                "supporting_messages": [],
                "confidence_score": 0.8,
            }
        ]

        result = attach_insights_to_discussion(
            discussion=discussion,
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        assert result.insights is not None
        assert len(result.insights) == 1
        assert result.insights[0].category == "ニーズ"

    def test_attach_insights_failure_returns_discussion_unchanged(self):
        """AI失敗時にインサイトなしの議論がそのまま返ること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )

        mock_ai_service = Mock()
        mock_ai_service.extract_insights.side_effect = Exception("AI error")

        result = attach_insights_to_discussion(
            discussion=discussion,
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        assert result.insights == [] or result.insights is None

    def test_save_categories_to_config(self):
        """カテゴリーがagent_configに保存されること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        categories = [
            InsightCategory(name="ニーズ", description="ユーザーのニーズ"),
            InsightCategory(name="課題", description="課題や不満"),
        ]

        result = save_categories_to_config(discussion, categories)

        assert result.agent_config is not None
        assert "insight_categories" in result.agent_config
        assert len(result.agent_config["insight_categories"]) == 2

    def test_save_categories_empty_returns_unchanged(self):
        """空カテゴリーの場合、議論がそのまま返ること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )

        result = save_categories_to_config(discussion, [])

        assert result.agent_config is None or "insight_categories" not in (
            result.agent_config or {}
        )

    def test_get_default_insight_categories(self):
        """デフォルトカテゴリーが取得できること"""
        categories = get_default_insight_categories()

        assert len(categories) > 0
        assert all(isinstance(c, InsightCategory) for c in categories)


@pytest.mark.unit
class TestNormalizeAndDeduplicateInsights:
    """normalize_and_deduplicate_insights のテスト"""

    def test_category_normalization(self):
        """カテゴリ名が部分一致で正規化されること"""
        categories = [
            InsightCategory(name="ユーザーニーズ", description=""),
            InsightCategory(name="課題", description=""),
        ]
        raw = [
            {
                "category": "ニーズ",
                "description": "テスト用のインサイト説明文です",
                "confidence_score": 0.8,
            }
        ]

        result = normalize_and_deduplicate_insights(raw, categories)

        assert len(result) == 1
        assert result[0]["category"] == "ユーザーニーズ"

    def test_confidence_clamping(self):
        """confidence scoreが0.0-1.0にクランプされること"""
        raw = [
            {
                "category": "テスト",
                "description": "十分に長い説明文テストです",
                "confidence_score": 1.5,
            },
            {
                "category": "テスト",
                "description": "もう一つの十分に長い説明文です",
                "confidence_score": -0.3,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert result[0]["confidence_score"] == 1.0
        assert result[1]["confidence_score"] == 0.0

    def test_short_description_filtered(self):
        """短い説明文がフィルタされること"""
        raw = [
            {"category": "テスト", "description": "短い", "confidence_score": 0.8},
            {
                "category": "テスト",
                "description": "これは十分に長い説明文のテストです",
                "confidence_score": 0.8,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert len(result) == 1
        assert "十分に長い" in result[0]["description"]

    def test_deduplication(self):
        """重複するdescriptionが除去されること"""
        raw = [
            {
                "category": "A",
                "description": "同じ説明文のインサイトです",
                "confidence_score": 0.8,
            },
            {
                "category": "B",
                "description": "同じ説明文のインサイトです",
                "confidence_score": 0.9,
            },
            {
                "category": "C",
                "description": "異なる説明文のインサイトです",
                "confidence_score": 0.7,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert len(result) == 2
        assert result[0]["category"] == "A"
        assert result[1]["category"] == "C"

    def test_invalid_confidence_defaults(self):
        """無効なconfidence値がデフォルト0.5になること"""
        raw = [
            {
                "category": "テスト",
                "description": "十分に長い説明文テストです",
                "confidence_score": "invalid",
            }
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert result[0]["confidence_score"] == 0.5

    def test_empty_input(self):
        """空入力で空リストが返ること"""
        result = normalize_and_deduplicate_insights([], None)
        assert result == []


@pytest.mark.unit
class TestDocumentLoaderPublic:
    """document_loader の public 化メソッドのテスト"""

    def test_build_content_block_png(self):
        """PNG画像のContentBlockが正しく構築されること"""
        result = build_content_block(b"\x89PNG", "image/png", "test.png")

        assert result is not None
        assert "image" in result
        assert result["image"]["format"] == "png"
        assert result["image"]["source"]["bytes"] == b"\x89PNG"

    def test_build_content_block_jpeg(self):
        """JPEG画像のContentBlockが正しく構築されること"""
        result = build_content_block(b"\xff\xd8", "image/jpeg", "photo.jpg")

        assert result is not None
        assert result["image"]["format"] == "jpeg"

    def test_build_content_block_pdf(self):
        """PDFのContentBlockが正しく構築されること"""
        result = build_content_block(b"%PDF-1.4", "application/pdf", "doc.pdf")

        assert result is not None
        assert "document" in result
        assert result["document"]["format"] == "pdf"
        assert result["document"]["name"] == "doc"

    def test_build_content_block_text(self):
        """テキストのContentBlockが正しく構築されること"""
        result = build_content_block(b"hello", "text/plain", "note.txt")

        assert result is not None
        assert result["document"]["format"] == "txt"

    def test_build_content_block_csv(self):
        """CSVのContentBlockが正しく構築されること"""
        result = build_content_block(b"a,b,c", "text/csv", "data.csv")

        assert result is not None
        assert result["document"]["format"] == "csv"

    def test_build_content_block_unsupported_mime(self):
        """未サポートMIMEでNoneが返ること"""
        result = build_content_block(b"data", "application/zip", "archive.zip")

        assert result is None

    def test_build_content_block_filename_sanitize(self):
        """特殊文字を含むファイル名がサニタイズされること"""
        result = build_content_block(b"%PDF", "application/pdf", "日本語ファイル名.pdf")

        assert result is not None
        name = result["document"]["name"]
        assert len(name) <= 100

    def test_is_supported_mime_type_valid(self):
        """サポートMIMEでTrueが返ること"""
        assert is_supported_mime_type("image/png") is True
        assert is_supported_mime_type("application/pdf") is True
        assert is_supported_mime_type("text/csv") is True

    def test_is_supported_mime_type_invalid(self):
        """未サポートMIMEでFalseが返ること"""
        assert is_supported_mime_type("application/zip") is False
        assert is_supported_mime_type("video/mp4") is False
        assert is_supported_mime_type("") is False

    def test_is_image_type(self):
        """画像MIMEの判定が正しいこと"""
        assert is_image_type("image/png") is True
        assert is_image_type("image/jpeg") is True
        assert is_image_type("application/pdf") is False
        assert is_image_type("text/plain") is False

    def test_mime_constants(self):
        """MIME定数が正しく定義されていること"""
        assert "image/png" in SUPPORTED_IMAGE_TYPES
        assert "application/pdf" in SUPPORTED_DOCUMENT_TYPES
        assert len(SUPPORTED_MIME_TYPES) == len(SUPPORTED_IMAGE_TYPES) + len(
            SUPPORTED_DOCUMENT_TYPES
        )
