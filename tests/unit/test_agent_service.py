"""
Agent サービスの単体テスト
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime

from src.services.agent_service import (
    AgentService,
    AgentInitializationError,
    AgentServiceError,
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

        assert "Strands Agent SDK" in str(exc_info.value)

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_bedrock_model_sets_retry_config(
        self, mock_bedrock_model, mock_agent
    ):
        """BedrockModelに一過性エラー対策のリトライ設定が渡されることを検証する

        ストリーミング開始時のConnection closedエラー対策として、
        boto_client_config（retries付き）が指定されていることを確認する。
        """
        agent_service = AgentService()
        mock_bedrock_model.reset_mock()

        agent_service._create_bedrock_model()

        mock_bedrock_model.assert_called_once()
        boto_config = mock_bedrock_model.call_args.kwargs["boto_client_config"]
        assert boto_config.retries["max_attempts"] == 3
        assert boto_config.retries["mode"] == "adaptive"

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_generate_persona_system_prompt(self, mock_bedrock_model, mock_agent):
        """ペルソナシステムプロンプト生成テスト"""
        agent_service = AgentService()

        # システムプロンプトを生成
        system_prompt = agent_service.generate_persona_system_prompt(self.test_persona)

        # プロンプトに必要な情報が含まれていることを確認
        assert self.test_persona.name in system_prompt
        assert str(self.test_persona.age) in system_prompt
        assert self.test_persona.occupation in system_prompt
        assert self.test_persona.background in system_prompt

        # 価値観、課題、目標が含まれていることを確認
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

        # システムプロンプトを生成
        system_prompt = agent_service.generate_persona_system_prompt(self.test_persona)

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
    """structured_output リトライロジックのテスト"""

    def _make_mock_agent(self):
        mock_agent_instance = MagicMock()
        mock_agent_instance.messages = []
        return mock_agent_instance

    @patch.object(AgentService, "create_persona_generation_agent")
    def test_structured_output_succeeds_first_try(self, mock_create_agent):
        """初回成功時はリトライなしで結果を返す"""
        mock_agent_instance = self._make_mock_agent()
        mock_create_agent.return_value = mock_agent_instance

        mock_result = MagicMock()
        mock_persona = MagicMock()
        mock_persona.name = "田中太郎"
        mock_persona.age = 30
        mock_persona.occupation = "エンジニア"
        mock_persona.background = "IT企業勤務"
        mock_persona.values = ["効率性"]
        mock_persona.pain_points = ["時間不足"]
        mock_persona.goals = ["キャリアアップ"]
        mock_persona.gender = "male"
        mock_persona.country = "JP"
        mock_persona.city = "東京都"
        mock_result.personas = [mock_persona]

        mock_agent_instance.structured_output.return_value = mock_result

        agent_service = AgentService()
        personas, _ = agent_service.generate_personas_with_agent(
            data_text="テストデータ",
            data_type="text",
            persona_count=1,
        )

        assert len(personas) == 1
        assert personas[0].name == "田中太郎"
        assert mock_agent_instance.structured_output.call_count == 1

    @patch.object(AgentService, "create_persona_generation_agent")
    def test_structured_output_retries_on_validation_error(self, mock_create_agent):
        """バリデーションエラー時にリトライして成功する"""
        mock_agent_instance = self._make_mock_agent()
        mock_create_agent.return_value = mock_agent_instance

        mock_result = MagicMock()
        mock_persona = MagicMock()
        mock_persona.name = "鈴木花子"
        mock_persona.age = 25
        mock_persona.occupation = "デザイナー"
        mock_persona.background = "フリーランス"
        mock_persona.values = ["創造性"]
        mock_persona.pain_points = ["収入不安定"]
        mock_persona.goals = ["独立"]
        mock_persona.gender = "female"
        mock_persona.country = "JP"
        mock_persona.city = "大阪府"
        mock_result.personas = [mock_persona]

        mock_agent_instance.structured_output.side_effect = [
            ValueError("1 validation error for PersonaListOutput"),
            mock_result,
        ]

        agent_service = AgentService()
        personas, _ = agent_service.generate_personas_with_agent(
            data_text="テストデータ",
            data_type="dwh",
            persona_count=1,
        )

        assert len(personas) == 1
        assert personas[0].name == "鈴木花子"
        assert mock_agent_instance.structured_output.call_count == 2

    @patch.object(AgentService, "create_persona_generation_agent")
    def test_structured_output_fails_after_max_retries(self, mock_create_agent):
        """最大リトライ回数を超えたら例外を発生"""
        mock_agent_instance = self._make_mock_agent()
        mock_create_agent.return_value = mock_agent_instance

        mock_agent_instance.structured_output.side_effect = ValueError(
            "validation error"
        )

        agent_service = AgentService()
        with pytest.raises(AgentServiceError, match="ペルソナ生成エラー"):
            agent_service.generate_personas_with_agent(
                data_text="テストデータ",
                data_type="dwh",
                persona_count=1,
            )

        assert mock_agent_instance.structured_output.call_count == 3
