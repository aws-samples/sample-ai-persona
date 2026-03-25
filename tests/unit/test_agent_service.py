"""
Agent サービスの単体テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.services.agent_service import (
    AgentService,
    AgentInitializationError,
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
        assert facilitator_agent.current_round == 0


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
        assert self.facilitator_agent.current_round == 0

    def test_should_continue(self):
        """議論継続判定テスト"""
        # 最初は継続すべき
        assert self.facilitator_agent.should_continue() is True

        # ラウンドを進める
        self.facilitator_agent.current_round = 2
        assert self.facilitator_agent.should_continue() is True

        # 最終ラウンドに達したら継続しない
        self.facilitator_agent.current_round = 3
        assert self.facilitator_agent.should_continue() is False

    def test_increment_round(self):
        """ラウンドインクリメントテスト"""
        assert self.facilitator_agent.current_round == 0

        self.facilitator_agent.increment_round()
        assert self.facilitator_agent.current_round == 1

        self.facilitator_agent.increment_round()
        assert self.facilitator_agent.current_round == 2

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

    def test_select_next_speaker(self):
        """次の発言者選択テスト"""
        # テスト用ペルソナエージェントを作成
        persona_agents = [
            Mock(get_persona_id=Mock(return_value="persona-1")),
            Mock(get_persona_id=Mock(return_value="persona-2")),
            Mock(get_persona_id=Mock(return_value="persona-3")),
        ]

        # まだ誰も発言していない場合
        spoken_in_round = []
        selected = self.facilitator_agent.select_next_speaker(
            persona_agents, spoken_in_round
        )
        assert selected in persona_agents

        # 一部が発言済みの場合
        spoken_in_round = ["persona-1"]
        selected = self.facilitator_agent.select_next_speaker(
            persona_agents, spoken_in_round
        )
        assert selected.get_persona_id() != "persona-1"

        # 全員が発言済みの場合
        spoken_in_round = ["persona-1", "persona-2", "persona-3"]
        selected = self.facilitator_agent.select_next_speaker(
            persona_agents, spoken_in_round
        )
        assert selected is None

    def test_summarize_round(self):
        """ラウンド要約テスト"""
        # モックの設定
        expected_summary = "ラウンド1の要約内容"
        self.mock_agent.return_value = expected_summary

        # テスト用メッセージを作成
        from src.models.message import Message

        round_messages = [
            Message.create_new("persona-1", "田中太郎", "私の意見は..."),
            Message.create_new("persona-2", "佐藤花子", "それに対して..."),
        ]

        # ラウンドを要約
        topic = "新商品のアイデア"
        summary = self.facilitator_agent.summarize_round(1, round_messages, topic)

        # 要約が正しいことを確認
        assert summary == expected_summary
        self.mock_agent.assert_called_once()

    def test_dispose(self):
        """ファシリテータエージェントのリソース解放テスト"""
        # disposeメソッドを持つモックエージェント
        self.mock_agent.dispose = Mock()

        # リソースを解放
        self.facilitator_agent.dispose()

        # disposeが呼ばれたことを確認
        self.mock_agent.dispose.assert_called_once()
        assert self.facilitator_agent.agent is None

    def test_create_prompt_for_persona(self):
        """ペルソナ向けプロンプト生成テスト"""
        # テスト用ペルソナエージェントを作成
        persona_agent = Mock(get_persona_name=Mock(return_value="田中太郎"))

        topic = "新商品のアイデア"

        # コンテキストなしの場合（最初の発言）
        prompt = self.facilitator_agent.create_prompt_for_persona(
            persona_agent, topic, []
        )
        assert topic in prompt

        # コンテキストありの場合
        context = [
            Message.create_new("persona-1", "佐藤", "最初の意見です"),
            Message.create_new("persona-2", "鈴木", "2番目の意見です"),
        ]
        prompt = self.facilitator_agent.create_prompt_for_persona(
            persona_agent, topic, context
        )
        assert topic in prompt
        assert "佐藤" in prompt or "鈴木" in prompt


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
        response = self.persona_agent.respond(prompt)

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
        response = self.persona_agent.respond(prompt)

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
        response = self.persona_agent.respond(prompt, include_documents=False)

        # エージェントがテキストのみで呼ばれたことを確認
        call_args = self.mock_agent.call_args[0][0]
        assert isinstance(call_args, str)

        # ドキュメントコンテンツは保持されていることを確認
        assert len(self.persona_agent._document_contents) == 1


class TestMarketResearchAgent:
    """市場調査分析エージェントのテストクラス"""

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_create_market_research_agent(self, mock_bedrock_model, mock_agent):
        """市場調査分析エージェント作成テスト"""
        mock_model_instance = Mock()
        mock_bedrock_model.return_value = mock_model_instance

        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance

        agent_service = AgentService()
        agent = agent_service.create_market_research_agent()

        assert agent is not None
        mock_agent.assert_called()

        # システムプロンプトに必要な内容が含まれていることを確認
        call_kwargs = mock_agent.call_args[1]
        assert "system_prompt" in call_kwargs
        assert "市場調査" in call_kwargs["system_prompt"]
        assert "ペルソナ" in call_kwargs["system_prompt"]

    @patch("src.services.agent_service.Agent", None)
    @patch("src.services.agent_service.BedrockModel", None)
    def test_create_market_research_agent_without_sdk(self):
        """SDKがない場合のエラーテスト"""
        with pytest.raises(AgentInitializationError):
            agent_service = AgentService()
            agent_service.create_market_research_agent()

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_generate_personas_from_report(self, mock_bedrock_model, mock_agent):
        """レポートからの複数ペルソナ生成テスト"""
        mock_model_instance = Mock()
        mock_bedrock_model.return_value = mock_model_instance

        # エージェントの応答をモック
        mock_response = """[
            {
                "name": "田中 花子",
                "age": 35,
                "occupation": "会社員",
                "background": "東京都在住のマーケター",
                "values": ["効率性", "品質"],
                "pain_points": ["時間不足", "情報過多"],
                "goals": ["キャリアアップ", "スキル向上"]
            },
            {
                "name": "佐藤 健太",
                "age": 28,
                "occupation": "エンジニア",
                "background": "神奈川県在住のフリーランス",
                "values": ["自由", "技術力"],
                "pain_points": ["収入不安定", "孤独"],
                "goals": ["安定収入", "コミュニティ"]
            }
        ]"""
        mock_agent_instance = Mock()
        mock_agent_instance.return_value = mock_response
        mock_agent.return_value = mock_agent_instance

        agent_service = AgentService()
        report_text = "これは市場調査レポートです。" * 50

        personas = agent_service.generate_personas_from_report(report_text, 2)

        assert len(personas) == 2
        assert personas[0].name == "田中 花子"
        assert personas[1].name == "佐藤 健太"

    @patch("src.services.agent_service.Agent")
    @patch("src.services.agent_service.BedrockModel")
    def test_generate_personas_from_report_invalid_json(
        self, mock_bedrock_model, mock_agent
    ):
        """無効なJSON応答のエラーハンドリングテスト"""
        mock_model_instance = Mock()
        mock_bedrock_model.return_value = mock_model_instance

        mock_agent_instance = Mock()
        mock_agent_instance.return_value = "これは無効なJSON応答です"
        mock_agent.return_value = mock_agent_instance

        agent_service = AgentService()
        report_text = "これは市場調査レポートです。" * 50

        from src.services.agent_service import AgentServiceError

        with pytest.raises(AgentServiceError):
            agent_service.generate_personas_from_report(report_text, 2)
