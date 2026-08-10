"""
Agent Service
Strands Agent SDKを使用したエージェント管理サービス
"""

import queue
import logging
import threading
from typing import List, Dict, Any, Optional, Generator

try:
    from strands import Agent
    from strands.models import BedrockModel
except ImportError:
    # Strands SDKがインストールされていない場合のフォールバック
    Agent = None  # type: ignore[assignment,misc]
    BedrockModel = None  # type: ignore[assignment,misc]

from ..config import config
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.message import Message


class AgentServiceError(CodedError):
    """Agent Service関連のエラー"""

    pass


class AgentInitializationError(AgentServiceError):
    """エージェント初期化関連のエラー"""

    pass


class AgentConfigurationError(AgentServiceError):
    """運用者の設定変更が必要なエラー（例: 追加ペルソナベースモデル選択時に
    ENABLE_ADDITIONAL_PERSONA_MODELSが無効）。

    create_persona_agent/create_facilitator_agentのexcept節はこの型のCodedErrorを
    AGENT_INITIALIZATION_FAILEDに丸めず素通しする（error-catalog.md「コード付き例外を
    そのまま再送出する経路を残す」）。
    """

    pass


class AgentCommunicationError(AgentServiceError):
    """エージェント通信関連のエラー"""

    pass


class GenerationCapacityError(AgentServiceError):
    """出力トークン上限超過・応答タイムアウト等、生成負荷に起因するエラー。

    ペルソナ数やファイル量が多すぎて1回の生成に収まらない場合に発生する。
    """

    code = ErrorCode.GENERATION_CAPACITY_EXCEEDED


class ReportGenerationCapacityError(AgentServiceError):
    """レポート生成が負荷に起因して完了しなかった場合のエラー。

    議論ログ・分析対象が多く出力トークン上限を超過した場合に発生する。
    """

    code = ErrorCode.REPORT_CAPACITY_EXCEEDED


def _clear_agent_history(agent: Any, label: str) -> None:
    """Strands Agent内部の会話履歴をクリアする共通ヘルパー。"""
    _logger = logging.getLogger(__name__)
    if agent and hasattr(agent, "messages"):
        agent.messages.clear()
        _logger.info(f"Cleared conversation history for {label}")


def _dispose_agent(agent_ref: Any, label: str) -> None:
    """Strands Agentリソースを解放する共通ヘルパー。解放後 agent_ref は呼び出し側で None にすること。"""
    _logger = logging.getLogger(__name__)
    try:
        if hasattr(agent_ref, "dispose"):
            agent_ref.dispose()
        elif hasattr(agent_ref, "close"):
            agent_ref.close()
        _logger.info(f"Disposed resources for {label}")
    except Exception as e:
        _logger.warning(f"Error disposing resources for {label}: {e}")


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

        _logger.warning("No text block found, falling back to str()")
        return str(result)
    except Exception as e:
        _logger.warning(f"Text extraction failed, using fallback: {e}")
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
            f"Set {len(self._document_contents)} documents for persona {self.persona.name}"
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

            self.logger.info(f"Persona {self.persona.name} generated a response")
            return response

        except Exception as e:
            error_msg = f"Failed to generate response for persona agent {self.persona.name}: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(
                error_msg, code=ErrorCode.AGENT_COMMUNICATION_FAILED
            ) from e

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
                f"Persona {self.persona.name} completed streaming response"
            )

        except AgentCommunicationError:
            raise
        except Exception as e:
            error_msg = f"Failed to generate streaming response for persona agent {self.persona.name}: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(
                error_msg, code=ErrorCode.AGENT_COMMUNICATION_FAILED
            ) from e

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

        self.logger.info(f"Facilitator started discussion: {topic}")
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
            error_msg = f"Facilitator invocation failed: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(
                error_msg, code=ErrorCode.AGENT_COMMUNICATION_FAILED
            ) from e

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
                        f"Agent error during facilitator streaming (not raised due to client disconnect): {agent_error}"
                    )

            if agent_error:
                raise agent_error

        except AgentCommunicationError:
            raise
        except Exception as e:
            error_msg = f"Facilitator streaming invocation failed: {e}"
            self.logger.error(error_msg)
            raise AgentCommunicationError(
                error_msg, code=ErrorCode.AGENT_COMMUNICATION_FAILED
            ) from e

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
                "strands-agents package is not installed",
                code=ErrorCode.AGENT_SDK_UNAVAILABLE,
            )

        self.logger.info("Agent Service initialized")

    def _create_tool_logging_callback(self, agent_name: str) -> Any:
        """ツールコールをログするコールバックハンドラーを作成"""
        from strands.handlers.callback_handler import PrintingCallbackHandler

        logger = self.logger

        class ToolLoggingCallback(PrintingCallbackHandler):
            def on_tool_start(self, tool: Any, input_data: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                input_str = str(input_data)[:500]
                logger.info(
                    f"[{agent_name}] Tool started: {tool_name} | input: {input_str}"
                )

            def on_tool_end(self, tool: Any, result: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                result_str = str(result)[:1000]
                logger.info(
                    f"[{agent_name}] Tool completed: {tool_name} | result: {result_str}"
                )

            def on_tool_error(self, tool: Any, error: Any, **kwargs: Any) -> None:
                tool_name = getattr(tool, "name", str(tool))
                logger.error(f"[{agent_name}] Tool error: {tool_name} | error: {error}")

        return ToolLoggingCallback()

    def _create_bedrock_model_instance(self, model_id: str, region: str) -> Any:
        """指定model_id/regionでBedrockModel（Converse/SigV4）インスタンスを作成する。"""
        from botocore.config import Config as BotoConfig

        # AWS認証情報を取得
        credentials = config.get_aws_credentials()

        # None の値を除去
        filtered_credentials = {
            k: v for k, v in credentials.items() if v is not None and k != "region_name"
        }

        # 一過性の接続エラー（ストリーミング開始時のConnection closed等）対策。
        # ai_serviceと異なり自前のバックオフ機構を持たないため、boto3標準リトライに委ねる
        boto_config = BotoConfig(
            connect_timeout=30,
            read_timeout=300,
            retries={"max_attempts": 3, "mode": "adaptive"},
        )

        return BedrockModel(
            model_id=model_id,
            region_name=region,
            boto_client_config=boto_config,
            **filtered_credentials,
        )

    def _create_openai_responses_model_instance(
        self, model_id: str, region: str
    ) -> Any:
        """指定model_id/regionでOpenAIResponsesModel（Bedrock Mantle）インスタンスを作成する。

        base_url・短期Bearerトークン・path（/openai/v1 or /v1）はbedrock_mantle_configが
        リクエスト毎に解決するため自前で組まない（トークン失効・モデル誤経路を回避する設計判断）。
        """
        from strands.models.openai_responses import OpenAIResponsesModel

        return OpenAIResponsesModel(
            model_id=model_id,
            bedrock_mantle_config={"region": region},
            params={"max_output_tokens": config.AGENT_MAX_TOKENS},
        )

    def _create_model(self, model_id: Optional[str] = None) -> Any:
        """
        モデルインスタンスを作成（プロバイダ分岐 factory）

        model_registryのModelSpec.providerに応じてBedrockModel（Converse/SigV4）または
        OpenAIResponsesModel（Bedrock Mantle）を生成する。

        Args:
            model_id: 選択されたモデルID。Noneの場合は既定モデル（従来の挙動）。

        Returns:
            BedrockModel または OpenAIResponsesModel インスタンス

        Raises:
            AgentConfigurationError: 追加ペルソナベースモデルがENABLE_ADDITIONAL_PERSONA_MODELS
                無効時に選択された場合、または依存パッケージ未導入等の設定不足
                （kind=CONFIG。設定画面へ誘導するためAGENT_INITIALIZATION_FAILEDに丸めず素通しする）
            AgentInitializationError: モデル作成エラー
        """
        from ..models.model_registry import (
            ModelProvider,
            get_model_spec,
            resolve_call_region,
        )

        spec = get_model_spec(model_id)
        region = resolve_call_region(spec, config.AWS_REGION)

        if spec.provider == ModelProvider.OPENAI_RESPONSES:
            if spec.requires_mantle and not config.ENABLE_ADDITIONAL_PERSONA_MODELS:
                raise AgentConfigurationError(
                    f"Model {spec.model_id!r} requires Mantle but "
                    "ENABLE_ADDITIONAL_PERSONA_MODELS is disabled",
                    code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED,
                )
            try:
                model = self._create_openai_responses_model_instance(
                    spec.model_id, region
                )
                self.logger.info(
                    f"OpenAIResponses (Mantle) model created: {spec.model_id} (region={region})"
                )
                return model
            except ImportError as e:
                error_msg = f"strands-agents[openai] extra is not installed: {e}"
                self.logger.error(error_msg)
                raise AgentConfigurationError(
                    error_msg, code=ErrorCode.AGENT_MODEL_ADDITIONAL_MODELS_DISABLED
                ) from e
            except Exception as e:
                error_msg = (
                    f"Failed to create OpenAIResponses model {spec.model_id!r}: {e}"
                )
                self.logger.error(error_msg)
                raise AgentInitializationError(
                    error_msg, code=ErrorCode.AGENT_INITIALIZATION_FAILED
                ) from e

        try:
            model = self._create_bedrock_model_instance(spec.model_id, region)
            self.logger.info(
                f"Bedrock model created: {spec.model_id} (region={region})"
            )
            return model

        except Exception as e:
            error_msg = f"Failed to create Bedrock model: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(
                error_msg, code=ErrorCode.AGENT_INITIALIZATION_FAILED
            ) from e

    @staticmethod
    def _build_boto_config() -> Any:
        """Strands Agent経由のBedrock呼び出し用のboto3クライアント設定を返す。

        read_timeoutを未設定にするとStrands SDKの既定120秒が適用され、
        複数件生成やadaptive thinkingで応答が遅延した際にReadTimeoutErrorとなる。
        接続エラー対策としてboto3標準リトライも有効化する。
        """
        from botocore.config import Config as BotoConfig

        return BotoConfig(
            connect_timeout=30,
            read_timeout=300,
            retries={"max_attempts": 3, "mode": "adaptive"},
        )

    def _build_generation_model(self) -> Any:
        """ペルソナ生成・レポート等に使う既定モデル(config.BEDROCK_MODEL_ID)を生成する。

        max_tokensを明示しないとBedrock/モデル側の補完デフォルト(Sonnet 5で4,096)に
        張り付き、複数件の一括生成やadaptive thinkingで上限を超過して
        MaxTokensReachedExceptionとなる。timeout/retryも併せて統一する。
        """
        credentials = config.get_aws_credentials()
        filtered_credentials = {
            k: v for k, v in credentials.items() if v is not None and k != "region_name"
        }
        return BedrockModel(
            model_id=config.BEDROCK_MODEL_ID,
            region_name=config.AWS_REGION,
            max_tokens=config.AGENT_MAX_TOKENS,
            boto_client_config=self._build_boto_config(),
            **filtered_credentials,
        )

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
        model_id: Optional[str] = None,
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
            model_id: 使用するモデルID（Noneの場合は既定モデル。後方互換）

        Returns:
            PersonaAgent: 作成されたペルソナエージェント

        Raises:
            AgentConfigurationError: Mantle系モデル選択時に設定が不足している場合（kind=CONFIG）
            AgentInitializationError: エージェント作成エラー
        """
        try:
            # ツールを準備
            tools = []

            # 追加ツールを追加
            if additional_tools:
                tools.extend([t for t in additional_tools if t is not None])

            # モデルを作成（プロバイダ分岐）
            model = self._create_model(model_id)

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
                                f"Set session manager for persona {persona.name} "
                                f"(mode={mode_label})"
                            )
                        else:
                            self.logger.warning(
                                f"Persona {persona.name}: failed to create session manager. "
                                "Creating agent without memory."
                            )
                    else:
                        self.logger.info(
                            f"Persona {persona.name}: long-term memory is disabled by configuration"
                        )

                except Exception as e:
                    self.logger.warning(
                        f"Persona {persona.name}: session manager creation error: {e}. "
                        "Creating agent without memory."
                    )
            elif enable_memory and not session_id:
                self.logger.warning(
                    f"Persona {persona.name}: enable_memory=True but no session_id "
                    "was provided. Creating agent without memory."
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
                    f"Registered {len(tools)} tools for persona {persona.name}"
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
                f"Created persona agent: {persona.name} (memory={memory_status})"
            )
            return persona_agent

        except CodedError:
            # _create_modelが投げるコード付き例外（例: Mantle無効のCONFIG）は
            # AGENT_INITIALIZATION_FAILEDに丸めず素通しする
            raise
        except Exception as e:
            error_msg = f"Failed to create persona agent {persona.name}: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(
                error_msg, code=ErrorCode.AGENT_INITIALIZATION_FAILED
            ) from e

    def create_facilitator_agent(
        self,
        rounds: int,
        additional_instructions: str = "",
        system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> FacilitatorAgent:
        """ファシリテータエージェントを作成。

        Args:
            rounds: ラウンド数
            additional_instructions: 追加の指示
            system_prompt: 構築済みシステムプロンプト（指定時はrounds/additional_instructionsからの自動生成をスキップ）
            model_id: 使用するモデルID（Noneの場合は既定モデル。後方互換）

        Raises:
            AgentConfigurationError: Mantle系モデル選択時に設定が不足している場合（kind=CONFIG）
            AgentInitializationError: エージェント作成エラー
        """
        try:
            if system_prompt is None:
                from ..prompts.discussion_interview_prompts import (
                    build_facilitator_system_prompt,
                )

                system_prompt = build_facilitator_system_prompt(
                    rounds, additional_instructions
                )

            model = self._create_model(model_id)
            agent = Agent(name="Facilitator", system_prompt=system_prompt, model=model)
            facilitator_agent = FacilitatorAgent(rounds, additional_instructions, agent)

            self.logger.info(f"Created facilitator agent (rounds: {rounds})")
            return facilitator_agent

        except CodedError:
            # _create_modelが投げるコード付き例外（例: Mantle無効のCONFIG）は
            # AGENT_INITIALIZATION_FAILEDに丸めず素通しする
            raise
        except Exception as e:
            error_msg = f"Failed to create facilitator agent: {e}"
            self.logger.error(error_msg)
            raise AgentInitializationError(
                error_msg, code=ErrorCode.AGENT_INITIALIZATION_FAILED
            ) from e

    def get_kb_tools(
        self, persona_id: str, db_service: Any
    ) -> tuple[list[Any], Optional[Dict[str, Any]]]:
        """KB連携ツールとKBメタ情報を返す。プロンプト構築はしない。

        Returns:
            (tools, kb_info) — kb_infoは {"name": str, "description": str, "metadata_filters": dict|None}
            バインディング未設定時は ([], None)
        """
        tools: list[Any] = []

        kb_binding = db_service.get_kb_binding_by_persona(persona_id)
        if kb_binding:
            kb = db_service.get_knowledge_base(kb_binding.kb_id)
            if kb:
                from .knowledge_base.kb_tools import create_kb_retrieval_tool

                kb_tool = create_kb_retrieval_tool(
                    knowledge_base_id=kb.knowledge_base_id,
                    metadata_filters=kb_binding.metadata_filters,
                    region=config.AWS_REGION,
                )
                tools.append(kb_tool)
                return tools, {
                    "name": kb.name,
                    "description": kb.description,
                    "metadata_filters": kb_binding.metadata_filters,
                }

        return tools, None

    def get_dataset_tools(
        self, persona_id: str, db_service: Any
    ) -> tuple[list[Any], List[Dict], List[Any]]:
        """データセット連携ツールとバインディング/データセット情報を返す。

        Returns:
            (tools, bindings_dict, datasets)
            バインディング未設定時は ([], [], [])
        """
        tools: list[Any] = []

        bindings = db_service.get_bindings_by_persona(persona_id)
        if not bindings:
            return tools, [], []

        dataset_ids = list(set(b.dataset_id for b in bindings))
        datasets = [db_service.get_dataset(did) for did in dataset_ids]
        datasets = [d for d in datasets if d is not None]
        bindings_dict = [
            {"dataset_id": b.dataset_id, "binding_keys": b.binding_keys}
            for b in bindings
        ]

        mcp_tools = self.get_mcp_tools()
        if mcp_tools:
            tools.extend(mcp_tools)

        return tools, bindings_dict, datasets

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
                "strands-agents package is not installed",
                code=ErrorCode.AGENT_SDK_UNAVAILABLE,
            )
        try:
            model = self._build_generation_model()
            agent_kwargs: dict = {
                "name": "PersonaGenerator",
                "model": model,
                "system_prompt": system_prompt,
                "tools": tools if tools else None,
            }
            if callback_handler is not None:
                agent_kwargs["callback_handler"] = callback_handler

            agent = Agent(**agent_kwargs)
            self.logger.info("Created persona generation agent")
            return agent
        except Exception as e:
            raise AgentInitializationError(
                f"generation agent creation failed ({type(e).__name__})",
                code=ErrorCode.AGENT_INITIALIZATION_FAILED,
            ) from e

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
                    if self._is_capacity_error(validation_err):
                        # 容量起因（トークン上限超過・タイムアウト）は確定的失敗であり、
                        # 出力修正のリトライで回復しないため即座に外側で変換する
                        raise
                    last_error = str(validation_err)
                    self.logger.warning(
                        f"structured_output validation error (attempt {attempt + 1}/{max_retries + 1}): {last_error}"
                    )
                    if attempt == max_retries:
                        raise

            assert result is not None
            return result, thinking_log

        except Exception as e:
            if self._is_capacity_error(e):
                self.logger.warning(
                    "Error caused by generation capacity limit", exc_info=True
                )
                raise GenerationCapacityError(
                    f"persona generation hit capacity limit ({type(e).__name__}), "
                    f"agent_max_tokens={config.AGENT_MAX_TOKENS}"
                ) from e
            raise AgentServiceError(
                f"persona generation failed ({type(e).__name__})"
            ) from e

    @staticmethod
    def _is_capacity_error(error: Exception) -> bool:
        """出力トークン上限超過・応答タイムアウト等の生成負荷起因エラーか判定する。

        StrandsのMaxTokensReachedException、structured_outputのmax_tokens起因
        ValueError、Bedrock接続のReadTimeoutを型名・メッセージから検出する。
        """
        error_type_names = {type(error).__name__}
        cause = error.__cause__ or error.__context__
        if cause is not None:
            error_type_names.add(type(cause).__name__)

        capacity_type_names = {
            "MaxTokensReachedException",
            "ReadTimeoutError",
            "ConnectTimeoutError",
        }
        if error_type_names & capacity_type_names:
            return True

        message = str(error).lower()
        capacity_markers = ("max_tokens", "read timed out", "read timeout")
        return any(marker in message for marker in capacity_markers)

    def create_data_agent_tools(self, event_queue: Any = None) -> list[Any]:
        """DWH用ツールリストを生成する"""
        from .data_agent_service import create_data_agent_tool

        if not config.DATA_AGENT_RUNTIME_ARN:
            raise AgentServiceError(
                "DATA_AGENT_RUNTIME_ARN is not configured",
                code=ErrorCode.DATA_AGENT_NOT_CONFIGURED,
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

    # =========================================================================

    # =========================================================================
    # レポートエージェント（データドリブン分析）
    # =========================================================================

    def run_report_agent_streaming(
        self,
        system_prompt: str,
        user_content: str,
        event_queue: Any = None,
        session_id: Optional[str] = None,
    ) -> Any:
        """データドリブンレポート用Strands Agentを作成・実行する。

        event_queueが渡された場合、thinking/tool_call/tool_resultイベントを
        リアルタイムでputし、最終的に_doneシグナルを送信する。
        event_queueがNoneの場合はエージェント結果をyieldする。

        Args:
            system_prompt: システムプロンプト
            user_content: ユーザーコンテンツ（議論ログ+インサイト）
            event_queue: リアルタイムイベント用queue
            session_id: AgentCore Memory STMセッションID
        """
        if not config.ENABLE_DATA_AGENT or not config.DATA_AGENT_RUNTIME_ARN:
            msg = (
                "⚠️ データ分析エージェントの接続設定がされていません。"
                "設定画面から Runtime ARN を設定してください。"
            )
            if event_queue is not None:
                event_queue.put({"type": "error", "content": msg})
                return
            yield msg
            return

        try:
            from .data_agent_service import create_data_agent_tool
        except ImportError as e:
            msg = f"⚠️ Strands Agent SDK の初期化に失敗しました: {e}"
            if event_queue is not None:
                event_queue.put({"type": "error", "content": msg})
                return
            yield msg
            return

        def _callback(**kwargs: Any) -> None:
            data = kwargs.get("data", "")
            if data and event_queue is not None:
                event_queue.put({"type": "thinking", "content": data})

        report_session_manager = None
        if session_id:
            try:
                from .memory.session_manager_factory import (
                    create_agentcore_session_manager,
                    is_memory_enabled,
                )

                if is_memory_enabled():
                    report_session_manager = create_agentcore_session_manager(
                        actor_id="report-agent",
                        session_id=session_id,
                        retrieval_config={},
                        memory_mode="full",
                    )
            except Exception as e:
                self.logger.warning(
                    f"Failed to create session manager for report agent: {e}"
                )

        try:
            model = self._build_generation_model()
            data_agent_tool = create_data_agent_tool(
                config.DATA_AGENT_RUNTIME_ARN,
                config.DATA_AGENT_REGION,
                event_queue=event_queue,
            )

            agent_kwargs: dict[str, Any] = {
                "model": model,
                "tools": [data_agent_tool],
                "system_prompt": system_prompt,
                "callback_handler": _callback if event_queue is not None else None,
            }
            if report_session_manager:
                agent_kwargs["session_manager"] = report_session_manager

            agent = Agent(**agent_kwargs)
            result = agent(user_content)

            session_has_history = (
                report_session_manager is not None and len(agent.messages) > 2
            )

            if event_queue is not None:
                event_queue.put(
                    {
                        "type": "session_id",
                        "session_id": session_id or "",
                        "has_history": session_has_history,
                    }
                )
                event_queue.put({"type": "_done"})
            else:
                yield str(result)
        except Exception as e:
            # ユーザー向け文言はプレゼンテーション層のカタログが持つ。ここでは
            # エラー種別をコード付き例外として送出するだけに留める。呼び出し側
            # (Router) は event_queue 経路でも future.exception() で受け取る。
            if self._is_capacity_error(e):
                self.logger.warning(
                    "Report generation hit capacity limit", exc_info=True
                )
                raise ReportGenerationCapacityError(
                    f"report generation hit capacity limit ({type(e).__name__}), "
                    f"agent_max_tokens={config.AGENT_MAX_TOKENS}"
                ) from e
            self.logger.error("Report generation error occurred.", exc_info=True)
            raise AgentServiceError(
                f"report generation failed ({type(e).__name__})",
                code=ErrorCode.AGENT_COMMUNICATION_FAILED,
            ) from e

    def run_segment_extraction_agent(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Any],
    ) -> None:
        """Strands Agentを実行する。toolsはManager層から渡される。"""
        model = self._build_generation_model()
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
        )
        agent(user_prompt)

    def suggest_column_mapping_with_llm(self, prompt: str) -> Dict[str, Any]:
        """Strands Agent Structured Outputでカラムマッピング提案を返す。"""
        from pydantic import BaseModel, Field

        class ExtraColumnItem(BaseModel):
            csv_column: str = Field(description="CSVカラム名")
            label: str = Field(description="日本語ラベル")
            description: str = Field(description="補足説明")

        class ColumnMappingOutput(BaseModel):
            mapping: Dict[str, str] = Field(
                description="標準カラム名→CSVカラム名のマッピング"
            )
            extra_columns: List[ExtraColumnItem] = Field(
                description="標準カラム以外で有用なカラムの補足情報"
            )

        model = self._build_generation_model()
        agent = Agent(model=model, tools=[])
        result = agent.structured_output(ColumnMappingOutput, prompt)

        return {
            "mapping": dict(result.mapping),
            "extra_columns": [e.model_dump() for e in result.extra_columns],
        }
