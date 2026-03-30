"""
Agent Service
Strands Agent SDKを使用したエージェント管理サービス
"""

import random
import logging
from typing import List, Dict, Any, Optional
from dataclasses import asdict

try:
    from strands import Agent
    from strands.models import BedrockModel
except ImportError:
    # Strands SDKがインストールされていない場合のフォールバック
    Agent = None  # type: ignore[assignment,misc]
    BedrockModel = None  # type: ignore[assignment,misc]

from ..config import config
from ..models.persona import Persona
from ..models.message import Message


class AgentServiceError(Exception):
    """Agent Service関連のエラー"""

    pass


class AgentInitializationError(AgentServiceError):
    """エージェント初期化関連のエラー"""

    pass


class AgentCommunicationError(AgentServiceError):
    """エージェント通信関連のエラー"""

    pass


class PersonaAgent:
    """
    個別のペルソナを表現するAIエージェント
    """

    def __init__(self, persona: Persona, system_prompt: str, agent: Any):
        """
        Initialize persona agent

        Args:
            persona: ペルソナオブジェクト
            system_prompt: システムプロンプト
            agent: Strands Agentインスタンス
        """
        self.persona = persona
        self.system_prompt = system_prompt
        self.agent = agent
        self.logger = logging.getLogger(__name__)
        self._document_contents: List[Dict[str, Any]] = []

    def set_document_contents(self, document_contents: List[Dict[str, Any]]) -> None:
        """
        マルチモーダルドキュメントコンテンツを設定

        Args:
            document_contents: Strands Agent SDK用のContentBlockリスト
                画像の場合: {"image": {"format": "png", "source": {"bytes": bytes}}}
                ドキュメントの場合: {"document": {"name": str, "format": str, "source": {"bytes": bytes}}}
        """
        self._document_contents = document_contents or []
        self.logger.info(
            f"ペルソナ {self.persona.name} に {len(self._document_contents)} 件のドキュメントを設定しました"
        )

    def respond(
        self, prompt: str, context: List[Message] | None = None, include_documents: bool = True
    ) -> str:
        """
        プロンプトに対して応答を生成

        Args:
            prompt: 発言を促すプロンプト
            context: これまでの議論コンテキスト
            include_documents: ドキュメントを含めるかどうか（デフォルト: True、最初の呼び出しのみ）

        Returns:
            str: 生成された発言

        Raises:
            AgentCommunicationError: エージェント通信エラー
        """
        try:
            # コンテキストを含めたプロンプトを構築
            full_prompt = self._build_prompt_with_context(prompt, context)

            # マルチモーダルコンテンツがある場合はContentBlockリストとして渡す
            if include_documents and self._document_contents:
                # テキストとドキュメントを組み合わせたContentBlockリストを作成
                content_blocks = [{"text": full_prompt}] + self._document_contents
                result = self.agent(content_blocks)
                # ドキュメントは最初の呼び出しでのみ渡す（会話履歴に残るため）
                self._document_contents = []
            else:
                # テキストのみの場合
                result = self.agent(full_prompt)

            # AgentResultからテキストコンテンツを正しく抽出
            response = self._extract_text_from_result(result, self.agent)

            self.logger.info(f"ペルソナ {self.persona.name} が応答を生成しました")
            return response

        except Exception as e:
            error_msg = (
                f"ペルソナエージェント {self.persona.name} の応答生成に失敗: {e}"
            )
            self.logger.error(error_msg)
            raise AgentCommunicationError(error_msg)

    def _extract_text_from_result(self, result: Any, agent: Any = None) -> str:
        """
        AgentResultからテキストコンテンツを抽出

        Strands Agent SDKの結果オブジェクトから、実際のテキスト応答を取得する。
        ツール呼び出しがある場合、result.messageは空になるため、
        エージェントの会話履歴から最新のアシスタントメッセージを取得する。

        Args:
            result: Strands Agent SDKのAgentResult
            agent: Strands Agentインスタンス（会話履歴アクセス用）

        Returns:
            str: 抽出されたテキストコンテンツ
        """
        try:
            text_parts = []

            # 方法1: result.message["content"]から直接テキストを抽出
            if hasattr(result, "message") and result.message:
                content = result.message.get("content", [])
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        text_parts.append(block["text"])

            # 方法2: result.messageが空の場合、エージェントの会話履歴から取得
            # ツール使用時はresult.messageが空になるため、agent.messagesを確認
            if not text_parts and agent and hasattr(agent, "messages"):
                # 最新のアシスタントメッセージからテキストを抽出
                for msg in reversed(agent.messages):
                    if msg.get("role") == "assistant":
                        msg_content = msg.get("content", [])
                        for block in msg_content:
                            if isinstance(block, dict) and "text" in block:
                                text_parts.append(block["text"])
                        # 最新のアシスタントメッセージのみ処理
                        if text_parts:
                            break

            if text_parts:
                return "\n".join(text_parts)

            # フォールバック: str()で変換
            self.logger.warning(
                "テキストブロックが見つかりません、str()でフォールバック"
            )
            return str(result)
        except Exception as e:
            self.logger.warning(f"テキスト抽出に失敗、フォールバック使用: {e}")
            return str(result)

    def _build_prompt_with_context(
        self, prompt: str, context: List[Message] | None = None
    ) -> str:
        """
        コンテキストを含めたプロンプトを構築

        コンテキストはFacilitatorAgent.create_prompt_for_persona()で構築済みのため、
        二重付加を防止しpromptをそのまま返す。

        Args:
            prompt: 基本プロンプト（構築済み）
            context: 議論コンテキスト（未使用、後方互換性のため残す）

        Returns:
            str: プロンプト（そのまま）
        """
        return prompt

    def clear_conversation_history(self) -> None:
        """Strands Agent内部の会話履歴をクリア（システムプロンプトは保持）"""
        if self.agent and hasattr(self.agent, "messages"):
            self.agent.messages.clear()
            self.logger.info(f"ペルソナ {self.persona.name} の会話履歴をクリアしました")

    def get_persona_id(self) -> str:
        """ペルソナIDを取得"""
        return self.persona.id

    def get_persona_name(self) -> str:
        """ペルソナ名を取得"""
        return self.persona.name

    def dispose(self) -> None:
        """
        エージェントリソースを解放
        メモリリークを防ぐためにエージェント使用後に呼び出す
        """
        try:
            # Strands Agentのリソース解放（もしdisposeメソッドがあれば）
            if hasattr(self.agent, "dispose"):
                self.agent.dispose()
            elif hasattr(self.agent, "close"):
                self.agent.close()

            # 参照をクリア
            self.agent = None
            self.logger.info(
                f"ペルソナエージェント {self.persona.name} のリソースを解放しました"
            )

        except Exception as e:
            self.logger.warning(
                f"ペルソナエージェント {self.persona.name} のリソース解放中にエラー: {e}"
            )


class FacilitatorAgent:
    """
    議論を進行管理するファシリテータエージェント
    """

    def __init__(self, rounds: int, additional_instructions: str, agent: Any):
        """
        Initialize facilitator agent

        Args:
            rounds: 議論のラウンド数
            additional_instructions: 追加の指示
            agent: Strands Agentインスタンス
        """
        self.rounds = rounds
        self.additional_instructions = additional_instructions
        self.agent = agent
        self.current_round = 0
        self.logger = logging.getLogger(__name__)

    def start_discussion(self, topic: str, persona_agents: List[PersonaAgent]) -> str:
        """
        議論を開始し、最初の発言者を選択

        Args:
            topic: 議論テーマ
            persona_agents: 参加ペルソナエージェントリスト

        Returns:
            str: 議論開始メッセージ
        """
        persona_names = [agent.get_persona_name() for agent in persona_agents]

        start_message = (
            f"議論を開始します。テーマは「{topic}」です。\n"
            f"参加者: {', '.join(persona_names)}\n"
            f"ラウンド数: {self.rounds}"
        )

        self.logger.info(f"ファシリテータが議論を開始しました: {topic}")
        return start_message

    def select_next_speaker(
        self, persona_agents: List[PersonaAgent], spoken_in_round: List[str]
    ) -> Optional[PersonaAgent]:
        """
        次の発言者をランダムに選択

        Args:
            persona_agents: 参加ペルソナエージェントリスト
            spoken_in_round: 現在のラウンドで既に発言したペルソナIDリスト

        Returns:
            Optional[PersonaAgent]: 選択されたペルソナエージェント
        """
        # まだ発言していないペルソナをフィルタリング
        available_agents = [
            agent
            for agent in persona_agents
            if agent.get_persona_id() not in spoken_in_round
        ]

        if not available_agents:
            return None

        # ランダムに選択
        selected_agent = random.choice(available_agents)
        self.logger.info(f"次の発言者を選択: {selected_agent.get_persona_name()}")
        return selected_agent

    def summarize_round(
        self, round_number: int, round_messages: List[Message], topic: str,
        previous_summaries: List[str] | None = None,
    ) -> str:
        """
        ラウンドの議論を要約

        Args:
            round_number: ラウンド番号
            round_messages: そのラウンドのメッセージリスト
            topic: 議論トピック
            previous_summaries: 過去ラウンドの要約リスト

        Returns:
            str: ラウンドの要約

        Raises:
            AgentCommunicationError: エージェント通信エラー
        """
        try:
            # ラウンドの発言のみを抽出（ファシリテータの発言は除く）
            statements = [
                msg
                for msg in round_messages
                if msg.message_type == "statement" and msg.persona_id != "facilitator"
            ]

            if not statements:
                return f"ラウンド{round_number}では発言がありませんでした。"

            # 発言内容を整理
            statements_text = "\n".join(
                [f"- {msg.persona_name}: {msg.content}" for msg in statements]
            )

            # プロンプト構築
            parts = [f"議論テーマ「{topic}」のラウンド{round_number}が完了しました。\n"]

            # 過去ラウンドの要約を含める
            if previous_summaries:
                parts.append("## これまでの議論の流れ")
                for i, summary in enumerate(previous_summaries, 1):
                    parts.append(f"ラウンド{i}: {summary}")
                parts.append("")

            parts.append(f"## ラウンド{round_number}の発言")
            parts.append(statements_text)
            parts.append("")
            parts.append(
                "以下の観点で簡潔に要約してください:\n"
                "- 各参加者の主要な意見や立場\n"
                "- 参加者間の共通点や対立点\n"
                "- まだ掘り下げられていない重要な観点\n"
                "- 次のラウンドで各参加者に答えてほしい具体的な問い（1-2個）\n"
                "3-5文で要約し、最後に問いかけで締めてください。"
            )

            prompt = "\n".join(parts)

            result = self.agent(prompt)
            summary = self._extract_text_from_result(result, self.agent)
            self.logger.info(f"ラウンド{round_number}の議論を要約しました")
            return summary

        except Exception as e:
            error_msg = f"ラウンド{round_number}の要約に失敗: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(error_msg)

    def create_prompt_for_persona(
        self, persona_agent: PersonaAgent, topic: str, context: List[Message],
        round_summaries: List[str] | None = None,
        latest_facilitator_message: str | None = None,
    ) -> str:
        """
        ペルソナエージェントへの発言促進プロンプトを生成

        Args:
            persona_agent: 対象ペルソナエージェント
            topic: 議論テーマ
            context: 直近の発言メッセージ（all_messages[-3:]）
            round_summaries: 各ラウンドの要約リスト
            latest_facilitator_message: ファシリテータの最新要約（問いかけ含む）

        Returns:
            str: 発言促進プロンプト
        """
        current = self.current_round
        total = self.rounds

        if not context and not round_summaries:
            # ラウンド1・最初の発言
            prompt = (
                f"議論テーマ「{topic}」について、あなたの立場から率直に意見を述べてください。\n"
                f"あなたの実体験や生活実感に基づいて、具体的なエピソードを交えて話してください。"
            )
        else:
            parts = [f"「{topic}」についての議論を続けてください。\n"]

            # 要約コンテキスト
            if round_summaries:
                parts.append("## これまでの議論の要約")
                for i, summary in enumerate(round_summaries, 1):
                    parts.append(f"ラウンド{i}: {summary}")
                parts.append("")

            # ファシリテータからの問いかけ（常に表示）
            if latest_facilitator_message:
                parts.append("## ファシリテータからの問いかけ")
                parts.append(latest_facilitator_message)
                parts.append("")

            # 直近の生発言（ファシリテータ以外）
            if context:
                recent_statements = [
                    msg for msg in context if msg.persona_id != "facilitator"
                ][-3:]
                if recent_statements:
                    parts.append("## 直近の発言")
                    for msg in recent_statements:
                        parts.append(f"- {msg.persona_name}: {msg.content}")
                    parts.append("")

            # ラウンドフェーズ別の指示
            if current <= total * 0.3:
                parts.append(
                    "このラウンドでは、他の参加者の意見に対してあなたが同意できる点・できない点を"
                    "率直に述べてください。「なぜそう思うのか」を具体的な経験に基づいて説明してください。"
                )
            elif current < total:
                parts.append(
                    "議論が深まってきました。他の参加者の意見を踏まえて、あなたの考えに変化はありますか？"
                    "新たに気づいたことや、まだ議論されていない重要な観点があれば提起してください。"
                )
            else:
                parts.append(
                    "最終ラウンドです。これまでの議論を踏まえて、あなたが最も重要だと感じたポイントと、"
                    "具体的にどうすべきかについて、あなたの立場から結論を述べてください。"
                )

            if latest_facilitator_message:
                parts.append("\nファシリテータの問いかけにも必ず触れてください。")

            prompt = "\n".join(parts)

        return prompt

    def should_continue(self) -> bool:
        """議論を継続すべきか判定"""
        return self.current_round < self.rounds

    def increment_round(self) -> None:
        """ラウンドをインクリメント"""
        self.current_round += 1
        self.logger.info(f"ラウンド {self.current_round}/{self.rounds} に進みました")

    def clear_conversation_history(self) -> None:
        """Strands Agent内部の会話履歴をクリア（システムプロンプトは保持）"""
        if self.agent and hasattr(self.agent, "messages"):
            self.agent.messages.clear()
            self.logger.info("ファシリテータの会話履歴をクリアしました")

    def _extract_text_from_result(self, result: Any, agent: Any = None) -> str:
        """
        AgentResultからテキストコンテンツを抽出

        Strands Agent SDKの結果オブジェクトから、実際のテキスト応答を取得する。
        ツール呼び出しがある場合、result.messageは空になるため、
        エージェントの会話履歴から最新のアシスタントメッセージを取得する。

        Args:
            result: Strands Agent SDKのAgentResult
            agent: Strands Agentインスタンス（会話履歴アクセス用）

        Returns:
            str: 抽出されたテキストコンテンツ
        """
        try:
            text_parts = []

            # 方法1: result.message["content"]から直接テキストを抽出
            if hasattr(result, "message") and result.message:
                content = result.message.get("content", [])
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        text_parts.append(block["text"])

            # 方法2: result.messageが空の場合、エージェントの会話履歴から取得
            if not text_parts and agent and hasattr(agent, "messages"):
                # 最新のアシスタントメッセージからテキストを抽出
                for msg in reversed(agent.messages):
                    if msg.get("role") == "assistant":
                        msg_content = msg.get("content", [])
                        for block in msg_content:
                            if isinstance(block, dict) and "text" in block:
                                text_parts.append(block["text"])
                        # 最新のアシスタントメッセージのみ処理
                        if text_parts:
                            break

            if text_parts:
                return "\n".join(text_parts)

            # フォールバック: str()で変換
            self.logger.warning(
                "テキストブロックが見つかりません、str()でフォールバック"
            )
            return str(result)
        except Exception as e:
            self.logger.warning(f"テキスト抽出に失敗、フォールバック使用: {e}")
            return str(result)

    def dispose(self) -> None:
        """
        ファシリテータエージェントリソースを解放
        メモリリークを防ぐためにエージェント使用後に呼び出す
        """
        try:
            # Strands Agentのリソース解放（もしdisposeメソッドがあれば）
            if hasattr(self.agent, "dispose"):
                self.agent.dispose()
            elif hasattr(self.agent, "close"):
                self.agent.close()

            # 参照をクリア
            self.agent = None
            self.logger.info("ファシリテータエージェントのリソースを解放しました")

        except Exception as e:
            self.logger.warning(
                f"ファシリテータエージェントのリソース解放中にエラー: {e}"
            )


class AgentService:
    """
    Strands Agent SDKを使用したエージェント管理サービス
    """

    def __init__(self) -> None:
        """Initialize agent service"""
        self.logger = logging.getLogger(__name__)

        # Strands SDKの利用可能性をチェック
        if Agent is None or BedrockModel is None:
            raise AgentInitializationError(
                "Strands Agent SDKがインストールされていません。"
                "pip install strands-agents を実行してください。"
            )

        self.logger.info("Agent Serviceを初期化しました")

    def _create_tool_logging_callback(self, agent_name: str) -> Any:
        """ツールコールをログするコールバックハンドラーを作成"""
        from strands.handlers.callback_handler import PrintingCallbackHandler

        logger = self.logger

        class ToolLoggingCallback(PrintingCallbackHandler):
            def on_tool_start(self, tool: Any, input_data: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                input_str = str(input_data)[:500]
                logger.info(
                    f"[{agent_name}] ツール開始: {tool_name} | 入力: {input_str}"
                )

            def on_tool_end(self, tool: Any, result: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                result_str = str(result)[:1000]
                logger.info(
                    f"[{agent_name}] ツール完了: {tool_name} | 結果: {result_str}"
                )

            def on_tool_error(self, tool: Any, error: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                logger.error(
                    f"[{agent_name}] ツールエラー: {tool_name} | エラー: {error}"
                )

        return ToolLoggingCallback()

    def _create_bedrock_model(self) -> Any:
        """
        Bedrock モデルインスタンスを作成

        Returns:
            BedrockModel: Bedrockモデルインスタンス

        Raises:
            AgentInitializationError: モデル作成エラー
        """
        try:
            # AWS認証情報を取得
            credentials = config.get_aws_credentials()

            # None の値を除去
            filtered_credentials = {
                k: v
                for k, v in credentials.items()
                if v is not None and k != "region_name"
            }

            # Bedrockモデルを作成
            model = BedrockModel(
                model_id=config.AGENT_MODEL_ID,
                region_name=config.AWS_REGION,
                **filtered_credentials,
            )

            self.logger.info(f"Bedrockモデルを作成しました: {config.AGENT_MODEL_ID}")
            return model

        except Exception as e:
            error_msg = f"Bedrockモデルの作成に失敗: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(error_msg)

    def create_persona_agent(
        self,
        persona: Persona,
        system_prompt: str,
        enable_memory: bool = False,
        session_id: Optional[str] = None,
        additional_tools: Optional[List] = None,
        memory_mode: str = "full",
        # 後方互換性のため残すが使用しない
        memory_service: Optional[Any] = None,
    ) -> PersonaAgent:
        """
        ペルソナエージェントを作成

        AgentCoreMemorySessionManagerを使用して、STM（短期記憶）とLTM（長期記憶）を
        自動管理する。これはStrands Agent SDKの推奨方式。

        Args:
            persona: ペルソナオブジェクト
            system_prompt: システムプロンプト
            enable_memory: 長期記憶を有効にするか（デフォルト: False）
            session_id: 議論セッションID（enable_memory=Trueの場合必須）
            additional_tools: 追加のツールリスト（オプション）
            memory_mode: メモリモード（デフォルト: "full"）
                - "full": 検索 + 保存
                - "retrieve_only": 検索のみ（保存しない）
                - "disabled": メモリ機能無効
            memory_service: 非推奨（後方互換性のため残す、使用しない）

        Returns:
            PersonaAgent: 作成されたペルソナエージェント

        Raises:
            AgentInitializationError: エージェント作成エラー
        """
        try:
            # ツールを準備
            tools = []

            # 追加ツールを追加
            if additional_tools:
                tools.extend([t for t in additional_tools if t is not None])

            # Bedrockモデルを作成
            model = self._create_bedrock_model()

            # セッションマネージャーを準備（メモリが有効な場合）
            session_manager = None
            effective_memory_mode = memory_mode if enable_memory else "disabled"

            if enable_memory and session_id and effective_memory_mode != "disabled":
                try:
                    from .memory.session_manager_factory import (
                        create_agentcore_session_manager,
                        is_memory_enabled,
                    )

                    if is_memory_enabled():
                        session_manager = create_agentcore_session_manager(
                            actor_id=persona.id,
                            session_id=session_id,
                            memory_mode=effective_memory_mode,  # type: ignore[arg-type]
                        )

                        if session_manager:
                            mode_label = (
                                "retrieve_only"
                                if effective_memory_mode == "retrieve_only"
                                else "full"
                            )
                            self.logger.info(
                                f"ペルソナ {persona.name} にセッションマネージャーを設定しました "
                                f"(mode={mode_label})"
                            )
                        else:
                            self.logger.warning(
                                f"ペルソナ {persona.name}: セッションマネージャーの作成に失敗しました。"
                                "メモリなしでエージェントを作成します。"
                            )
                    else:
                        self.logger.info(
                            f"ペルソナ {persona.name}: 長期記憶は設定で無効化されています"
                        )

                except Exception as e:
                    self.logger.warning(
                        f"ペルソナ {persona.name}: セッションマネージャー作成エラー: {e}. "
                        "メモリなしでエージェントを作成します。"
                    )
            elif enable_memory and not session_id:
                self.logger.warning(
                    f"ペルソナ {persona.name}: enable_memory=Trueですが、"
                    "session_idが指定されていません。メモリなしでエージェントを作成します。"
                )

            # Agentを作成
            agent_kwargs = {
                "name": persona.name,
                "system_prompt": system_prompt,
                "model": model,
                "callback_handler": self._create_tool_logging_callback(persona.name),
            }

            # ツールを設定
            if tools:
                agent_kwargs["tools"] = tools
                self.logger.info(
                    f"ペルソナ {persona.name} にツールを登録: {len(tools)}個"
                )

            if session_manager:
                agent_kwargs["session_manager"] = session_manager

            agent = Agent(**agent_kwargs)

            # PersonaAgentを作成
            persona_agent = PersonaAgent(persona, system_prompt, agent)

            memory_status = "disabled"
            if session_manager:
                memory_status = effective_memory_mode

            self.logger.info(
                f"ペルソナエージェントを作成しました: {persona.name} "
                f"(memory={memory_status})"
            )
            return persona_agent

        except Exception as e:
            error_msg = f"ペルソナエージェント {persona.name} の作成に失敗: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(error_msg)

    def create_facilitator_agent(
        self, rounds: int, additional_instructions: str = ""
    ) -> FacilitatorAgent:
        """
        ファシリテータエージェントを作成

        Args:
            rounds: ラウンド数
            additional_instructions: 追加の指示

        Returns:
            FacilitatorAgent: 作成されたファシリテータエージェント

        Raises:
            AgentInitializationError: エージェント作成エラー
        """
        try:
            # システムプロンプトを生成
            system_prompt = self._generate_facilitator_system_prompt(
                rounds, additional_instructions
            )

            # Bedrockモデルを作成
            model = self._create_bedrock_model()

            # Agentを作成
            agent = Agent(name="Facilitator", system_prompt=system_prompt, model=model)

            # FacilitatorAgentを作成
            facilitator_agent = FacilitatorAgent(rounds, additional_instructions, agent)

            self.logger.info(
                f"ファシリテータエージェントを作成しました (ラウンド数: {rounds})"
            )
            return facilitator_agent

        except Exception as e:
            error_msg = f"ファシリテータエージェントの作成に失敗: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(error_msg)

    def _generate_facilitator_system_prompt(
        self, rounds: int, additional_instructions: str
    ) -> str:
        """
        ファシリテータ用システムプロンプトを生成

        Args:
            rounds: ラウンド数
            additional_instructions: 追加の指示

        Returns:
            str: 生成されたシステムプロンプト
        """
        prompt = f"""あなたは議論のファシリテータです。{rounds}ラウンドの議論を進行管理します。

# 役割
- 議論の進行を管理し、深い洞察を引き出す
- 各ラウンドの議論を要約し、次の議論の方向性を示す
- 表面的な合意に留まらず、本質的な議論を促進する

# 進行方針
- 各ラウンドで全ペルソナが1回ずつ発言する
- 発言順序はランダムに決定される
- ラウンド終了後に、議論全体を要約し次ラウンドへの問いかけを行う
- 議論が表面的になっていたら「なぜそう思うのか」「具体的にはどういう場面か」と掘り下げる

# ラウンド要約のポイント
- 各参加者の主要な意見や立場を簡潔にまとめる
- 共通点や対立点を明確にする
- まだ掘り下げられていない重要な観点を指摘する
- 各ペルソナに次のラウンドで答えてほしい具体的な問いを提示する
- 3-5文で要約し、最後に問いかけで締める
- 最終ラウンドでは、議論全体の結論と実践的な示唆をまとめる
"""

        if additional_instructions:
            prompt += f"\n# 追加の指示\n{additional_instructions}\n"

        return prompt

    def generate_persona_system_prompt(self, persona: Persona) -> str:
        """
        ペルソナからシステムプロンプトを自動生成

        Args:
            persona: ペルソナオブジェクト

        Returns:
            str: 生成されたシステムプロンプト
        """
        persona_dict = asdict(persona)

        prompt = f"""あなたは{persona_dict["name"]}として議論に参加します。

# あなたのプロフィール
- 名前: {persona_dict["name"]}
- 年齢: {persona_dict["age"]}歳
- 職業: {persona_dict["occupation"]}

# 背景
{persona_dict["background"]}

# 価値観
{chr(10).join(f"- {value}" for value in persona_dict["values"])}

# 抱えている課題
{chr(10).join(f"- {pain}" for pain in persona_dict["pain_points"])}

# 目標・願望
{chr(10).join(f"- {goal}" for goal in persona_dict["goals"])}

# 議論での振る舞い
- あなたの立場から率直に意見を述べてください。同意できない点は遠慮なく指摘してください
- 抽象的な意見ではなく、あなたの実体験や生活実感に基づいた具体的なエピソードを交えて話してください
- 他の参加者の意見に違和感があれば、なぜそう感じるのか正直に伝えてください
- 「なんとなく」ではなく、あなたの価値観や課題に紐づけて理由を明確にしてください

# 重要な注意事項
- あなたは{persona_dict["name"]}です。この人格を一貫して維持してください
- {persona_dict["age"]}歳の{persona_dict["occupation"]}として自然な口調で話してください
- 発言は実際の会話のような口語体にしてください（##などの見出しは不要です）
"""

        return prompt

    def create_persona_agent_with_kb(
        self,
        persona: Persona,
        system_prompt: str,
        knowledge_base_id: str,
        kb_name: str,
        kb_description: str = "",
        metadata_filters: Dict[str, str] | None = None,
        enable_memory: bool = False,
        session_id: Optional[str] = None,
        memory_mode: str = "full",
    ) -> PersonaAgent:
        """
        ナレッジベース連携付きペルソナエージェントを作成

        Args:
            persona: ペルソナオブジェクト
            system_prompt: システムプロンプト
            knowledge_base_id: Bedrock Knowledge Base ID
            kb_name: ナレッジベース名
            metadata_filters: メタデータフィルタ
            enable_memory: 長期記憶を有効にするか
            session_id: セッションID
            memory_mode: メモリモード

        Returns:
            PersonaAgent: ナレッジベース連携付きペルソナエージェント
        """
        from .knowledge_base.kb_tools import create_kb_retrieval_tool

        # KB検索ツールを作成
        kb_tool = create_kb_retrieval_tool(
            knowledge_base_id=knowledge_base_id,
            metadata_filters=metadata_filters or {},
            region=config.AWS_REGION,
        )

        # システムプロンプトにKB利用の指示を追加
        filter_desc = ""
        if metadata_filters:
            filter_desc = "（フィルタ: " + ", ".join(
                f"{k}={v}" for k, v in metadata_filters.items()
            ) + "）"

        desc_line = ""
        if kb_description:
            desc_line = f"\n内容: {kb_description}"

        kb_instruction = f"""

# 【ナレッジベース連携】

あなたにはナレッジベース「{kb_name}」{filter_desc}を検索するツール（search_knowledge_base）が提供されています。{desc_line}

## 使用ルール
1. 議論トピックに関連する具体的な情報（商品情報、仕様、データなど）が必要な場合、ナレッジベースを検索してください
2. 検索結果を参考にしつつ、あなた自身のペルソナとしての視点で発言してください
3. 検索結果をそのまま読み上げるのではなく、自分の言葉で自然に組み込んでください
"""
        enhanced_prompt = system_prompt + kb_instruction

        return self.create_persona_agent(
            persona=persona,
            system_prompt=enhanced_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            additional_tools=[kb_tool],
            memory_mode=memory_mode,
        )

    def create_persona_agent_with_dataset(
        self,
        persona: Persona,
        system_prompt: str,
        dataset_bindings: List[Dict],
        datasets: List[Any],
        enable_memory: bool = False,
        session_id: Optional[str] = None,
        memory_mode: str = "full",
    ) -> PersonaAgent:
        """
        データセット連携付きペルソナエージェントを作成

        Args:
            persona: ペルソナオブジェクト
            system_prompt: システムプロンプト
            dataset_bindings: 紐付け情報リスト [{"dataset_id": "...", "binding_keys": {...}}]
            datasets: データセットオブジェクトリスト
            enable_memory: 長期記憶を有効にするか
            session_id: セッションID
            memory_mode: メモリモード

        Returns:
            PersonaAgent: データセット連携付きペルソナエージェント
        """
        from .mcp_server_manager import get_mcp_manager

        # データセット情報をシステムプロンプトに追加
        enhanced_prompt = self._enhance_prompt_with_dataset_info(
            system_prompt, dataset_bindings, datasets
        )

        # MCPクライアントからツールを取得
        additional_tools = []
        mcp_manager = get_mcp_manager()

        # MCPサーバーが起動していない場合は自動起動
        if not mcp_manager.is_running():
            self.logger.info("MCP Server not running, starting automatically...")
            if mcp_manager.start():
                self.logger.info("MCP Server started automatically")
            else:
                self.logger.warning("Failed to start MCP Server automatically")

        self.logger.info(f"MCP Manager running: {mcp_manager.is_running()}")
        if mcp_manager.is_running():
            mcp_tools = mcp_manager.get_tools()
            if mcp_tools:
                additional_tools.extend(mcp_tools)
                self.logger.info(f"Added {len(mcp_tools)} MCP tools for {persona.name}")

        # 通常のエージェント作成
        return self.create_persona_agent(
            persona=persona,
            system_prompt=enhanced_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            additional_tools=additional_tools if additional_tools else None,
            memory_mode=memory_mode,
        )

    def _enhance_prompt_with_kb_info(
        self, base_prompt: str, kb_name: str, kb_description: str, metadata_filters: Dict[str, str] | None = None
    ) -> str:
        """ナレッジベース情報をシステムプロンプトに追加"""
        filter_desc = ""
        if metadata_filters:
            filter_desc = "（フィルタ: " + ", ".join(
                f"{k}={v}" for k, v in metadata_filters.items()
            ) + "）"

        desc_line = ""
        if kb_description:
            desc_line = f"\n内容: {kb_description}"

        return base_prompt + f"""

# 【ナレッジベース連携】

あなたにはナレッジベース「{kb_name}」{filter_desc}を検索するツール（search_knowledge_base）が提供されています。{desc_line}

## 使用ルール
1. 議論トピックに関連する具体的な情報（商品情報、仕様、データなど）が必要な場合、ナレッジベースを検索してください
2. 検索結果を参考にしつつ、あなた自身のペルソナとしての視点で発言してください
3. 検索結果をそのまま読み上げるのではなく、自分の言葉で自然に組み込んでください
"""

    def _enhance_prompt_with_dataset_info(
        self, base_prompt: str, bindings: List[Dict], datasets: List[Any]
    ) -> str:
        """
        データセット情報をシステムプロンプトに追加
        """
        if not bindings or not datasets:
            return base_prompt

        # データセットIDからデータセットを検索するマップ
        dataset_map = {d.id: d for d in datasets}

        dataset_info_parts = []
        for binding in bindings:
            dataset = dataset_map.get(binding.get("dataset_id"))
            if not dataset:
                continue

            binding_keys = binding.get("binding_keys", {})
            keys_str = ", ".join(f"{k}='{v}'" for k, v in binding_keys.items())
            filter_condition = " AND ".join(
                f"{k} = '{v}'" for k, v in binding_keys.items()
            )

            columns_str = ", ".join(c.name for c in dataset.columns)

            dataset_info_parts.append(f"""
### データセット: {dataset.name}
- 説明: {dataset.description}
- あなたの識別キー: {keys_str}
- S3パス: {dataset.s3_path}
- カラム: {columns_str}
- 行数: {dataset.row_count}行

あなたのデータを取得するクエリ:
```sql
SELECT * FROM read_csv('{dataset.s3_path}') WHERE {filter_condition};
```
""")

        if not dataset_info_parts:
            return base_prompt

        dataset_section = (
            """
# 【重要】外部データセットへのアクセス - 必ず使用すること

あなたには外部データセットにアクセスするためのツール（query）が提供されています。
このツールを使って、あなた自身の購買履歴や経験に関する具体的なデータを取得できます。

## ★★★ 絶対に守るべきルール ★★★

1. **購買履歴、過去の経験、具体的な商品名について話す場合は、必ず最初にデータセットを参照してください**
2. **データを参照せずに購買履歴や具体的な経験を話すことは禁止です**
3. **ツールを使用する際は、必ず以下の手順に従ってください**

## ツール使用手順（この順番で実行）

### ステップ1: AWS認証の設定（最初の1回のみ必須）
最初にデータにアクセスする前に、以下のSQLを実行してAWS認証を設定してください：
```sql
CREATE SECRET IF NOT EXISTS aws_secret (TYPE S3, PROVIDER CREDENTIAL_CHAIN);
```

### ステップ2: データの取得
認証設定後、以下のようなクエリでデータを取得してください：
```sql
SELECT * FROM read_csv('s3://バケット/パス.csv') WHERE 条件;
```

## 利用可能なデータセット
"""
            + "".join(dataset_info_parts)
            + """

## ツール使用時の注意事項

- **認証エラーが発生した場合**: ステップ1の認証SQLを再実行してください
- **データが見つからない場合**: 条件を確認し、正しい識別キーを使用しているか確認してください
- **ツールが応答しない場合**: 少し待ってから再試行してください

## 回答の仕方

1. ユーザーから購買履歴や経験について質問されたら、まずツールでデータを取得
2. 取得したデータに基づいて、具体的な商品名、日付、金額を含めて回答
3. データがない場合のみ、「データを確認しましたが、該当する記録がありませんでした」と正直に伝える

"""
        )

        return base_prompt + dataset_section

    def create_market_research_agent(self) -> Agent:
        """
        市場調査レポート分析用のエージェントを作成

        Returns:
            Agent: 市場調査分析エージェント

        Raises:
            AgentInitializationError: エージェント作成に失敗した場合
        """
        if Agent is None or BedrockModel is None:
            raise AgentInitializationError(
                "Strands Agent SDKがインストールされていません"
            )

        system_prompt = """あなたは提供される市場調査レポート、顧客分析資料またはマーケティングレポートを分析し、複数の異なるペルソナを生成する専門家です。

# 役割
これら資料、レポートの内容を詳細に分析し、以下の観点から異なる顧客セグメントを識別してペルソナを生成してください：

1. **市場セグメント**: レポートから読み取れる異なる顧客層・ターゲット層
2. **行動パターン**: 購買行動、利用パターン、意思決定プロセスの違い
3. **デモグラフィック**: 年齢、職業、ライフスタイル、価値観の違い

# 分析アプローチ
- レポート全体を俯瞰し、複数の異なる顧客像を抽出
- 各ペルソナが明確に区別できるよう、特徴的な違いを強調
- 実在しそうなリアルで具体的なペルソナを作成
- 日本市場を想定した日本人のペルソナを生成

# 出力形式
**必ず以下のJSON配列形式のみで出力してください。説明文、前置き、後書きは一切不要です：**

[
    {
        "name": "田中 花子",
        "age": 35,
        "occupation": "会社員（マーケティング部）",
        "background": "東京都在住。大学卒業後、現在の会社に就職し10年目。一人暮らしで、仕事とプライベートのバランスを重視している。",
        "values": ["効率性を重視する", "新しいことへの挑戦を大切にする", "人とのつながりを大事にする"],
        "pain_points": ["時間管理が難しい", "情報過多で選択に迷う", "仕事のストレスが多い"],
        "goals": ["キャリアアップを目指す", "ワークライフバランスを改善する", "新しいスキルを身につける"]
    },
    {
        "name": "佐藤 健太",
        "age": 28,
        "occupation": "フリーランスエンジニア",
        "background": "神奈川県在住。大学卒業後、IT企業に3年勤務した後、フリーランスとして独立。リモートワーク中心の生活。",
        "values": ["自由な働き方を重視", "技術力の向上を追求", "効率的な時間の使い方"],
        "pain_points": ["収入の不安定さ", "孤独を感じることがある", "自己管理の難しさ"],
        "goals": ["安定した収入源を確保", "技術コミュニティとのつながり", "ワークライフバランスの確立"]
    }
]

# 重要な注意事項
- 各ペルソナは明確に異なる特徴を持つこと
- 日本人の名前を使用（多様な姓と名を使用）
- 年齢は数値のみ（引用符なし）
- 各リスト項目は3-5個程度
- JSON形式を厳密に守り、構文エラーがないようにする
- **出力は上記JSON配列のみで、他の文章は絶対に含めない**
"""

        try:
            model = BedrockModel(
                model_id=config.BEDROCK_MODEL_ID, region=config.AWS_REGION  # type: ignore[call-arg]
            )

            agent = Agent(
                name="MarketResearchAnalyst", model=model, system_prompt=system_prompt
            )

            self.logger.info("市場調査分析エージェントを作成しました")
            return agent

        except Exception as e:
            error_msg = f"市場調査分析エージェント作成エラー: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(error_msg)

    def generate_personas_from_report(
        self, report_text: str, persona_count: int
    ) -> List[Persona]:
        """
        市場調査レポートから複数のペルソナを生成

        Args:
            report_text: 市場調査レポートのテキスト
            persona_count: 生成するペルソナの数（1-10）

        Returns:
            List[Persona]: 生成されたペルソナのリスト

        Raises:
            AgentServiceError: ペルソナ生成に失敗した場合
        """
        if not report_text or not report_text.strip():
            raise AgentServiceError("レポートテキストが空です")

        if persona_count < 1 or persona_count > 10:
            raise AgentServiceError("ペルソナ数は1-10の範囲で指定してください")

        agent = None
        try:
            # 市場調査分析エージェントを作成
            agent = self.create_market_research_agent()

            # プロンプトを構築
            prompt = f"""以下の市場調査レポートを分析し、**{persona_count}個**の異なるペルソナを生成してください。

# 市場調査レポート
{report_text}

# 指示
上記のレポートから、{persona_count}個の明確に異なる顧客ペルソナを抽出してください。
各ペルソナは、市場セグメント、行動パターン、デモグラフィックの観点で区別できるようにしてください。

JSON配列:"""

            # エージェントを実行
            self.logger.info(
                f"市場調査レポートから{persona_count}個のペルソナを生成中..."
            )
            result = agent(prompt)

            # AgentResultからテキストを抽出
            response_text = ""
            try:
                if hasattr(result, "message") and result.message:
                    content = result.message.get("content", [])
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            text_parts.append(block["text"])
                    if text_parts:
                        response_text = "\n".join(text_parts)

                # フォールバック: agent.messagesから取得
                if not response_text and hasattr(agent, "messages"):
                    for msg in reversed(agent.messages):
                        if msg.get("role") == "assistant":
                            msg_content = msg.get("content", [])
                            text_parts = []
                            for block in msg_content:
                                if isinstance(block, dict) and "text" in block:
                                    text_parts.append(block["text"])
                            if text_parts:
                                response_text = "\n".join(text_parts)
                                break

                # 最終フォールバック
                if not response_text:
                    response_text = str(result)
            except Exception as e:
                self.logger.warning(f"テキスト抽出に失敗、フォールバック使用: {e}")
                response_text = str(result)

            self.logger.debug(f"エージェントレスポンス: {response_text[:500]}...")

            # レスポンスをパース
            personas = self._parse_personas_from_response(response_text)

            # 指定された数のペルソナが生成されたか確認
            if len(personas) != persona_count:
                self.logger.warning(
                    f"要求された{persona_count}個ではなく{len(personas)}個のペルソナが生成されました"
                )

            self.logger.info(f"{len(personas)}個のペルソナ生成が完了しました")
            return personas

        except Exception as e:
            error_msg = f"市場調査レポートからのペルソナ生成エラー: {e}"
            self.logger.error(error_msg)
            raise AgentServiceError(error_msg)
        finally:
            # エージェントのクリーンアップ（Strands SDKではdisposeは不要）
            if agent is not None:
                self.logger.debug("市場調査分析エージェントの処理が完了しました")

    def _parse_personas_from_response(self, response: str) -> List[Persona]:
        """
        エージェントのレスポンスから複数のペルソナをパース

        Args:
            response: エージェントのレスポンス文字列

        Returns:
            List[Persona]: パースされたペルソナのリスト

        Raises:
            AgentServiceError: パースに失敗した場合
        """
        import json
        import re

        try:
            # JSON配列部分を抽出
            json_match = re.search(r"\[[\s\S]*\]", response)
            if not json_match:
                raise AgentServiceError("レスポンスからJSON配列を抽出できませんでした")

            json_str = json_match.group(0)
            personas_data = json.loads(json_str)

            if not isinstance(personas_data, list):
                raise AgentServiceError("レスポンスがJSON配列ではありません")

            # 各ペルソナデータをPersonaオブジェクトに変換
            personas = []
            for persona_data in personas_data:
                # 必須フィールドの検証
                required_fields = [
                    "name",
                    "age",
                    "occupation",
                    "background",
                    "values",
                    "pain_points",
                    "goals",
                ]
                for field in required_fields:
                    if field not in persona_data:
                        raise AgentServiceError(
                            f"必須フィールド '{field}' が見つかりません"
                        )

                # Personaオブジェクトを作成
                persona = Persona.create_new(
                    name=persona_data["name"],
                    age=int(persona_data["age"]),
                    occupation=persona_data["occupation"],
                    background=persona_data["background"],
                    values=persona_data["values"],
                    pain_points=persona_data["pain_points"],
                    goals=persona_data["goals"],
                )
                personas.append(persona)

            return personas

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析エラー - レスポンス: {response[:500]}...")
            raise AgentServiceError(f"JSON解析エラー: {e}")
        except Exception as e:
            self.logger.error(f"ペルソナパースエラー: {e}")
            raise AgentServiceError(f"ペルソナパースエラー: {e}")
