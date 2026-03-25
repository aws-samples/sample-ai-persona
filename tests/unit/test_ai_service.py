"""
AI サービスの単体テスト
"""

import json
import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.services.ai_service import (
    AIService,
    AIServiceError,
    BedrockConnectionError,
    BedrockAPIError,
)
from src.models.persona import Persona
from src.models.message import Message


class TestAIService:
    """AI サービスのテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # モック Bedrock クライアントを作成
        self.mock_bedrock_client = Mock()
        self.ai_service = AIService(bedrock_client=self.mock_bedrock_client)

        # テスト用ペルソナデータ
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
        """Bedrock クライアントを指定した初期化のテスト"""
        mock_client = Mock()
        service = AIService(bedrock_client=mock_client)
        assert service.bedrock_client == mock_client

    @patch("src.services.ai_service.boto3")
    def test_init_without_bedrock_client(self, mock_boto3):
        """Bedrock クライアントを自動作成する初期化のテスト"""
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        service = AIService()

        mock_boto3.client.assert_called_once()
        assert service.bedrock_client == mock_client

    @patch("src.services.ai_service.boto3", None)
    def test_init_without_boto3(self):
        """boto3 がインストールされていない場合のテスト"""
        with pytest.raises(
            BedrockConnectionError, match="boto3 がインストールされていません"
        ):
            AIService()

    def test_create_bedrock_client_error(self):
        """Bedrock クライアント作成エラーのテスト"""
        with patch("src.services.ai_service.boto3") as mock_boto3:
            mock_boto3.client.side_effect = Exception("AWS認証エラー")

            with pytest.raises(
                BedrockConnectionError, match="Bedrock クライアントの作成に失敗"
            ):
                AIService()

    def test_is_retryable_error(self):
        """リトライ可能エラーの判定テスト"""
        from botocore.exceptions import ClientError, BotoCoreError

        # リトライ可能な ClientError
        retryable_error = ClientError(
            error_response={"Error": {"Code": "ThrottlingException"}},
            operation_name="InvokeModel",
        )
        assert self.ai_service._is_retryable_error(retryable_error) is True

        # リトライ不可能な ClientError
        non_retryable_error = ClientError(
            error_response={"Error": {"Code": "ValidationException"}},
            operation_name="InvokeModel",
        )
        assert self.ai_service._is_retryable_error(non_retryable_error) is False

        # BotoCoreError はリトライ可能
        botocore_error = BotoCoreError()
        assert self.ai_service._is_retryable_error(botocore_error) is True

        # その他のエラーはリトライ不可能
        other_error = ValueError("その他のエラー")
        assert self.ai_service._is_retryable_error(other_error) is False

    def test_invoke_model_success(self):
        """モデル呼び出し成功のテスト"""
        # モックレスポンスを設定
        mock_response = {"body": Mock()}
        mock_response["body"].read.return_value = json.dumps(
            {"content": [{"text": "テスト応答"}]}
        ).encode()

        self.mock_bedrock_client.invoke_model.return_value = mock_response

        result = self.ai_service._invoke_model("テストプロンプト")

        assert result == "テスト応答"
        self.mock_bedrock_client.invoke_model.assert_called_once()

    def test_invoke_model_empty_response(self):
        """モデル呼び出しで空のレスポンスの場合のテスト"""
        mock_response = {"body": Mock()}
        mock_response["body"].read.return_value = json.dumps({"content": []}).encode()

        self.mock_bedrock_client.invoke_model.return_value = mock_response

        with pytest.raises(BedrockAPIError, match="モデルからの応答が空です"):
            self.ai_service._invoke_model("テストプロンプト")

    def test_invoke_model_json_decode_error(self):
        """モデル呼び出しで JSON 解析エラーの場合のテスト"""
        mock_response = {"body": Mock()}
        mock_response["body"].read.return_value = b"invalid json"

        self.mock_bedrock_client.invoke_model.return_value = mock_response

        with pytest.raises(BedrockAPIError, match="レスポンスの JSON 解析に失敗"):
            self.ai_service._invoke_model("テストプロンプト")

    def test_generate_persona_success(self):
        """ペルソナ生成成功のテスト"""
        mock_response = """
        {
            "name": "田中花子",
            "age": 30,
            "occupation": "マーケティング担当",
            "background": "大学卒業後、現在の会社で5年間勤務",
            "values": ["効率性", "品質", "革新性"],
            "pain_points": ["時間不足", "情報過多", "コスト意識"],
            "goals": ["キャリアアップ", "ワークライフバランス", "スキル向上"]
        }
        """

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = mock_response

            result = self.ai_service.generate_persona("テストインタビュー")

            assert isinstance(result, Persona)
            assert result.name == "田中花子"
            assert result.age == 30
            assert result.occupation == "マーケティング担当"
            assert len(result.values) == 3
            assert len(result.pain_points) == 3
            assert len(result.goals) == 3
            mock_retry.assert_called_once()

    def test_generate_persona_empty_interview(self):
        """空のインタビューテキストでペルソナ生成のテスト"""
        with pytest.raises(AIServiceError, match="インタビューテキストが空です"):
            self.ai_service.generate_persona("")

        with pytest.raises(AIServiceError, match="インタビューテキストが空です"):
            self.ai_service.generate_persona("   ")

    def test_generate_persona_error(self):
        """ペルソナ生成エラーのテスト"""
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = Exception("API エラー")

            with pytest.raises(AIServiceError, match="ペルソナ生成中にエラーが発生"):
                self.ai_service.generate_persona("テストインタビュー")

    def test_facilitate_discussion_success(self):
        """議論進行成功のテスト"""
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

    def test_facilitate_discussion_insufficient_personas(self):
        """ペルソナ数不足で議論進行のテスト"""
        with pytest.raises(AIServiceError, match="議論には最低2つのペルソナが必要です"):
            self.ai_service.facilitate_discussion([self.test_persona], "テストトピック")

        with pytest.raises(AIServiceError, match="議論には最低2つのペルソナが必要です"):
            self.ai_service.facilitate_discussion([], "テストトピック")

    def test_facilitate_discussion_empty_topic(self):
        """空のトピックで議論進行のテスト"""
        personas = [self.test_persona, self.test_persona2]

        with pytest.raises(AIServiceError, match="議論トピックが空です"):
            self.ai_service.facilitate_discussion(personas, "")

        with pytest.raises(AIServiceError, match="議論トピックが空です"):
            self.ai_service.facilitate_discussion(personas, "   ")

    def test_facilitate_discussion_too_many_personas(self):
        """ペルソナ数過多で議論進行のテスト"""
        personas = [self.test_persona] * 6  # 6つのペルソナ

        with pytest.raises(AIServiceError, match="議論参加ペルソナは最大5つまでです"):
            self.ai_service.facilitate_discussion(personas, "テストトピック")

    def test_facilitate_discussion_long_topic(self):
        """長すぎるトピックで議論進行のテスト"""
        personas = [self.test_persona, self.test_persona2]
        long_topic = "あ" * 201  # 201文字のトピック

        with pytest.raises(
            AIServiceError, match="議論トピックは200文字以内で入力してください"
        ):
            self.ai_service.facilitate_discussion(personas, long_topic)

    def test_facilitate_discussion_error(self):
        """議論進行エラーのテスト"""
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = Exception("API エラー")

            personas = [self.test_persona, self.test_persona2]
            with pytest.raises(AIServiceError, match="議論進行中にエラーが発生"):
                self.ai_service.facilitate_discussion(personas, "テストトピック")

    def test_extract_insights_success(self):
        """インサイト抽出成功のテスト（構造化データ）"""
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

            # 最初のインサイトを詳細チェック
            first_insight = result[0]
            assert isinstance(first_insight, dict)
            assert first_insight["category"] == "顧客ニーズ"
            assert (
                first_insight["description"] == "顧客は効率性を重視している傾向がある"
            )
            assert first_insight["confidence_score"] == 0.85

            # 2番目のインサイトをチェック
            second_insight = result[1]
            assert second_insight["category"] == "市場機会"
            assert (
                second_insight["description"]
                == "価格よりも品質を優先する傾向が見られる"
            )
            assert second_insight["confidence_score"] == 0.72

    def test_extract_insights_with_custom_categories(self):
        """カスタムカテゴリーでのインサイト抽出のテスト"""
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
        """空のメッセージでインサイト抽出のテスト"""
        with pytest.raises(AIServiceError, match="議論メッセージが空です"):
            self.ai_service.extract_insights([])

    def test_extract_insights_insufficient_messages(self):
        """メッセージ数不足でインサイト抽出のテスト"""
        messages = [
            Message.create_new(
                persona_id="1",
                persona_name="田中太郎",
                content="これは十分に長いテストメッセージです。",
            )
        ]

        with pytest.raises(
            AIServiceError, match="インサイト抽出には最低2つのメッセージが必要です"
        ):
            self.ai_service.extract_insights(messages)

    def test_extract_insights_short_content(self):
        """短すぎる議論内容でインサイト抽出のテスト"""
        messages = [
            Message.create_new(persona_id="1", persona_name="田中太郎", content="短い"),
            Message.create_new(persona_id="2", persona_name="佐藤花子", content="短い"),
        ]

        with pytest.raises(AIServiceError, match="議論内容が短すぎます"):
            self.ai_service.extract_insights(messages)

    def test_extract_insights_error(self):
        """インサイト抽出エラーのテスト"""
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

            with pytest.raises(AIServiceError, match="インサイト抽出中にエラーが発生"):
                self.ai_service.extract_insights(messages)

    def test_parse_discussion_response(self):
        """議論レスポンス解析のテスト"""
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
        """インサイトレスポンス解析のテスト"""
        # 実装に合わせたテストデータ
        # - 箇条書き記号の後にスペースが必要
        # - 10文字以上の内容が必要
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

        # 実装では箇条書き記号（- • *）と番号付きリスト（1. 2.）を認識
        # ・（中黒）は認識されない、短い行は除外される
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
        """リトライ後に成功するケースのテスト"""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        # 最初の2回は失敗、3回目で成功
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
        """最大リトライ回数を超えた場合のテスト"""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        mock_func.side_effect = ClientError(
            error_response={"Error": {"Code": "ThrottlingException"}},
            operation_name="InvokeModel",
        )

        with pytest.raises(BedrockAPIError, match="最大リトライ回数"):
            self.ai_service._retry_with_backoff(mock_func)

        assert mock_func.call_count == 3  # max_retries
        assert mock_sleep.call_count == 2  # max_retries - 1

    @patch("time.sleep")
    def test_retry_with_backoff_non_retryable_error(self, mock_sleep):
        """リトライ不可能なエラーの場合のテスト"""
        from botocore.exceptions import ClientError

        mock_func = Mock()
        mock_func.side_effect = ClientError(
            error_response={"Error": {"Code": "ValidationException"}},
            operation_name="InvokeModel",
        )

        with pytest.raises(BedrockAPIError):
            self.ai_service._retry_with_backoff(mock_func)

        assert mock_func.call_count == 1  # リトライしない
        assert mock_sleep.call_count == 0

    def test_create_persona_generation_prompt(self):
        """ペルソナ生成プロンプト作成のテスト"""
        interview_text = "テストインタビュー内容"
        prompt = self.ai_service._create_persona_generation_prompt(interview_text)

        assert "テストインタビュー内容" in prompt
        assert "JSON形式" in prompt
        assert "name" in prompt
        assert "age" in prompt

    def test_create_discussion_prompt(self):
        """議論プロンプト作成のテスト"""
        personas = [self.test_persona, self.test_persona2]
        topic = "テストトピック"
        prompt = self.ai_service._create_discussion_prompt(personas, topic)

        assert "テストトピック" in prompt
        assert "田中太郎" in prompt
        assert "佐藤花子" in prompt
        assert "効率性" in prompt  # test_persona の価値観
        assert "創造性" in prompt  # test_persona2 の価値観

    def test_create_insight_extraction_prompt(self):
        """インサイト抽出プロンプト作成のテスト"""
        messages = [
            Message.create_new(
                persona_id="1", persona_name="田中太郎", content="テストメッセージ1"
            ),
            Message.create_new(
                persona_id="2", persona_name="佐藤花子", content="テストメッセージ2"
            ),
        ]

        prompt = self.ai_service._create_insight_extraction_prompt(
            messages, categories=None
        )

        assert "**田中太郎**: テストメッセージ1" in prompt
        assert "**佐藤花子**: テストメッセージ2" in prompt
        assert "インサイト" in prompt
        assert "マーケティング" in prompt

    def test_parse_and_validate_persona_success(self):
        """ペルソナ解析・検証成功のテスト"""
        response = """
        以下がペルソナです：
        {
            "name": "田中花子",
            "age": 30,
            "occupation": "マーケティング担当",
            "background": "大学卒業後、現在の会社で5年間勤務",
            "values": ["効率性", "品質", "革新性"],
            "pain_points": ["時間不足", "情報過多", "コスト意識"],
            "goals": ["キャリアアップ", "ワークライフバランス", "スキル向上"]
        }
        その他の説明文
        """

        result = self.ai_service._parse_and_validate_persona(response)

        assert isinstance(result, Persona)
        assert result.name == "田中花子"
        assert result.age == 30
        assert result.occupation == "マーケティング担当"
        assert result.background == "大学卒業後、現在の会社で5年間勤務"
        assert result.values == ["効率性", "品質", "革新性"]
        assert result.pain_points == ["時間不足", "情報過多", "コスト意識"]
        assert result.goals == ["キャリアアップ", "ワークライフバランス", "スキル向上"]

    def test_parse_and_validate_persona_invalid_json(self):
        """無効なJSONでペルソナ解析のテスト"""
        response = "invalid json content"

        with pytest.raises(
            AIServiceError, match="ペルソナの解析中にエラーが発生しました"
        ):
            self.ai_service._parse_and_validate_persona(response)

    def test_parse_and_validate_persona_missing_fields(self):
        """必須フィールド不足でペルソナ解析のテスト"""
        response = """
        {
            "name": "田中花子",
            "age": 30
        }
        """

        with pytest.raises(
            AIServiceError, match="ペルソナの解析中にエラーが発生しました"
        ):
            self.ai_service._parse_and_validate_persona(response)

    def test_extract_json_from_response_success(self):
        """レスポンスからJSON抽出成功のテスト"""
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
        """JSONが含まれていないレスポンスのテスト"""
        response = "JSONが含まれていないレスポンス"

        with pytest.raises(
            AIServiceError, match="レスポンスから有効なJSONを抽出できませんでした"
        ):
            self.ai_service._extract_json_from_response(response)

    def test_extract_json_from_response_incomplete_json(self):
        """不完全なJSONのテスト"""
        response = '{"name": "田中花子", "age": 30'  # 閉じ括弧なし

        with pytest.raises(
            AIServiceError, match="レスポンスから有効なJSONを抽出できませんでした"
        ):
            self.ai_service._extract_json_from_response(response)

    def test_validate_persona_data_success(self):
        """ペルソナデータ検証成功のテスト"""
        valid_data = {
            "name": "田中花子",
            "age": 30,
            "occupation": "マーケティング担当",
            "background": "大学卒業後、現在の会社で5年間勤務",
            "values": ["効率性", "品質", "革新性"],
            "pain_points": ["時間不足", "情報過多", "コスト意識"],
            "goals": ["キャリアアップ", "ワークライフバランス", "スキル向上"],
        }

        # 例外が発生しないことを確認
        self.ai_service._validate_persona_data(valid_data)

    def test_validate_persona_data_missing_required_field(self):
        """必須フィールド不足の検証テスト"""
        invalid_data = {
            "name": "田中花子",
            "age": 30,
            # occupation が不足
        }

        with pytest.raises(
            AIServiceError, match="必須フィールド 'occupation' が不足しています"
        ):
            self.ai_service._validate_persona_data(invalid_data)

    def test_validate_persona_data_invalid_name(self):
        """無効な名前の検証テスト"""
        invalid_data = {
            "name": "",  # 空の名前
            "age": 30,
            "occupation": "マーケティング担当",
            "background": "背景",
            "values": ["価値観"],
            "pain_points": ["課題"],
            "goals": ["目標"],
        }

        with pytest.raises(
            AIServiceError, match="名前は空でない文字列である必要があります"
        ):
            self.ai_service._validate_persona_data(invalid_data)

    def test_validate_persona_data_invalid_age(self):
        """無効な年齢の検証テスト"""
        # 負の年齢
        invalid_data = {
            "name": "田中花子",
            "age": -5,
            "occupation": "マーケティング担当",
            "background": "背景",
            "values": ["価値観"],
            "pain_points": ["課題"],
            "goals": ["目標"],
        }

        with pytest.raises(
            AIServiceError, match="年齢は0から150の範囲である必要があります"
        ):
            self.ai_service._validate_persona_data(invalid_data)

        # 範囲外の年齢
        invalid_data["age"] = 200
        with pytest.raises(
            AIServiceError, match="年齢は0から150の範囲である必要があります"
        ):
            self.ai_service._validate_persona_data(invalid_data)

        # 文字列の年齢（無効）
        invalid_data["age"] = "abc"
        with pytest.raises(
            AIServiceError, match="年齢は有効な数値である必要があります"
        ):
            self.ai_service._validate_persona_data(invalid_data)

    def test_validate_persona_data_invalid_list_fields(self):
        """無効なリストフィールドの検証テスト"""
        # 空のリスト
        invalid_data = {
            "name": "田中花子",
            "age": 30,
            "occupation": "マーケティング担当",
            "background": "背景",
            "values": [],  # 空のリスト
            "pain_points": ["課題"],
            "goals": ["目標"],
        }

        with pytest.raises(
            AIServiceError, match="'values' は少なくとも1つの要素が必要です"
        ):
            self.ai_service._validate_persona_data(invalid_data)

        # リストでない
        invalid_data["values"] = "文字列"
        with pytest.raises(
            AIServiceError, match="'values' はリスト形式である必要があります"
        ):
            self.ai_service._validate_persona_data(invalid_data)

        # 空の文字列要素
        invalid_data["values"] = ["有効な値", ""]
        with pytest.raises(
            AIServiceError,
            match="'values' の各要素は空でない文字列である必要があります",
        ):
            self.ai_service._validate_persona_data(invalid_data)

    def test_generate_persona_parsing_error(self):
        """ペルソナ生成時の解析エラーのテスト"""
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = "invalid json response"

            with pytest.raises(AIServiceError, match="ペルソナ生成中にエラーが発生"):
                self.ai_service.generate_persona("テストインタビュー")

    def test_invoke_converse_api_basic(self):
        """Converse API基本呼び出しテスト (Task 3)"""
        messages = [{"role": "user", "content": [{"text": "こんにちは"}]}]

        mock_response = {"output": {"message": {"content": [{"text": "こんにちは！"}]}}}

        with patch.object(
            self.ai_service.bedrock_client, "converse", return_value=mock_response
        ):
            response = self.ai_service._invoke_converse_api(messages)
            assert response == "こんにちは！"

    def test_prepare_document_content_image(self):
        """画像ドキュメント準備テスト (Task 3)"""
        import tempfile
        import os

        # 一時ファイル作成
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
        """PDFドキュメント準備テスト (Task 3)"""
        import tempfile
        import os

        # 一時ファイル作成
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
        """ドキュメントパラメータ付き議論テスト (Task 3)"""
        # テスト用ペルソナを作成
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

        # ドキュメントなしの場合（後方互換性）
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = "[田中太郎]: テスト発言\n[佐藤花子]: テスト応答"

            messages = self.ai_service.facilitate_discussion(
                personas=personas, topic="テストトピック"
            )

            assert len(messages) >= 2


if __name__ == "__main__":
    pytest.main([__file__])
