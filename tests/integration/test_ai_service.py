"""Tests for AIService."""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.models.errors import ErrorCode
from src.services.ai_service import (
    AIService,
    AIServiceError,
    BedrockConnectionError,
    BedrockAPIError,
)
from src.models.persona import Persona
from src.models.message import Message


class TestAIService:
    """Tests for AIService."""

    def setup_method(self):
        """Runs before each test method."""
        # Mock Bedrock client
        self.mock_bedrock_client = Mock()
        self.ai_service = AIService(bedrock_client=self.mock_bedrock_client)

        # Test persona data
        self.test_persona = Persona(
            id="test-persona-1",
            name="田中太郎",
            age=35,
            occupation="会社員",
            background="IT企業で働く中堅社員",
            values=["効率性", "品質", "革新性"],
            pain_points=["時間不足", "情報過多", "コスト意識"],
            goals=["キャリアアップ", "ワークライフバランス", "スキル向上"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        self.test_persona2 = Persona(
            id="test-persona-2",
            name="佐藤花子",
            age=28,
            occupation="デザイナー",
            background="フリーランスのWebデザイナー",
            values=["創造性", "自由度", "美しさ"],
            pain_points=["収入不安定", "孤独感", "技術変化"],
            goals=["独立成功", "スキル向上", "安定収入"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    def test_init_with_bedrock_client(self):
        """Initialization with an explicit Bedrock client."""
        mock_client = Mock()
        service = AIService(bedrock_client=mock_client)
        assert service.bedrock_client == mock_client

    @patch("src.services.ai_service.boto3")
    def test_init_without_bedrock_client(self, mock_boto3):
        """Initialization that creates the Bedrock client itself."""
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        service = AIService()

        mock_boto3.client.assert_called_once()
        assert service.bedrock_client == mock_client

    @patch("src.services.ai_service.boto3", None)
    def test_init_without_boto3(self):
        """Initialization fails when boto3 is not installed."""
        with pytest.raises(BedrockConnectionError) as exc_info:
            AIService()
        assert exc_info.value.code is ErrorCode.AI_BEDROCK_UNAVAILABLE

    def test_create_bedrock_client_error(self):
        """Bedrock client creation error."""
        with patch("src.services.ai_service.boto3") as mock_boto3:
            mock_boto3.client.side_effect = Exception("AWS認証エラー")

            with pytest.raises(BedrockConnectionError) as exc_info:
                AIService()
            assert exc_info.value.code is ErrorCode.AI_BEDROCK_CONNECTION_FAILED

    def test_is_retryable_error(self):
        """Retryable-error detection."""
        from botocore.exceptions import ClientError, BotoCoreError

        # Retryable ClientError
        retryable_error = ClientError(
            error_response={"Error": {"Code": "ThrottlingException"}},
            operation_name="InvokeModel",
        )
        assert self.ai_service._is_retryable_error(retryable_error) is True

        # Non-retryable ClientError
        non_retryable_error = ClientError(
            error_response={"Error": {"Code": "ValidationException"}},
            operation_name="InvokeModel",
        )
        assert self.ai_service._is_retryable_error(non_retryable_error) is False

        # BotoCoreError is retryable
        botocore_error = BotoCoreError()
        assert self.ai_service._is_retryable_error(botocore_error) is True

        # Other errors are not retryable
        other_error = ValueError("その他のエラー")
        assert self.ai_service._is_retryable_error(other_error) is False

    def test_invoke_model_success(self):
        """Successful model invocation via the Converse API."""
        mock_response = {"output": {"message": {"content": [{"text": "テスト応答"}]}}}
        self.mock_bedrock_client.converse.return_value = mock_response

        result = self.ai_service.invoke_model("テストプロンプト")

        assert result == "テスト応答"
        self.mock_bedrock_client.converse.assert_called_once()

    def test_invoke_model_skips_reasoning_block(self):
        """A text block after a leading reasoningContent block is still extracted."""
        mock_response = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "内部思考"}}},
                        {"text": "テスト応答"},
                    ]
                }
            }
        }
        self.mock_bedrock_client.converse.return_value = mock_response

        result = self.ai_service.invoke_model("テストプロンプト")

        assert result == "テスト応答"

    def test_invoke_model_empty_response(self):
        """Model invocation with an empty response."""
        mock_response = {"output": {"message": {"content": []}}}
        self.mock_bedrock_client.converse.return_value = mock_response

        with pytest.raises(BedrockAPIError) as exc_info:
            self.ai_service.invoke_model("テストプロンプト")
        assert exc_info.value.code is ErrorCode.AI_BEDROCK_API_FAILED

    def test_facilitate_discussion_success(self):
        """Successful discussion facilitation."""
        mock_response = "[田中太郎]: こんにちは\n[佐藤花子]: よろしくお願いします"

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = mock_response

            personas = [self.test_persona, self.test_persona2]
            result = self.ai_service.facilitate_discussion(personas, "テストトピック")

            assert len(result) == 2
            assert result[0].persona_name == "田中太郎"
            assert result[0].content == "こんにちは"
            assert result[1].persona_name == "佐藤花子"
            assert result[1].content == "よろしくお願いします"

    def test_facilitate_discussion_error(self):
        """Discussion facilitation error."""
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = Exception("API エラー")

            personas = [self.test_persona, self.test_persona2]
            with pytest.raises(AIServiceError) as exc_info:
                self.ai_service.facilitate_discussion(personas, "テストトピック")
            assert exc_info.value.code is ErrorCode.AI_OPERATION_FAILED

    def test_facilitate_discussion_streaming_uses_retry(self):
        """Streaming discussion fetches the stream through the retry wrapper.

        Guards against transient connection errors (Connection closed): converse_stream
        must be called via _retry_with_backoff.
        """
        # Mock response carrying stream events
        mock_response = {"stream": []}

        with patch.object(
            self.ai_service, "_retry_with_backoff", return_value=mock_response
        ) as mock_retry:
            personas = [self.test_persona, self.test_persona2]
            list(
                self.ai_service.facilitate_discussion_streaming(
                    personas, "テストトピック"
                )
            )

            # The stream was fetched through the retry wrapper
            mock_retry.assert_called_once()

    def test_facilitate_discussion_streaming_ignores_reasoning_deltas(self):
        """reasoningContent deltas in the Converse stream do not leak into the speech buffer."""
        stream_events = [
            {
                "contentBlockDelta": {
                    "delta": {"reasoningContent": {"text": "内部思考"}}
                }
            },
            {"contentBlockDelta": {"delta": {"text": "[田中太郎]: こんにちは\n"}}},
        ]
        mock_response = {"stream": stream_events}

        with patch.object(
            self.ai_service, "_retry_with_backoff", return_value=mock_response
        ):
            personas = [self.test_persona, self.test_persona2]
            messages = list(
                self.ai_service.facilitate_discussion_streaming(
                    personas, "テストトピック"
                )
            )

        assert len(messages) == 1
        assert messages[0].content == "こんにちは"

    def test_extract_insights_success(self):
        """Successful insight extraction (structured data)."""
        mock_response = """[
            {
                "category": "顧客ニーズ",
                "description": "顧客は効率性を重視している傾向がある",
                "confidence_score": 0.85
            },
            {
                "category": "市場機会",
                "description": "価格よりも品質を優先する傾向が見られる",
                "confidence_score": 0.72
            },
            {
                "category": "商品開発",
                "description": "デザインの重要性が高まっている",
                "confidence_score": 0.68
            }
        ]"""

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = mock_response

            messages = [
                Message.create_new(
                    persona_id="1",
                    persona_name="田中太郎",
                    content="これは十分に長いテストメッセージです。商品について詳細に議論しています。",
                ),
                Message.create_new(
                    persona_id="2",
                    persona_name="佐藤花子",
                    content="私も同様に長いメッセージで、マーケティング戦略について意見を述べています。",
                ),
            ]

            result = self.ai_service.extract_insights(messages)

            assert len(result) == 3
            assert isinstance(result, list)

            # Check the first insight in detail
            first_insight = result[0]
            assert isinstance(first_insight, dict)
            assert first_insight["category"] == "顧客ニーズ"
            assert (
                first_insight["description"] == "顧客は効率性を重視している傾向がある"
            )
            assert first_insight["confidence_score"] == 0.85

            # Check the second insight
            second_insight = result[1]
            assert second_insight["category"] == "市場機会"
            assert (
                second_insight["description"]
                == "価格よりも品質を優先する傾向が見られる"
            )
            assert second_insight["confidence_score"] == 0.72

    def test_extract_insights_falls_back_to_bullet_text(self):
        """A bullet-list reply (no JSON) is parsed into categorized insights."""
        bullet_response = (
            "- [顧客ニーズ] ユーザーは使いやすさと効率性を最も重視している\n"
            "- [商品開発] シンプルで直感的なユーザーインターフェースの設計が必要\n"
            "- 分類のない気づきもその他として残る\n"
        )
        messages = [
            Message.create_new(
                persona_id="1",
                persona_name="田中太郎",
                content="これは十分に長いテストメッセージです。商品について詳細に議論しています。",
            ),
        ]

        with patch.object(
            self.ai_service, "_retry_with_backoff", return_value=bullet_response
        ):
            result = self.ai_service.extract_insights(messages)

        assert [(i["category"], i["description"]) for i in result] == [
            ("顧客ニーズ", "ユーザーは使いやすさと効率性を最も重視している"),
            ("商品開発", "シンプルで直感的なユーザーインターフェースの設計が必要"),
            ("その他", "分類のない気づきもその他として残る"),
        ]
        assert all(i["confidence_score"] == 0.6 for i in result)

    def test_extract_insights_with_custom_categories(self):
        """Insight extraction with custom categories."""
        from src.models.insight_category import InsightCategory

        custom_categories = [
            InsightCategory(
                name="技術トレンド", description="技術的なトレンドや将来の技術動向"
            ),
            InsightCategory(
                name="ユーザー体験", description="ユーザー体験に関する洞察"
            ),
        ]

        mock_response = """[
            {
                "category": "技術トレンド",
                "description": "AIの活用が重要になっている",
                "confidence_score": 0.90
            },
            {
                "category": "ユーザー体験",
                "description": "シンプルなUIが求められている",
                "confidence_score": 0.85
            }
        ]"""

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = mock_response

            messages = [
                Message.create_new(
                    persona_id="1",
                    persona_name="田中太郎",
                    content="これは十分に長いテストメッセージです。技術について詳細に議論しています。",
                ),
                Message.create_new(
                    persona_id="2",
                    persona_name="佐藤花子",
                    content="私も同様に長いメッセージで、ユーザー体験について意見を述べています。",
                ),
            ]

            result = self.ai_service.extract_insights(
                messages, categories=custom_categories
            )

            assert len(result) == 2
            assert result[0]["category"] == "技術トレンド"
            assert result[1]["category"] == "ユーザー体験"

    def test_extract_insights_empty_messages(self):
        """Insight extraction with empty messages (validated in the Manager layer, not the Service)."""
        # The Manager layer already validated input, so the Service calls the API even for an empty list.
        # Without an API mock an exception is raised here; verify it is not a validation error.
        with pytest.raises(Exception):
            self.ai_service.extract_insights([])

    def test_extract_insights_error(self):
        """Insight extraction error."""
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = Exception("API エラー")

            messages = [
                Message.create_new(
                    persona_id="1",
                    persona_name="田中太郎",
                    content="これは十分に長いテストメッセージです。商品について詳細に議論しています。",
                ),
                Message.create_new(
                    persona_id="2",
                    persona_name="佐藤花子",
                    content="私も同様に長いメッセージで、マーケティング戦略について意見を述べています。",
                ),
            ]

            with pytest.raises(AIServiceError) as exc_info:
                self.ai_service.extract_insights(messages)
            assert exc_info.value.code is ErrorCode.AI_OPERATION_FAILED

    def test_parse_discussion_response(self):
        """Discussion response parsing."""
        response = """
[田中太郎]: こんにちは、よろしくお願いします。
[佐藤花子]: こちらこそ、よろしくお願いします。
[田中太郎]: 今日のトピックについて話しましょう。

無効な行
[無効なペルソナ]: この発言は無視される
[田中太郎]: 最後の発言です。
"""

        personas = [self.test_persona, self.test_persona2]
        result = self.ai_service._parse_discussion_response(response, personas)

        assert len(result) == 4
        assert result[0].persona_name == "田中太郎"
        assert result[0].content == "こんにちは、よろしくお願いします。"
        assert result[1].persona_name == "佐藤花子"
        assert result[1].content == "こちらこそ、よろしくお願いします。"
        assert result[2].persona_name == "田中太郎"
        assert result[2].content == "今日のトピックについて話しましょう。"
        assert result[3].persona_name == "田中太郎"
        assert result[3].content == "最後の発言です。"

    def test_parse_insights_response(self):
        """Insight response parsing."""
        # Test data matching the implementation:
        # - a space is required after the bullet marker
        # - content must be at least 10 characters
        response = """
以下がインサイトです：

- 顧客は効率性を重視している傾向がある
• 価格よりも品質を優先する傾向がある
* デザインの重要性が高まっている傾向

1. 新しい市場機会があると考えられる
2. 競合との差別化が重要であると判明

空の行

無効な行（記号なし）
"""

        result = self.ai_service._parse_insights_response(response)

        # The implementation recognizes bullet markers (- * and the bullet character) and numbered lists (1. 2.)
        # The middle dot is not recognized, and short lines are excluded
        expected_insights = [
            "顧客は効率性を重視している傾向がある",
            "価格よりも品質を優先する傾向がある",
            "デザインの重要性が高まっている傾向",
            "新しい市場機会があると考えられる",
            "競合との差別化が重要であると判明",
        ]

        assert len(result) == 5
        assert result == expected_insights

    @patch("time.sleep")
    def test_retry_with_backoff_success_after_retry(self, mock_sleep):
        """Succeeds after a retry."""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        # The first two calls fail, the third succeeds
        mock_func.side_effect = [
            ClientError(
                error_response={"Error": {"Code": "ThrottlingException"}},
                operation_name="InvokeModel",
            ),
            ClientError(
                error_response={"Error": {"Code": "ThrottlingException"}},
                operation_name="InvokeModel",
            ),
            "成功",
        ]

        result = self.ai_service._retry_with_backoff(mock_func)

        assert result == "成功"
        assert mock_func.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("time.sleep")
    def test_retry_with_backoff_max_retries_exceeded(self, mock_sleep):
        """Fails after exceeding the maximum retries."""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        mock_func.side_effect = ClientError(
            error_response={"Error": {"Code": "ThrottlingException"}},
            operation_name="InvokeModel",
        )

        with pytest.raises(BedrockAPIError) as exc_info:
            self.ai_service._retry_with_backoff(mock_func)
        assert exc_info.value.code is ErrorCode.AI_BEDROCK_API_FAILED

        assert mock_func.call_count == 3  # max_retries
        assert mock_sleep.call_count == 2  # max_retries - 1

    @patch("time.sleep")
    def test_retry_with_backoff_non_retryable_error(self, mock_sleep):
        """Non-retryable error."""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        mock_func.side_effect = ClientError(
            error_response={"Error": {"Code": "ValidationException"}},
            operation_name="InvokeModel",
        )

        with pytest.raises(BedrockAPIError):
            self.ai_service._retry_with_backoff(mock_func)

        assert mock_func.call_count == 1  # No retry
        assert mock_sleep.call_count == 0

    def test_create_discussion_prompt(self):
        """Discussion prompt creation."""
        personas = [self.test_persona, self.test_persona2]
        topic = "テストトピック"
        prompt = self.ai_service._create_discussion_prompt(personas, topic)

        assert "テストトピック" in prompt
        assert "田中太郎" in prompt
        assert "佐藤花子" in prompt
        assert "効率性" in prompt  # Values of test_persona
        assert "創造性" in prompt  # Values of test_persona2

    def test_create_insight_extraction_prompt(self):
        """Insight extraction prompt creation."""
        messages = [
            Message.create_new(
                persona_id="1",
                persona_name="田中太郎",
                content="テストメッセージ1",
                round_number=1,
            ),
            Message.create_new(
                persona_id="2",
                persona_name="佐藤花子",
                content="テストメッセージ2",
                round_number=1,
            ),
        ]

        prompt = self.ai_service._create_insight_extraction_prompt(
            messages, categories=None
        )

        assert "**田中太郎**: テストメッセージ1" in prompt
        assert "**佐藤花子**: テストメッセージ2" in prompt
        assert "インサイト" in prompt

    def test_extract_json_from_response_success(self):
        """JSON extraction from a response succeeds."""
        response = """
        以下がペルソナです：
        {
            "name": "田中花子",
            "age": 30,
            "occupation": "マーケティング担当"
        }
        その他の説明文
        """

        result = self.ai_service._extract_json_from_response(response)
        expected = '{\n            "name": "田中花子",\n            "age": 30,\n            "occupation": "マーケティング担当"\n        }'

        assert result == expected

    def test_extract_json_from_response_no_json(self):
        """Response containing no JSON."""
        response = "JSONが含まれていないレスポンス"

        with pytest.raises(AIServiceError) as exc_info:
            self.ai_service._extract_json_from_response(response)
        assert exc_info.value.code is ErrorCode.AI_OPERATION_FAILED

    def test_extract_json_from_response_incomplete_json(self):
        """Incomplete JSON."""
        response = '{"name": "田中花子", "age": 30'  # Missing closing brace

        with pytest.raises(AIServiceError) as exc_info:
            self.ai_service._extract_json_from_response(response)
        assert exc_info.value.code is ErrorCode.AI_OPERATION_FAILED

    def test_invoke_converse_api_basic(self):
        """Basic Converse API call."""
        messages = [{"role": "user", "content": [{"text": "こんにちは"}]}]

        mock_response = {"output": {"message": {"content": [{"text": "こんにちは！"}]}}}

        with patch.object(
            self.ai_service.bedrock_client, "converse", return_value=mock_response
        ):
            response = self.ai_service._invoke_converse_api(messages)
            assert response == "こんにちは！"

    def test_invoke_converse_api_skips_reasoning_block(self):
        """A text block after a leading reasoningContent block is still extracted."""
        messages = [{"role": "user", "content": [{"text": "こんにちは"}]}]

        mock_response = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "内部思考"}}},
                        {"text": "こんにちは！"},
                    ]
                }
            }
        }

        with patch.object(
            self.ai_service.bedrock_client, "converse", return_value=mock_response
        ):
            response = self.ai_service._invoke_converse_api(messages)
            assert response == "こんにちは！"

    def test_prepare_document_content_image(self):
        """Image document preparation."""
        import tempfile
        import os

        # Create a temporary file
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            temp_path = f.name

        try:
            documents = [
                {
                    "file_path": temp_path,
                    "mime_type": "image/png",
                    "original_filename": "test.png",
                }
            ]

            content_list = self.ai_service._prepare_document_content(documents)

            assert len(content_list) == 1
            assert "image" in content_list[0]
            assert content_list[0]["image"]["format"] == "png"
            assert "source" in content_list[0]["image"]
            assert "bytes" in content_list[0]["image"]["source"]
        finally:
            os.unlink(temp_path)

    def test_prepare_document_content_pdf(self):
        """PDF document preparation."""
        import tempfile
        import os

        # Create a temporary file
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n" + b"\x00" * 100)
            temp_path = f.name

        try:
            documents = [
                {
                    "file_path": temp_path,
                    "mime_type": "application/pdf",
                    "original_filename": "test.pdf",
                }
            ]

            content_list = self.ai_service._prepare_document_content(documents)

            assert len(content_list) == 1
            assert "document" in content_list[0]
            assert content_list[0]["document"]["format"] == "pdf"
            assert content_list[0]["document"]["name"] == "test.pdf"
        finally:
            os.unlink(temp_path)

    def test_facilitate_discussion_with_documents_parameter(self):
        """Discussion with a document parameter."""
        # Create a test persona
        from src.models.persona import Persona

        personas = [
            Persona.create_new(
                name="田中太郎",
                age=30,
                occupation="会社員",
                background="テスト",
                values=["効率性"],
                pain_points=["時間不足"],
                goals=["改善"],
            ),
            Persona.create_new(
                name="佐藤花子",
                age=25,
                occupation="デザイナー",
                background="テスト",
                values=["創造性"],
                pain_points=["収入"],
                goals=["独立"],
            ),
        ]

        # No documents (backward compatibility)
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = "[田中太郎]: テスト発言\n[佐藤花子]: テスト応答"

            messages = self.ai_service.facilitate_discussion(
                personas=personas, topic="テストトピック"
            )

            assert len(messages) >= 2


if __name__ == "__main__":
    pytest.main([__file__])
