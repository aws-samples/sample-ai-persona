"""
Agent Service
Strands Agent SDKを使用したエージェント管理サービス
"""

import queue
import logging
import threading
from typing import List, Dict, Any, Optional, Generator
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
from ..models.demographics import gender_label
from .country_service import country_name
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


def _clear_agent_history(agent: Any, label: str) -> None:
    """Strands Agent内部の会話履歴をクリアする共通ヘルパー。"""
    _logger = logging.getLogger(__name__)
    if agent and hasattr(agent, "messages"):
        agent.messages.clear()
        _logger.info(f"{label} の会話履歴をクリアしました")


def _dispose_agent(agent_ref: Any, label: str) -> None:
    """Strands Agentリソースを解放する共通ヘルパー。解放後 agent_ref は呼び出し側で None にすること。"""
    _logger = logging.getLogger(__name__)
    try:
        if hasattr(agent_ref, "dispose"):
            agent_ref.dispose()
        elif hasattr(agent_ref, "close"):
            agent_ref.close()
        _logger.info(f"{label} のリソースを解放しました")
    except Exception as e:
        _logger.warning(f"{label} のリソース解放中にエラー: {e}")


def _extract_text_from_agent_result(result: Any, agent: Any = None) -> str:
    """
    AgentResultからテキストコンテンツを抽出する共通ヘルパー。

    Strands Agent SDKの結果オブジェクトから実際のテキスト応答を取得する。
    ツール呼び出しがある場合 result.message は空になるため、
    エージェントの会話履歴から最新のアシスタントメッセージを取得する。
    """
    _logger = logging.getLogger(__name__)
    try:
        text_parts = []

        if hasattr(result, "message") and result.message:
            content = result.message.get("content", [])
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    text_parts.append(block["text"])

        if not text_parts and agent and hasattr(agent, "messages"):
            for msg in reversed(agent.messages):
                if msg.get("role") == "assistant":
                    msg_content = msg.get("content", [])
                    for block in msg_content:
                        if isinstance(block, dict) and "text" in block:
                            text_parts.append(block["text"])
                    if text_parts:
                        break

        if text_parts:
            return "\n".join(text_parts)

        _logger.warning("テキストブロックが見つかりません、str()でフォールバック")
        return str(result)
    except Exception as e:
        _logger.warning(f"テキスト抽出に失敗、フォールバック使用: {e}")
        return str(result)


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
        self,
        prompt: str,
        context: List[Message] | None = None,
        include_documents: bool = True,
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
            full_prompt = prompt

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

    def respond_streaming(
        self,
        prompt: str,
        context: List[Message] | None = None,
        include_documents: bool = True,
    ) -> Generator[str, None, None]:
        """
        トークンを逐次yieldする応答生成

        Args:
            prompt: 発言を促すプロンプト
            context: これまでの議論コンテキスト
            include_documents: ドキュメントを含めるかどうか

        Yields:
            str: トークン文字列

        Raises:
            AgentCommunicationError: エージェント通信エラー
        """
        try:
            full_prompt = prompt
            token_queue: queue.Queue[Optional[str]] = queue.Queue()

            class _TokenCapture:
                def __call__(self, **kwargs: Any) -> None:
                    data = kwargs.get("data", "")
                    if data:
                        token_queue.put(data)

            original_handler = self.agent.callback_handler
            self.agent.callback_handler = _TokenCapture()

            agent_error: Optional[Exception] = None

            def _run_agent() -> None:
                nonlocal agent_error
                try:
                    if include_documents and self._document_contents:
                        content_blocks = [
                            {"text": full_prompt}
                        ] + self._document_contents
                        self.agent(content_blocks)
                        self._document_contents = []
                    else:
                        self.agent(full_prompt)
                except Exception as e:
                    agent_error = e
                finally:
                    token_queue.put(None)

            thread = threading.Thread(target=_run_agent, daemon=True)
            thread.start()

            try:
                while True:
                    token = token_queue.get()
                    if token is None:
                        break
                    yield token
            finally:
                thread.join()
                self.agent.callback_handler = original_handler

            if agent_error:
                raise agent_error

            self.logger.info(
                f"ペルソナ {self.persona.name} がストリーミング応答を完了しました"
            )

        except AgentCommunicationError:
            raise
        except Exception as e:
            error_msg = f"ペルソナエージェント {self.persona.name} のストリーミング応答生成に失敗: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(error_msg)

    def _extract_text_from_result(self, result: Any, agent: Any = None) -> str:
        """AgentResultからテキストコンテンツを抽出"""
        return _extract_text_from_agent_result(result, agent)

    def clear_conversation_history(self) -> None:
        """Strands Agent内部の会話履歴をクリア（システムプロンプトは保持）"""
        _clear_agent_history(self.agent, f"ペルソナ {self.persona.name}")

    def get_persona_id(self) -> str:
        """ペルソナIDを取得"""
        return self.persona.id

    def get_persona_name(self) -> str:
        """ペルソナ名を取得"""
        return self.persona.name

    def dispose(self) -> None:
        """エージェントリソースを解放"""
        _dispose_agent(self.agent, f"ペルソナエージェント {self.persona.name}")
        self.agent = None


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

    def clear_conversation_history(self) -> None:
        """Strands Agent内部の会話履歴をクリア（システムプロンプトは保持）"""
        _clear_agent_history(self.agent, "ファシリテータ")

    def _extract_text_from_result(self, result: Any, agent: Any = None) -> str:
        """AgentResultからテキストコンテンツを抽出"""
        return _extract_text_from_agent_result(result, agent)

    def invoke(self, prompt: str) -> str:
        """
        プロンプトを渡してテキスト応答を取得する。

        Args:
            prompt: 入力プロンプト

        Returns:
            生成されたテキスト応答

        Raises:
            AgentCommunicationError: エージェント通信エラー
        """
        try:
            result = self.agent(prompt)
            return self._extract_text_from_result(result, self.agent)
        except Exception as e:
            error_msg = f"ファシリテータ呼び出しに失敗: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(error_msg)

    def invoke_streaming(self, prompt: str) -> Generator[str, None, None]:
        """
        プロンプトを渡してトークンストリーミング応答を取得する。

        Args:
            prompt: 入力プロンプト

        Yields:
            トークン文字列

        Raises:
            AgentCommunicationError: エージェント通信エラー
        """
        try:
            token_queue: queue.Queue[Optional[str]] = queue.Queue()

            class _TokenCapture:
                def __call__(self, **kwargs: Any) -> None:
                    data = kwargs.get("data", "")
                    if data:
                        token_queue.put(data)

            original_handler = self.agent.callback_handler
            self.agent.callback_handler = _TokenCapture()

            agent_error: Optional[Exception] = None

            def _run_agent() -> None:
                nonlocal agent_error
                try:
                    self.agent(prompt)
                except Exception as e:
                    agent_error = e
                finally:
                    token_queue.put(None)

            thread = threading.Thread(target=_run_agent, daemon=True)
            thread.start()

            try:
                while True:
                    token = token_queue.get()
                    if token is None:
                        break
                    yield token
            finally:
                thread.join()
                self.agent.callback_handler = original_handler
                if agent_error:
                    self.logger.error(
                        f"ファシリテータストリーミング中にエージェントエラー（クライアント切断で未送出）: {agent_error}"
                    )

            if agent_error:
                raise agent_error

        except AgentCommunicationError:
            raise
        except Exception as e:
            error_msg = f"ファシリテータストリーミング呼び出しに失敗: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(error_msg)

    def dispose(self) -> None:
        """ファシリテータエージェントリソースを解放"""
        _dispose_agent(self.agent, "ファシリテータエージェント")
        self.agent = None


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
            from botocore.config import Config as BotoConfig

            # AWS認証情報を取得
            credentials = config.get_aws_credentials()

            # None の値を除去
            filtered_credentials = {
                k: v
                for k, v in credentials.items()
                if v is not None and k != "region_name"
            }

            # 一過性の接続エラー（ストリーミング開始時のConnection closed等）対策。
            # ai_serviceと異なり自前のバックオフ機構を持たないため、boto3標準リトライに委ねる
            boto_config = BotoConfig(
                connect_timeout=30,
                read_timeout=300,
                retries={"max_attempts": 3, "mode": "adaptive"},
            )

            # Bedrockモデルを作成
            model = BedrockModel(
                model_id=config.AGENT_MODEL_ID,
                region_name=config.AWS_REGION,
                boto_client_config=boto_config,
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

        # 設定済みのデモグラフィック属性のみプロフィールに追加（表示名へ変換）
        profile_lines = [
            f"- 名前: {persona_dict['name']}",
            f"- 年齢: {persona_dict['age']}歳",
        ]
        if persona.gender:
            profile_lines.append(f"- 性別: {gender_label(persona.gender)}")
        if persona.country:
            location = country_name(persona.country)
            if persona.city:
                location += f"・{persona.city}"
            profile_lines.append(f"- 居住地: {location}")
        elif persona.city:
            profile_lines.append(f"- 居住地: {persona.city}")
        profile_lines.append(f"- 職業: {persona_dict['occupation']}")
        profile_text = "\n".join(profile_lines)

        prompt = f"""あなたは{persona_dict["name"]}として議論に参加します。

# あなたのプロフィール
{profile_text}

# 背景
{persona_dict["background"]}

# 価値観
{chr(10).join(f"- {value}" for value in persona_dict["values"])}

# 抱えている課題
{chr(10).join(f"- {pain}" for pain in persona_dict["pain_points"])}

# 目標・願望
{chr(10).join(f"- {goal}" for goal in persona_dict["goals"])}

# この議論の目的
あなたの率直な意見、本音、具体的な生活体験が求められています。
議論のテーマに記載された目的を意識して発言してください。

# 議論での振る舞い
- あなたの立場から率直に意見を述べてください。同意できない点は遠慮なく指摘してください
- 抽象的な意見ではなく、あなたの実体験や生活実感に基づいた具体的なエピソードを交えて話してください
- 他の参加者の意見に違和感があれば、なぜそう感じるのか正直に伝えてください
- 「なんとなく」ではなく、あなたの価値観や課題に紐づけて理由を明確にしてください
- 不満・懐疑・迷いがあれば隠さず表明してください。無理に肯定的である必要はありません
- 状況や条件によって判断が変わる場合は、その条件を示してください
- あなたのコミュニケーションスタイルに合った強度で意見してください（全員が同じ強さで主張する必要はない）

# 重要な注意事項
- あなたは{persona_dict["name"]}です。この人格を一貫して維持してください
- {persona_dict["age"]}歳の{persona_dict["occupation"]}として自然な口調で話してください
- 発言は実際の会話のような口語体にしてください（##などの見出しは不要です）
- 1回の発言は500文字以内に収めてください。長すぎる発言は避けてください
"""

        return prompt

    def _enhance_prompt_with_kb_info(
        self,
        base_prompt: str,
        kb_name: str,
        kb_description: str,
        metadata_filters: Dict[str, str] | None = None,
    ) -> str:
        """ナレッジベース情報をシステムプロンプトに追加"""
        filter_desc = ""
        if metadata_filters:
            filter_desc = (
                "（フィルタ: "
                + ", ".join(f"{k}={v}" for k, v in metadata_filters.items())
                + "）"
            )

        desc_line = ""
        if kb_description:
            desc_line = f"\n内容: {kb_description}"

        return (
            base_prompt
            + f"""

# 【ナレッジベース連携】

あなたにはナレッジベース「{kb_name}」{filter_desc}を検索するツール（search_knowledge_base）が提供されています。{desc_line}

## 使用ルール
1. 議論トピックに関連する具体的な情報（商品情報、仕様、データなど）が必要な場合、ナレッジベースを検索してください
2. 検索結果を参考にしつつ、あなた自身のペルソナとしての視点で発言してください
3. 検索結果をそのまま読み上げるのではなく、自分の言葉で自然に組み込んでください
"""
        )

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
            columns_str = ", ".join(c.name for c in dataset.columns)

            if binding_keys:
                keys_str = ", ".join(f"{k}='{v}'" for k, v in binding_keys.items())
                filter_condition = " AND ".join(
                    f"{k} = '{v}'" for k, v in binding_keys.items()
                )
                query_example = f"SELECT * FROM read_csv('{dataset.s3_path}') WHERE {filter_condition};"
            else:
                keys_str = "（全行がこのペルソナのデータ）"
                query_example = f"SELECT * FROM read_csv('{dataset.s3_path}');"

            dataset_info_parts.append(f"""
### データセット: {dataset.name}
- 説明: {dataset.description}
- あなたの識別キー: {keys_str}
- S3パス: {dataset.s3_path}
- カラム: {columns_str}
- 行数: {dataset.row_count}行

あなたのデータを取得するクエリ:
```sql
{query_example}
```
""")

        if not dataset_info_parts:
            return base_prompt

        dataset_section = (
            """
# 【重要】外部データセットへのアクセス - 必ず使用すること

あなたには外部データセットにアクセスするためのツール（execute_query）が提供されています。
このツールを使って、あなた自身の購買履歴や経験に関する具体的なデータを取得できます。

## ★★★ 絶対に守るべきルール ★★★

1. **購買履歴、過去の経験、具体的な商品名について話す場合は、必ず最初にデータセットを参照してください**
2. **データを参照せずに購買履歴や具体的な経験を話すことは禁止です**

## データの取得方法

**初回のみ**: 認証設定とデータ取得を1回のクエリにまとめて実行してください：
```sql
CREATE SECRET IF NOT EXISTS aws_secret (TYPE S3, PROVIDER CREDENTIAL_CHAIN);
SELECT * FROM read_csv('s3://バケット/パス.csv') WHERE 条件;
```

**2回目以降**: 認証は設定済みなのでSELECT文だけでOKです：
```sql
SELECT * FROM read_csv('s3://バケット/パス.csv') WHERE 条件;
```

## 利用可能なデータセット
"""
            + "".join(dataset_info_parts)
            + """

## ツール使用時の注意事項

- **認証エラー（403 Forbidden）が出た場合**: CREATE SECRET文を含めて再実行してください
- **データが見つからない場合**: 条件を確認し、正しい識別キーを使用しているか確認してください

## 回答の仕方

1. ユーザーから購買履歴や経験について質問されたら、まずツールでデータを取得
2. 取得したデータに基づいて、具体的な商品名、日付、金額を含めて回答
3. データがない場合のみ、「データを確認しましたが、該当する記録がありませんでした」と正直に伝える

"""
        )

        return base_prompt + dataset_section

    # --- Flexible Persona Generation ---
    #
    # ADR: ペルソナ生成エージェントをクラス化しない理由
    #
    # 決定: ペルソナ生成は PersonaAgent/FacilitatorAgent のようなラッパークラスを作らず、
    #       AgentService のメソッドとして実装する。
    #
    # 背景: PersonaAgent/FacilitatorAgent がクラスになっているのは、議論ループ中に
    #       繰り返し respond()/invoke() を呼び、ドキュメント設定・会話履歴管理・
    #       dispose によるリソース解放が必要なため。
    #
    # 根拠: ペルソナ生成は agent(prompt) → agent.structured_output() の2回呼び出しで
    #       完結する。履歴管理もドキュメント添付も不要で、ラッパーの恩恵がない。
    #       クラス化すると呼び出し側に不要な dispose() 義務が生じるだけで複雑さが増す。
    #

    @staticmethod
    def _extract_thinking_log(agent: Any) -> list[dict[str, str]]:
        """エージェントのメッセージ履歴から思考ログを抽出"""
        log: list[dict[str, str]] = []
        last_tool_name = ""
        for msg in getattr(agent, "messages", []):
            role = msg.get("role", "")
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if "text" in block and role == "assistant":
                    log.append({"type": "thinking", "content": block["text"]})
                elif "toolUse" in block:
                    tool = block["toolUse"]
                    name = tool.get("name", "unknown")
                    input_str = str(tool.get("input", ""))[:5000]
                    last_tool_name = name
                    log.append({"type": "tool_call", "content": f"{name}: {input_str}"})
                elif "toolResult" in block:
                    result_content = block["toolResult"].get("content", [])
                    text_parts = []
                    for part in result_content:
                        if isinstance(part, dict) and "text" in part:
                            text_parts.append(part["text"])
                    if text_parts:
                        log.append(
                            {
                                "type": "tool_result",
                                "tool_name": last_tool_name,
                                "content": "\n".join(text_parts)[:10000],
                            }
                        )
        return log

    def create_generation_agent(
        self,
        system_prompt: str,
        tools: list[Any] | None = None,
        callback_handler: Any = None,
    ) -> Any:
        """渡されたsystem_promptとtoolsでペルソナ生成用Agentを生成する"""
        if Agent is None or BedrockModel is None:
            raise AgentInitializationError(
                "Strands Agent SDKがインストールされていません"
            )
        try:
            credentials = config.get_aws_credentials()
            filtered_credentials = {
                k: v
                for k, v in credentials.items()
                if v is not None and k != "region_name"
            }
            model = BedrockModel(
                model_id=config.BEDROCK_MODEL_ID,
                region_name=config.AWS_REGION,
                **filtered_credentials,
            )
            agent_kwargs: dict = {
                "name": "PersonaGenerator",
                "model": model,
                "system_prompt": system_prompt,
                "tools": tools if tools else None,
            }
            if callback_handler is not None:
                agent_kwargs["callback_handler"] = callback_handler

            agent = Agent(**agent_kwargs)
            self.logger.info("ペルソナ生成エージェントを作成")
            return agent
        except Exception as e:
            raise AgentInitializationError(f"ペルソナ生成エージェント作成エラー: {e}")

    def run_persona_generation(
        self,
        agent: Any,
        prompt: str,
        structured_prompt: str,
        output_schema: type,
    ) -> tuple[Any, list[dict[str, str]]]:
        """Agentを実行し、Structured Outputで結果を返す。

        Returns: (structured_result, thinking_log)
        """
        try:
            agent(prompt)
            thinking_log = self._extract_thinking_log(agent)

            max_retries = 2
            last_error = None
            result = None
            for attempt in range(max_retries + 1):
                try:
                    retry_prompt = structured_prompt
                    if last_error and attempt > 0:
                        retry_prompt = (
                            f"前回の出力でバリデーションエラーが発生しました:\n{last_error}\n\n"
                            f"エラーを修正して再度出力してください。\n{structured_prompt}"
                        )
                    result = agent.structured_output(output_schema, retry_prompt)
                    break
                except Exception as validation_err:
                    last_error = str(validation_err)
                    self.logger.warning(
                        f"structured_output バリデーションエラー (attempt {attempt + 1}/{max_retries + 1}): {last_error}"
                    )
                    if attempt == max_retries:
                        raise

            assert result is not None
            return result, thinking_log

        except Exception as e:
            raise AgentServiceError(f"ペルソナ生成実行エラー: {e}")

    def create_data_agent_tools(self, event_queue: Any = None) -> list[Any]:
        """DWH用ツールリストを生成する"""
        from .data_agent_service import create_data_agent_tool

        if not config.DATA_AGENT_RUNTIME_ARN:
            raise AgentServiceError(
                "データ分析エージェントの接続設定がされていません。設定画面から Runtime ARN を設定してください"
            )
        tool = create_data_agent_tool(
            config.DATA_AGENT_RUNTIME_ARN,
            config.DATA_AGENT_REGION,
            event_queue=event_queue,
        )
        return [tool]

    def get_mcp_tools(self) -> list[Any]:
        """MCP（MotherDuck）ツールリストを取得する"""
        from .mcp_server_manager import get_mcp_manager

        mcp_manager = get_mcp_manager()
        if not mcp_manager.is_running():
            mcp_manager.start()
        if mcp_manager.is_running():
            mcp_tools = mcp_manager.get_tools()
            if mcp_tools:
                return list(mcp_tools)
        return []
