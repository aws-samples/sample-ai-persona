"""
Agent サービスの単体テスト
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime

from src.models.errors import ErrorCode
from src.services.agent_service import (
    AgentService,
    AgentConfigurationError,
    AgentInitializationError,
    AgentServiceError,
    GenerationCapacityError,
    ReportGenerationCapacityError,
    PersonaAgent,
    FacilitatorAgent,
)
from src.models.persona import Persona
from src.models.message import Message


class TestAgentService:
    """Agent サービスのテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
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

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_agent_service_initialization(self, mock_bedrock_model, mock_agent):
        """AgentServiceの初期化テスト"""
        # AgentServiceを初期化
        agent_service = AgentService()

        # 初期化が成功することを確認
        assert agent_service is not None

    @patch("src.services.agent_service.Agent", None)
    @patch("src.services.agent_service.BedrockModel", None)
    def test_agent_service_initialization_without_sdk(self):
        """Strands SDKがない場合の初期化エラーテスト"""
        # SDKがない場合はエラーが発生することを確認
        with pytest.raises(AgentInitializationError) as exc_info:
            AgentService()

        assert exc_info.value.code is ErrorCode.AGENT_SDK_UNAVAILABLE

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_model_sets_retry_config(self, mock_bedrock_model, mock_agent):
        """BedrockModelに一過性エラー対策のリトライ設定が渡されることを検証する

        ストリーミング開始時のConnection closedエラー対策として、
        boto_client_config（retries付き）が指定されていることを確認する。
        """
        agent_service = AgentService()
        mock_bedrock_model.reset_mock()

        agent_service._create_model()

        mock_bedrock_model.assert_called_once()
        boto_config = mock_bedrock_model.call_args.kwargs["boto_client_config"]
        assert boto_config.retries["max_attempts"] == 3
        assert boto_config.retries["mode"] == "adaptive"

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_model_defaults_to_default_model_id(
        self, mock_bedrock_model, mock_agent
    ):
        """model_id未指定時は既定モデル（DEFAULT_MODEL_ID）が使われる（後方互換）。"""
        from src.models.model_registry import DEFAULT_MODEL_ID

        agent_service = AgentService()
        mock_bedrock_model.reset_mock()

        agent_service._create_model()

        assert mock_bedrock_model.call_args.kwargs["model_id"] == DEFAULT_MODEL_ID

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_model_unknown_id_falls_back_to_default(
        self, mock_bedrock_model, mock_agent
    ):
        """未知のmodel_idは既定モデルに丸められる（get_model_specの丸め動作）。"""
        from src.models.model_registry import DEFAULT_MODEL_ID

        agent_service = AgentService()
        mock_bedrock_model.reset_mock()

        agent_service._create_model("unknown.model-id")

        assert mock_bedrock_model.call_args.kwargs["model_id"] == DEFAULT_MODEL_ID

    @patch("src.services.agent_service.config")
    def test_create_model_mantle_disabled_raises_config_error(self, mock_config):
        """ENABLE_MANTLE_MODELS無効時にMantle系モデルを選択するとCONFIGエラーになる。"""
        mock_config.ENABLE_MANTLE_MODELS = False

        with (
            patch("src.services.agent_service.Agent"),
            patch("src.services.agent_service.BedrockModel"),
        ):
            agent_service = AgentService()

        with pytest.raises(AgentConfigurationError) as exc_info:
            agent_service._create_model("openai.gpt-5.6-terra")

        assert exc_info.value.code is ErrorCode.AGENT_MODEL_MANTLE_DISABLED

    def test_create_model_mantle_enabled_uses_bedrock_mantle_config(self):
        """Mantle有効時はOpenAIResponsesModelにbedrock_mantle_configとmax_output_tokensを渡す。"""
        with (
            patch("src.services.agent_service.Agent"),
            patch("src.services.agent_service.BedrockModel"),
        ):
            agent_service = AgentService()

        with (
            patch("src.services.agent_service.config") as mock_config,
            patch(
                "strands.models.openai_responses.OpenAIResponsesModel"
            ) as mock_openai_model,
        ):
            mock_config.ENABLE_MANTLE_MODELS = True
            mock_config.AGENT_MAX_TOKENS = 32000

            agent_service._create_model("openai.gpt-5.6-terra")

            mock_openai_model.assert_called_once()
            call_kwargs = mock_openai_model.call_args.kwargs
            assert call_kwargs["model_id"] == "openai.gpt-5.6-terra"
            assert call_kwargs["bedrock_mantle_config"] == {"region": "us-east-1"}
            assert call_kwargs["params"] == {"max_output_tokens": 32000}

    def test_build_persona_system_prompt(self):
        """ペルソナシステムプロンプト生成テスト（src/prompts/に移動済み）"""
        from src.prompts.discussion_interview_prompts import (
            build_persona_system_prompt,
        )

        system_prompt = build_persona_system_prompt(self.test_persona)

        assert self.test_persona.name in system_prompt
        assert str(self.test_persona.age) in system_prompt
        assert self.test_persona.occupation in system_prompt
        assert self.test_persona.background in system_prompt

        for value in self.test_persona.values:
            assert value in system_prompt
        for pain_point in self.test_persona.pain_points:
            assert pain_point in system_prompt
        for goal in self.test_persona.goals:
            assert goal in system_prompt

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_persona_agent(self, mock_bedrock_model, mock_agent):
        """ペルソナエージェント作成テスト"""
        # モックの設定
        mock_model_instance = Mock()
        mock_bedrock_model.return_value = mock_model_instance

        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance

        agent_service = AgentService()

        # _create_tool_logging_callbackをモック（strandsモジュールが必要なため）
        agent_service._create_tool_logging_callback = Mock(return_value=None)

        from src.prompts.discussion_interview_prompts import (
            build_persona_system_prompt,
        )

        system_prompt = build_persona_system_prompt(self.test_persona)

        # ペルソナエージェントを作成
        persona_agent = agent_service.create_persona_agent(
            self.test_persona, system_prompt
        )

        # エージェントが正しく作成されたことを確認
        assert persona_agent is not None
        assert isinstance(persona_agent, PersonaAgent)
        assert persona_agent.persona == self.test_persona
        assert persona_agent.system_prompt == system_prompt
        assert persona_agent.get_persona_id() == self.test_persona.id
        assert persona_agent.get_persona_name() == self.test_persona.name

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_facilitator_agent(self, mock_bedrock_model, mock_agent):
        """ファシリテータエージェント作成テスト"""
        # モックの設定
        mock_model_instance = Mock()
        mock_bedrock_model.return_value = mock_model_instance

        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance

        agent_service = AgentService()

        # ファシリテータエージェントを作成
        rounds = 3
        additional_instructions = "議論を活発にしてください"
        facilitator_agent = agent_service.create_facilitator_agent(
            rounds, additional_instructions
        )

        # エージェントが正しく作成されたことを確認
        assert facilitator_agent is not None
        assert isinstance(facilitator_agent, FacilitatorAgent)
        assert facilitator_agent.rounds == rounds
        assert facilitator_agent.additional_instructions == additional_instructions


class TestPersonaAgent:
    """PersonaAgentのテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
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

        # モックエージェントを作成
        self.mock_agent = Mock()
        self.system_prompt = "テスト用システムプロンプト"
        self.persona_agent = PersonaAgent(
            self.test_persona, self.system_prompt, self.mock_agent
        )

    def test_persona_agent_initialization(self):
        """PersonaAgentの初期化テスト"""
        assert self.persona_agent.persona == self.test_persona
        assert self.persona_agent.system_prompt == self.system_prompt
        assert self.persona_agent.agent == self.mock_agent

    def test_get_persona_id(self):
        """ペルソナID取得テスト"""
        assert self.persona_agent.get_persona_id() == self.test_persona.id

    def test_get_persona_name(self):
        """ペルソナ名取得テスト"""
        assert self.persona_agent.get_persona_name() == self.test_persona.name

    def test_respond(self):
        """応答生成テスト"""
        # モックの設定
        expected_response = "これはテスト応答です"
        self.mock_agent.return_value = expected_response

        # 応答を生成
        prompt = "テストプロンプト"
        response = self.persona_agent.respond(prompt)

        # 応答が正しいことを確認
        assert response == expected_response

    def test_respond_with_context(self):
        """コンテキスト付き応答生成テスト"""
        # モックの設定
        expected_response = "コンテキストを考慮した応答です"
        self.mock_agent.return_value = expected_response

        # コンテキストメッセージを作成
        context = [
            Message.create_new("persona-1", "佐藤", "最初のメッセージ"),
            Message.create_new("persona-2", "鈴木", "2番目のメッセージ"),
        ]

        # 応答を生成
        prompt = "コンテキストを踏まえて応答してください"
        response = self.persona_agent.respond(prompt, context)

        # 応答が正しいことを確認
        assert response == expected_response

        # エージェントが呼ばれたことを確認
        self.mock_agent.assert_called_once()

    def test_dispose(self):
        """リソース解放テスト"""
        # disposeメソッドを持つモックエージェント
        self.mock_agent.dispose = Mock()

        # リソースを解放
        self.persona_agent.dispose()

        # disposeが呼ばれたことを確認
        self.mock_agent.dispose.assert_called_once()
        assert self.persona_agent.agent is None

    def test_dispose_with_close_method(self):
        """closeメソッドを持つエージェントのリソース解放テスト"""
        # disposeメソッドを持たず、closeメソッドを持つモックエージェント
        # spec=[]でMockの自動属性生成を無効化
        mock_agent_with_close = Mock(spec=[])
        mock_agent_with_close.close = Mock()
        self.persona_agent.agent = mock_agent_with_close

        # リソースを解放
        self.persona_agent.dispose()

        # closeが呼ばれたことを確認
        mock_agent_with_close.close.assert_called_once()
        assert self.persona_agent.agent is None

    def test_dispose_without_cleanup_method(self):
        """cleanup メソッドを持たないエージェントのリソース解放テスト"""
        # disposeもcloseも持たないモックエージェント
        mock_agent_no_cleanup = Mock(spec=[])
        self.persona_agent.agent = mock_agent_no_cleanup

        # リソースを解放（エラーが発生しないことを確認）
        self.persona_agent.dispose()

        # エージェント参照がクリアされることを確認
        assert self.persona_agent.agent is None


class TestFacilitatorAgent:
    """FacilitatorAgentのテストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # モックエージェントを作成
        self.mock_agent = Mock()
        self.rounds = 3
        self.additional_instructions = "活発な議論を促してください"
        self.facilitator_agent = FacilitatorAgent(
            self.rounds, self.additional_instructions, self.mock_agent
        )

    def test_facilitator_agent_initialization(self):
        """FacilitatorAgentの初期化テスト"""
        assert self.facilitator_agent.rounds == self.rounds
        assert (
            self.facilitator_agent.additional_instructions
            == self.additional_instructions
        )
        assert self.facilitator_agent.agent == self.mock_agent

    def test_start_discussion(self):
        """議論開始テスト"""
        # テスト用ペルソナエージェントを作成
        persona_agents = [
            Mock(get_persona_name=Mock(return_value="田中太郎")),
            Mock(get_persona_name=Mock(return_value="佐藤花子")),
        ]

        topic = "新商品のアイデア"
        start_message = self.facilitator_agent.start_discussion(topic, persona_agents)

        # 開始メッセージに必要な情報が含まれていることを確認
        assert topic in start_message
        assert "田中太郎" in start_message
        assert "佐藤花子" in start_message
        assert str(self.rounds) in start_message

    def test_dispose(self):
        """ファシリテータエージェントのリソース解放テスト"""
        # disposeメソッドを持つモックエージェント
        self.mock_agent.dispose = Mock()

        # リソースを解放
        self.facilitator_agent.dispose()

        # disposeが呼ばれたことを確認
        self.mock_agent.dispose.assert_called_once()
        assert self.facilitator_agent.agent is None


class TestPersonaAgentMultimodal:
    """PersonaAgentのマルチモーダル機能テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        self.test_persona = Persona(
            id="test-persona-1",
            name="田中太郎",
            age=35,
            occupation="会社員",
            background="IT企業で働く中堅社員",
            values=["効率性"],
            pain_points=["時間不足"],
            goals=["キャリアアップ"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self.mock_agent = Mock()
        self.system_prompt = "テスト用システムプロンプト"
        self.persona_agent = PersonaAgent(
            self.test_persona, self.system_prompt, self.mock_agent
        )

    def test_set_document_contents(self):
        """ドキュメントコンテンツ設定テスト"""
        # 画像コンテンツを設定
        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image_data"}}}
        ]

        self.persona_agent.set_document_contents(document_contents)

        # ドキュメントコンテンツが設定されていることを確認
        assert len(self.persona_agent._document_contents) == 1
        assert "image" in self.persona_agent._document_contents[0]

    def test_set_document_contents_empty(self):
        """空のドキュメントコンテンツ設定テスト"""
        self.persona_agent.set_document_contents([])
        assert len(self.persona_agent._document_contents) == 0

        self.persona_agent.set_document_contents(None)
        assert len(self.persona_agent._document_contents) == 0

    def test_respond_with_documents(self):
        """ドキュメント付き応答テスト"""
        # モックの設定
        expected_response = "画像を見て応答します"
        self.mock_agent.return_value = expected_response

        # ドキュメントコンテンツを設定
        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image_data"}}}
        ]
        self.persona_agent.set_document_contents(document_contents)

        # 応答を取得
        prompt = "この画像について意見を述べてください"
        self.persona_agent.respond(prompt)

        # エージェントがContentBlockリストで呼ばれたことを確認
        call_args = self.mock_agent.call_args[0][0]
        assert isinstance(call_args, list)
        assert len(call_args) == 2  # テキスト + 画像
        assert "text" in call_args[0]
        assert "image" in call_args[1]

        # ドキュメントコンテンツがクリアされていることを確認（1回のみ渡す）
        assert len(self.persona_agent._document_contents) == 0

    def test_respond_without_documents(self):
        """ドキュメントなし応答テスト"""
        # モックの設定
        expected_response = "通常の応答です"
        self.mock_agent.return_value = expected_response

        # ドキュメントコンテンツなし
        prompt = "意見を述べてください"
        self.persona_agent.respond(prompt)

        # エージェントがテキストのみで呼ばれたことを確認
        call_args = self.mock_agent.call_args[0][0]
        assert isinstance(call_args, str)

    def test_respond_include_documents_false(self):
        """include_documents=Falseの場合のテスト"""
        # モックの設定
        expected_response = "ドキュメントなしの応答"
        self.mock_agent.return_value = expected_response

        # ドキュメントコンテンツを設定
        document_contents = [
            {"image": {"format": "png", "source": {"bytes": b"fake_image_data"}}}
        ]
        self.persona_agent.set_document_contents(document_contents)

        # include_documents=Falseで応答を取得
        prompt = "意見を述べてください"
        self.persona_agent.respond(prompt, include_documents=False)

        # エージェントがテキストのみで呼ばれたことを確認
        call_args = self.mock_agent.call_args[0][0]
        assert isinstance(call_args, str)

        # ドキュメントコンテンツは保持されていることを確認
        assert len(self.persona_agent._document_contents) == 1


class TestStructuredOutputRetry:
    """run_persona_generation リトライロジックのテスト"""

    def _make_mock_agent(self):
        mock_agent_instance = MagicMock()
        mock_agent_instance.messages = []
        return mock_agent_instance

    def test_structured_output_succeeds_first_try(self):
        """初回成功時はリトライなしで結果を返す"""
        mock_agent_instance = self._make_mock_agent()

        mock_result = MagicMock()
        mock_agent_instance.structured_output.return_value = mock_result

        agent_service = AgentService()
        result, thinking_log = agent_service.run_persona_generation(
            agent=mock_agent_instance,
            prompt="テストデータ",
            structured_prompt="JSON出力してください",
            output_schema=MagicMock,
        )

        assert result == mock_result
        assert mock_agent_instance.structured_output.call_count == 1

    def test_structured_output_retries_on_validation_error(self):
        """バリデーションエラー時にリトライして成功する"""
        mock_agent_instance = self._make_mock_agent()

        mock_result = MagicMock()
        mock_agent_instance.structured_output.side_effect = [
            ValueError("1 validation error for PersonaListOutput"),
            mock_result,
        ]

        agent_service = AgentService()
        result, _ = agent_service.run_persona_generation(
            agent=mock_agent_instance,
            prompt="テストデータ",
            structured_prompt="JSON出力してください",
            output_schema=MagicMock,
        )

        assert result == mock_result
        assert mock_agent_instance.structured_output.call_count == 2

    def test_structured_output_fails_after_max_retries(self):
        """最大リトライ回数を超えたら例外を発生"""
        mock_agent_instance = self._make_mock_agent()

        mock_agent_instance.structured_output.side_effect = ValueError(
            "validation error"
        )

        agent_service = AgentService()
        with pytest.raises(AgentServiceError) as exc_info:
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

        # 容量起因ではないので専用コードは付かず、元例外はチェーンで辿れる
        assert exc_info.value.code is ErrorCode.UNKNOWN
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert mock_agent_instance.structured_output.call_count == 3

    def test_max_tokens_error_raises_capacity_error(self):
        """出力トークン上限超過は GenerationCapacityError に変換される"""
        mock_agent_instance = self._make_mock_agent()

        class MaxTokensReachedException(Exception):
            pass

        mock_agent_instance.side_effect = MaxTokensReachedException(
            "Agent has reached an unrecoverable state due to max_tokens limit."
        )

        agent_service = AgentService()
        with pytest.raises(GenerationCapacityError) as exc_info:
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

        # ユーザー向け文言ではなくエラーコードで種別を表現する（#112）
        assert exc_info.value.code is ErrorCode.GENERATION_CAPACITY_EXCEEDED
        assert isinstance(exc_info.value.__cause__, MaxTokensReachedException)

    def test_capacity_error_message_carries_no_user_wording(self):
        """例外メッセージは診断情報に限られ、ユーザー向け文言を含まない（#112）"""
        mock_agent_instance = self._make_mock_agent()
        mock_agent_instance.side_effect = Exception(
            "Agent has reached an unrecoverable state due to max_tokens limit."
        )

        agent_service = AgentService()
        with pytest.raises(GenerationCapacityError) as exc_info:
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

        message = str(exc_info.value)
        assert "ペルソナ生成数を減らす" not in message
        assert "capacity limit" in message

    def test_read_timeout_raises_capacity_error(self):
        """Bedrock応答タイムアウトは GenerationCapacityError に変換される"""
        mock_agent_instance = self._make_mock_agent()
        mock_agent_instance.structured_output.side_effect = Exception(
            "AWSHTTPSConnectionPool(host='bedrock-runtime...'): Read timed out."
        )

        agent_service = AgentService()
        with pytest.raises(GenerationCapacityError):
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

    def test_capacity_error_fails_fast_without_retry(self):
        """容量起因エラーはバリデーションリトライせず1回で確定的に失敗する"""
        mock_agent_instance = self._make_mock_agent()
        mock_agent_instance.structured_output.side_effect = Exception(
            "Model returned stop_reason: max_tokens instead of tool_use."
        )

        agent_service = AgentService()
        with pytest.raises(GenerationCapacityError):
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

        # リトライループを即抜けるため呼び出しは1回のみ（従来は3回リトライしていた）
        assert mock_agent_instance.structured_output.call_count == 1

    def test_validation_error_still_retries(self):
        """通常のバリデーションエラーは従来通りリトライされる（fail-fastの巻き添えにしない）"""
        mock_agent_instance = self._make_mock_agent()

        mock_result = MagicMock()
        mock_agent_instance.structured_output.side_effect = [
            ValueError("1 validation error for PersonaListOutput: field required"),
            mock_result,
        ]

        agent_service = AgentService()
        result, _ = agent_service.run_persona_generation(
            agent=mock_agent_instance,
            prompt="テストデータ",
            structured_prompt="JSON出力してください",
            output_schema=MagicMock,
        )

        assert result == mock_result
        assert mock_agent_instance.structured_output.call_count == 2

    def test_generic_error_stays_agent_service_error(self):
        """負荷起因でないエラーは従来通り AgentServiceError のまま"""
        mock_agent_instance = self._make_mock_agent()
        mock_agent_instance.structured_output.side_effect = RuntimeError(
            "予期しない内部エラー"
        )

        agent_service = AgentService()
        with pytest.raises(AgentServiceError) as exc_info:
            agent_service.run_persona_generation(
                agent=mock_agent_instance,
                prompt="テストデータ",
                structured_prompt="JSON出力してください",
                output_schema=MagicMock,
            )

        assert not isinstance(exc_info.value, GenerationCapacityError)
        assert exc_info.value.code is ErrorCode.UNKNOWN
        # 内部例外の文言は診断メッセージにも転写しない
        assert "予期しない内部エラー" not in str(exc_info.value)


class TestReportGenerationCapacity:
    """run_report_agent_streaming の負荷超過エラーハンドリングのテスト。

    Issue #110 が明示する「レポート・DWH双方でのMaxTokensReachedException」の
    うちレポート経路（データドリブンレポート）を検証する。

    #112 以降、本メソッドはユーザー向け文言をyield/putせず、コード付き例外を
    送出する。文言の解決は Router が web.error_messages 経由で行う。
    """

    @staticmethod
    def _configure(mock_config):
        mock_config.ENABLE_DATA_AGENT = True
        mock_config.DATA_AGENT_RUNTIME_ARN = "arn:aws:...:runtime/test"
        mock_config.DATA_AGENT_REGION = "ap-northeast-1"
        mock_config.AGENT_MAX_TOKENS = 32000
        mock_config.get_aws_credentials.return_value = {}

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    @patch("src.services.agent_service.config")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    def test_report_max_tokens_error_raises_capacity_error_no_queue(
        self, mock_tool, mock_config, mock_bedrock_model, mock_agent
    ):
        """event_queueなし: 出力トークン上限超過は容量エラーを送出する。"""
        self._configure(mock_config)

        class MaxTokensReachedException(Exception):
            pass

        mock_agent.return_value.side_effect = MaxTokensReachedException(
            "Agent has reached an unrecoverable state due to max_tokens limit."
        )

        agent_service = AgentService()
        with pytest.raises(ReportGenerationCapacityError) as exc_info:
            list(
                agent_service.run_report_agent_streaming(
                    system_prompt="sys",
                    user_content="data",
                )
            )

        assert exc_info.value.code is ErrorCode.REPORT_CAPACITY_EXCEEDED
        assert isinstance(exc_info.value.__cause__, MaxTokensReachedException)

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    @patch("src.services.agent_service.config")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    def test_report_read_timeout_raises_capacity_error_with_queue(
        self, mock_tool, mock_config, mock_bedrock_model, mock_agent
    ):
        """event_queueありでも文言をputせず例外を送出する（Routerが受け取る）。"""
        import queue as queue_mod

        self._configure(mock_config)

        mock_agent.return_value.side_effect = Exception(
            "AWSHTTPSConnectionPool(host='bedrock-runtime...'): Read timed out."
        )

        eq: queue_mod.Queue = queue_mod.Queue()
        agent_service = AgentService()
        with pytest.raises(ReportGenerationCapacityError):
            list(
                agent_service.run_report_agent_streaming(
                    system_prompt="sys",
                    user_content="data",
                    event_queue=eq,
                )
            )

        assert eq.empty(), "文言を event_queue に put してはならない"

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    @patch("src.services.agent_service.config")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    def test_report_generic_error_raises_agent_service_error(
        self, mock_tool, mock_config, mock_bedrock_model, mock_agent
    ):
        """負荷起因でないエラーは容量エラーにせず、内部詳細も転写しない。"""
        self._configure(mock_config)

        mock_agent.return_value.side_effect = RuntimeError(
            "内部スタックトレースを含む詳細"
        )

        agent_service = AgentService()
        with pytest.raises(AgentServiceError) as exc_info:
            list(
                agent_service.run_report_agent_streaming(
                    system_prompt="sys",
                    user_content="data",
                )
            )

        assert not isinstance(exc_info.value, ReportGenerationCapacityError)
        assert "内部スタックトレース" not in str(exc_info.value)
