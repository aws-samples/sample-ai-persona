"""
Agent Discussion Manager for AI Persona System.
Handles AI agent mode discussion setup, execution, and persistence.
"""

import json
import logging
from typing import Generator, List, Dict, Mapping, Optional, Any

from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.message import Message
from ..models.insight_category import InsightCategory
from ..prompts.discussion_interview_prompts import build_persona_system_prompt
from ..services.agent_service import (
    AgentService,
    PersonaAgent,
    FacilitatorAgent,
    AgentConfigurationError,
    AgentInitializationError,
    AgentCommunicationError,
)
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory
from .shared.agent_cleanup import dispose_agents
from .shared.model_validation import (
    any_model_requires_mantle,
    resolve_effective_persona_models,
    validate_document_size_for_models,
    validate_model_selection,
)


class AgentDiscussionManagerError(CodedError):
    """Base exception for agent discussion manager related errors."""

    pass


class DiscussionFlowError(AgentDiscussionManagerError):
    """Discussion flow related errors."""

    pass


class AgentDiscussionManager:
    """
    Manager class for handling AI agent mode discussion operations.
    Orchestrates agent creation, discussion execution, and persistence.
    """

    def __init__(
        self,
        agent_service: AgentService | None = None,
        database_service: Optional[DatabaseService] = None,
    ):
        """
        Initialize agent discussion manager.

        Args:
            agent_service: Agent service instance for agent management (optional, uses singleton if not provided)
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
        """
        self.logger = logging.getLogger(__name__)

        # Use singleton services if not provided
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )

    def create_persona_agents(
        self,
        personas: List[Persona],
        system_prompts: Dict[str, str],
        enable_memory: bool = False,
        session_id: Optional[str] = None,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
        persona_models: Optional[Dict[str, str]] = None,
    ) -> List[PersonaAgent]:
        """
        Create persona agents from personas and system prompts.

        Args:
            personas: List of Persona objects
            system_prompts: Dictionary mapping persona_id to system_prompt
            enable_memory: Whether to enable long-term memory for agents (default: False)
            session_id: Discussion session ID for memory association (required if enable_memory=True)
            memory_mode: Memory mode (default: "full")
                - "full": 検索 + 保存
                - "retrieve_only": 検索のみ（保存しない）
                - "disabled": メモリ機能無効
            enable_dataset: Whether to enable external dataset access (default: False)
            enable_kb: Whether to enable knowledge base access (default: False)
            persona_models: persona_id -> model_id のマップ（省略時は既定モデル）

        Returns:
            List[PersonaAgent]: Created persona agents

        Raises:
            AgentDiscussionManagerError: If agent creation fails
        """
        if not personas:
            raise AgentDiscussionManagerError(
                "persona list is empty",
                code=ErrorCode.DISCUSSION_PERSONAS_REQUIRED,
            )

        if len(personas) < 2:
            raise AgentDiscussionManagerError(
                f"{len(personas)} personas given, minimum is 2",
                code=ErrorCode.DISCUSSION_TOO_FEW_PERSONAS,
                context={"min_personas": 2},
            )

        self._validate_model_selection(persona_models)

        self.logger.info(
            f"Creating {len(personas)} persona agents "
            f"(enable_memory={enable_memory}, memory_mode={memory_mode}, enable_dataset={enable_dataset}, enable_kb={enable_kb})"
        )

        # Validate memory configuration
        if enable_memory and not session_id:
            self.logger.warning(
                "Long-term memory requested but session_id is not provided. "
                "Agents will be created without memory."
            )
            enable_memory = False

        persona_agents = []
        failed_personas = []

        for persona in personas:
            try:
                # Get system prompt for this persona
                system_prompt = system_prompts.get(
                    persona.id,
                    build_persona_system_prompt(persona),
                )

                # Create persona agent with memory and dataset/KB configuration
                persona_agent = self._create_agent_with_integrations(
                    persona=persona,
                    system_prompt=system_prompt,
                    enable_memory=enable_memory,
                    session_id=session_id,
                    memory_mode=memory_mode,
                    enable_dataset=enable_dataset,
                    enable_kb=enable_kb,
                    model_id=(persona_models or {}).get(persona.id),
                )
                persona_agents.append(persona_agent)

                self.logger.info(f"Created persona agent: {persona.name}")

            except AgentConfigurationError as e:
                # 追加ペルソナベースモデル無効等の設定不足は個別ペルソナの失敗として
                # 握り潰さず、DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED（CONFIG）として即時通知する。
                # ここまでに作成済みのpersona_agentsはこの後どこにも渡らないため、raise前に解放する。
                self.logger.error(
                    f"Configuration error creating agent for persona {persona.name}: {e}"
                )
                self.cleanup_agents(persona_agents)
                raise AgentDiscussionManagerError(
                    f"model configuration error for persona {persona.name} "
                    f"({type(e).__name__})",
                    code=ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
                ) from e
            except AgentInitializationError as e:
                error_msg = f"Failed to create agent for persona {persona.name}: {e}"
                self.logger.error(error_msg)
                failed_personas.append(persona.name)
            except Exception as e:
                error_msg = (
                    f"Unexpected error creating agent for persona {persona.name}: {e}"
                )
                self.logger.error(error_msg)
                failed_personas.append(persona.name)

        # Check if we have enough agents
        if len(persona_agents) < 2:
            self.logger.error(
                "Failed to create enough persona agents. Failed: %s",
                ", ".join(failed_personas),
            )
            # 最低数に満たない場合、作成済みのpersona_agentsはこの後どこにも渡らないため解放する
            self.cleanup_agents(persona_agents)
            raise AgentDiscussionManagerError(
                f"only {len(persona_agents)} persona agents created, minimum is 2",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

        if failed_personas:
            self.logger.warning(
                f"Some persona agents failed to initialize: {', '.join(failed_personas)}"
            )

        self.logger.info(f"Successfully created {len(persona_agents)} persona agents")
        return persona_agents

    def create_facilitator_agent(
        self,
        rounds: int,
        additional_instructions: str = "",
        facilitator_model: Optional[str] = None,
    ) -> FacilitatorAgent:
        """
        Create facilitator agent for discussion management.

        Args:
            rounds: Number of discussion rounds
            additional_instructions: Additional instructions for facilitator
            facilitator_model: 使用するモデルID（省略時は既定モデル）

        Returns:
            FacilitatorAgent: Created facilitator agent

        Raises:
            AgentDiscussionManagerError: If facilitator creation fails
        """
        if rounds < 1:
            raise AgentDiscussionManagerError(
                f"rounds {rounds} below minimum 1",
                code=ErrorCode.DISCUSSION_ROUNDS_TOO_FEW,
                context={"min_rounds": 1},
            )

        if rounds > 10:
            raise AgentDiscussionManagerError(
                f"rounds {rounds} exceeds maximum 10",
                code=ErrorCode.DISCUSSION_ROUNDS_TOO_MANY,
                context={"max_rounds": 10},
            )

        self._validate_model_selection(
            {"facilitator": facilitator_model} if facilitator_model else None
        )

        self.logger.info(f"Creating facilitator agent with {rounds} rounds")

        try:
            facilitator = self.agent_service.create_facilitator_agent(
                rounds, additional_instructions, model_id=facilitator_model
            )

            self.logger.info("Successfully created facilitator agent")
            return facilitator

        except AgentConfigurationError as e:
            # _create_modelが投げるコード付き例外（例: 追加ペルソナベースモデル無効のCONFIG）は
            # DISCUSSION_AGENT_SETUP_FAILEDに丸めず自ドメインのCONFIGコードへ変換する
            self.logger.error("Facilitator model configuration error", exc_info=True)
            raise AgentDiscussionManagerError(
                f"facilitator model configuration error ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
            ) from e
        except AgentInitializationError as e:
            self.logger.error("Failed to create facilitator agent", exc_info=True)
            raise AgentDiscussionManagerError(
                f"facilitator agent creation failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            ) from e
        except Exception as e:
            self.logger.error(
                "Unexpected error creating facilitator agent", exc_info=True
            )
            raise AgentDiscussionManagerError(
                f"facilitator agent creation failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            ) from e

    def _validate_model_selection(
        self, model_ids: Optional[Mapping[str, Optional[str]]]
    ) -> None:
        """選択されたmodel_idが利用可能か検証する。

        未対応のmodel_idはVALIDATION、追加ペルソナベースモデルだが
        ENABLE_ADDITIONAL_PERSONA_MODELS無効時はCONFIG。

        Args:
            model_ids: 検証対象の {識別子: model_id} マップ（Noneは既定モデルとしてスキップ）
        """
        validate_model_selection(
            model_ids,
            AgentDiscussionManagerError,
            ErrorCode.DISCUSSION_MODEL_UNSUPPORTED,
            ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
        )

    def start_agent_discussion(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
        enable_memory: bool = False,
        document_ids: Optional[List[str]] = None,
        persona_models: Optional[Dict[str, str]] = None,
        facilitator_model: Optional[str] = None,
    ) -> Discussion:
        """
        Start and execute an AI agent mode discussion.

        Args:
            personas: List of participating personas
            topic: Discussion topic
            persona_agents: List of persona agents
            facilitator: Facilitator agent
            enable_memory: Whether long-term memory is enabled for this discussion
            document_ids: Optional list of document IDs to include in discussion
            persona_models: persona_id -> model_id のマップ（agent_config保存・入力サイズ検証用）
            facilitator_model: facilitatorのmodel_id（agent_config保存用）

        Returns:
            Discussion: Discussion object with generated messages

        Raises:
            AgentDiscussionManagerError: If discussion execution fails

        Requirements:
            - 6.3: Pass memory configuration through discussion flow
        """
        try:
            # Validate input（トピック長等の検証失敗時もfinallyでagentを解放するためtry内で行う）
            self._validate_discussion_input(
                personas, topic, persona_agents, facilitator
            )

            # Load documents if provided
            documents_metadata, document_context, document_contents = (
                self._load_and_attach_documents(
                    document_ids, persona_agents, persona_models
                )
            )

            self.logger.info(
                f"Starting agent discussion with {len(persona_agents)} agents on topic: '{topic[:50]}...' "
                f"(enable_memory={enable_memory}, documents={len(documents_metadata) if documents_metadata else 0})"
            )

            # Create agent_config with facilitator settings and memory configuration
            agent_config = {
                "rounds": facilitator.rounds,
                "additional_instructions": facilitator.additional_instructions,
                "enable_memory": enable_memory,
                "persona_models": persona_models,
                "facilitator_model": facilitator_model,
            }

            # Create new discussion instance with documents
            discussion = Discussion.create_new(
                topic=topic.strip(),
                participants=[persona.id for persona in personas],
                mode="agent",
                agent_config=agent_config,
                documents=documents_metadata,
            )

            # Add document context to topic if documents present
            discussion_topic = topic
            if document_context:
                discussion_topic = f"{topic}\n{document_context}"

            # Start discussion with facilitator
            start_message = facilitator.start_discussion(
                discussion_topic, persona_agents
            )
            self.logger.info(f"Facilitator started discussion: {start_message}")

            # Execute discussion rounds
            all_messages: list[Any] = []
            round_summaries: list[str] = []

            total_rounds = facilitator.rounds
            for current_round in range(1, total_rounds + 1):
                self.logger.info(f"Starting round {current_round}/{total_rounds}")

                # ラウンド開始時: 全エージェントの会話履歴をクリア（コンテキスト膨張防止）
                if current_round > 1:
                    for agent in persona_agents:
                        agent.clear_conversation_history()
                        if document_contents:
                            agent.set_document_contents(document_contents.copy())
                    facilitator.clear_conversation_history()
                    self.logger.info(
                        f"Cleared conversation history for round {current_round}"
                    )

                # Track who has spoken in this round and round messages
                spoken_in_round: list[str] = []
                round_messages = []

                # Each persona speaks once per round
                for _ in range(len(persona_agents)):
                    speaker = self._select_next_speaker(persona_agents, spoken_in_round)

                    if speaker is None:
                        break

                    prompt = self._build_persona_prompt(
                        speaker,
                        topic,
                        all_messages[-10:],
                        current_round,
                        total_rounds,
                        round_summaries=round_summaries if round_summaries else None,
                        latest_facilitator_message=round_summaries[-1]
                        if round_summaries
                        else None,
                    )

                    # Get persona's response (context=None, already in prompt)
                    try:
                        statement = speaker.respond(prompt, None)

                        # Create message for persona statement
                        message = Message.create_new(
                            persona_id=speaker.get_persona_id(),
                            persona_name=speaker.get_persona_name(),
                            content=statement,
                            message_type="statement",
                            round_number=current_round,
                        )
                        all_messages.append(message)
                        round_messages.append(message)

                        self.logger.info(
                            f"Persona {speaker.get_persona_name()} spoke in round {current_round}"
                        )

                        # Mark this persona as having spoken
                        spoken_in_round.append(speaker.get_persona_id())

                    except AgentCommunicationError as e:
                        error_msg = f"Failed to get response from {speaker.get_persona_name()}: {e}"
                        self.logger.error(error_msg)
                        # Continue with other personas
                        spoken_in_round.append(speaker.get_persona_id())
                        continue

                # ラウンド終了後にファシリテータがラウンド全体を要約
                if round_messages:
                    try:
                        summary_prompt = self._build_summary_prompt(
                            current_round,
                            round_messages,
                            topic,
                            total_rounds,
                            previous_summaries=round_summaries
                            if round_summaries
                            else None,
                        )
                        round_summary = facilitator.invoke(summary_prompt)

                        # 要約を蓄積（次ラウンドのコンテキストとして使用）
                        round_summaries.append(round_summary)

                        # Create message for round summary
                        summary_message = Message.create_new(
                            persona_id="facilitator",
                            persona_name="ファシリテータ",
                            content=round_summary,
                            message_type="summary",
                            round_number=current_round,
                        )
                        all_messages.append(summary_message)

                        self.logger.info(
                            f"Facilitator summarized round {current_round}"
                        )

                    except AgentCommunicationError as e:
                        self.logger.warning(
                            f"Failed to summarize round {current_round}: {e}"
                        )
                        # Continue without round summary

                self.logger.info(
                    f"Completed round {current_round}/{facilitator.rounds}"
                )

            # Add all messages to discussion
            for message in all_messages:
                discussion = discussion.add_message(message)

            # Validate discussion results
            self._validate_discussion_results(discussion, personas)

            self.logger.info(
                f"Agent discussion completed successfully: {discussion.id} with {len(all_messages)} messages"
            )
            return discussion

        except AgentCommunicationError as e:
            self.logger.error(
                "Agent communication error during discussion", exc_info=True
            )
            raise AgentDiscussionManagerError(
                f"agent discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        except DiscussionFlowError as e:
            self.logger.error("Discussion flow error", exc_info=True)
            raise AgentDiscussionManagerError(
                f"agent discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        except AgentDiscussionManagerError:
            # 入力検証・ドキュメント検証（VALIDATION/CAPACITY等）が投げたコード付き例外は
            # そのまま再送出する（DISCUSSION_OPERATION_FAILEDに丸めるとユーザーが取れる
            # 行動の情報が失われる）
            raise
        except Exception as e:
            self.logger.error("Unexpected error during agent discussion", exc_info=True)
            raise AgentDiscussionManagerError(
                f"agent discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        finally:
            # エージェントリソースを確実に解放
            self.cleanup_agents(persona_agents, facilitator)

    def save_agent_discussion(self, discussion: Discussion) -> str:
        """
        Save an AI agent mode discussion to the database.

        Args:
            discussion: Discussion object to save

        Returns:
            str: The discussion ID

        Raises:
            AgentDiscussionManagerError: If save operation fails
        """
        if not discussion:
            raise AgentDiscussionManagerError(
                "discussion object is falsy",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        if discussion.mode != "agent":
            raise AgentDiscussionManagerError(
                f"invalid discussion mode {discussion.mode!r}, expected 'agent'",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        # Validate discussion before saving
        self._validate_discussion_for_save(discussion)

        try:
            discussion_id = self.database_service.save_discussion(discussion)
            self.logger.info(
                f"Agent discussion saved successfully: {discussion.topic} (ID: {discussion_id})"
            )
            return discussion_id

        except DatabaseError as e:
            self.logger.error(
                "Database error while saving agent discussion", exc_info=True
            )
            raise AgentDiscussionManagerError(
                f"agent discussion save failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error(
                "Unexpected error while saving agent discussion", exc_info=True
            )
            raise AgentDiscussionManagerError(
                f"agent discussion save failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e

    def _resolve_effective_persona_models(
        self,
        persona_agents: List[PersonaAgent],
        persona_models: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """未選択のペルソナにはconfig.AGENT_MODEL_ID（環境既定モデル）を補完する。

        環境既定モデル（config.AGENT_MODEL_ID）はGemma4等のMantle系モデルに設定されうるため、
        添付種別・サイズ検証は「フォームで明示的に選ばれたモデル」だけでなく実際に呼び出される
        モデルを対象にする必要がある（persona_models=Noneのまま検証をスキップすると、環境既定を
        Mantle系にした運用でサイズ・種別制限を回避できてしまう）。
        """
        return resolve_effective_persona_models(
            (agent.get_persona_id() for agent in persona_agents), persona_models
        )

    def _load_and_attach_documents(
        self,
        document_ids: Optional[List[str]],
        persona_agents: List[PersonaAgent],
        persona_models: Optional[Dict[str, str]] = None,
    ) -> tuple:
        """ドキュメントを読み込みエージェントに添付する。"""
        documents_metadata = None
        document_context = None
        document_contents: List[Dict[str, Any]] = []

        if document_ids:
            from .shared.document_loader import (
                build_document_context,
                load_documents_metadata,
                prepare_document_contents,
            )

            documents_metadata = load_documents_metadata(
                document_ids, self.database_service
            )

            if documents_metadata:
                effective_persona_models = self._resolve_effective_persona_models(
                    persona_agents, persona_models
                )
                self._validate_document_support_for_models(
                    documents_metadata, effective_persona_models
                )
                self._validate_document_size_for_models(
                    documents_metadata, effective_persona_models
                )

                document_context = build_document_context(documents_metadata)
                self.logger.info(
                    f"Loaded {len(documents_metadata)} documents for discussion"
                )

                s3_service = service_factory.get_s3_service()
                document_contents = prepare_document_contents(
                    documents_metadata, s3_service
                )
                if document_contents:
                    self.logger.info(
                        f"Prepared {len(document_contents)} document contents for agents"
                    )
                    for agent in persona_agents:
                        agent.set_document_contents(document_contents.copy())

        return documents_metadata, document_context, document_contents

    def _validate_document_support_for_models(
        self,
        documents_metadata: List[Dict[str, Any]],
        persona_models: Optional[Dict[str, str]],
    ) -> None:
        """追加ペルソナベースモデル（Mantle経由）に非対応のドキュメントが添付されていないか検証する。

        Strands SDK（1.51.0時点）のOpenAIResponsesModel（Mantle経由）は、document
        （PDF等。input_file）構築時にfilenameを送信せずMantle側のファイル種別判定が失敗する
        既知の実装漏れがある（Mantleエンドポイント自体はfilename付きの正しい形式なら受理する。
        AWS実機での直接検証で確認済み。上流SDK修正待ち: strands-agents #3576 / #3674）。
        image（input_image）はfilenameという概念自体を持たないパラメータ形式のため、
        現行実装のままMantle側も問題なく受理する（同検証で確認済み）。
        そのためdocument系（PDF等）のみを拒否し、imageは許可する。
        """
        if not any_model_requires_mantle(persona_models):
            return

        from .shared.document_loader import is_image_type

        for doc in documents_metadata:
            mime_type = doc.get("mime_type", "")
            if not is_image_type(mime_type):
                raise AgentDiscussionManagerError(
                    f"document mime_type {mime_type!r} is not supported by "
                    "Mantle-routed models (image is supported, other document "
                    "types are not)",
                    code=ErrorCode.DISCUSSION_MODEL_DOCUMENT_UNSUPPORTED,
                )

    def _validate_document_size_for_models(
        self,
        documents_metadata: List[Dict[str, Any]],
        persona_models: Optional[Dict[str, str]],
    ) -> None:
        """選択モデルのmax_request_bytes（Gemma4等）に対しドキュメント合計サイズを検証する。

        base64化によるオーバーヘッド（概算4/3倍）を見込んだ実効上限で判定する。
        """
        total_size = sum(doc.get("file_size", 0) for doc in documents_metadata)
        validate_document_size_for_models(
            total_size,
            persona_models,
            AgentDiscussionManagerError,
            ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
        )

    def _validate_discussion_input(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
    ) -> None:
        """
        Validate input parameters for discussion start.

        Args:
            personas: List of personas
            topic: Discussion topic
            persona_agents: List of persona agents
            facilitator: Facilitator agent

        Raises:
            AgentDiscussionManagerError: If validation fails
        """
        # Validate personas
        if not personas:
            raise AgentDiscussionManagerError(
                "persona list is empty",
                code=ErrorCode.DISCUSSION_PERSONAS_REQUIRED,
            )

        if len(personas) < 2:
            raise AgentDiscussionManagerError(
                f"{len(personas)} personas given, minimum is 2",
                code=ErrorCode.DISCUSSION_TOO_FEW_PERSONAS,
                context={"min_personas": 2},
            )

        # Validate topic
        if not topic or not topic.strip():
            raise AgentDiscussionManagerError(
                "topic is blank", code=ErrorCode.DISCUSSION_TOPIC_REQUIRED
            )

        if len(topic.strip()) < 5:
            raise AgentDiscussionManagerError(
                f"topic length {len(topic.strip())} below minimum 5",
                code=ErrorCode.DISCUSSION_TOPIC_TOO_SHORT,
                context={"min_length": 5},
            )

        if len(topic.strip()) > 200:
            raise AgentDiscussionManagerError(
                f"topic length {len(topic.strip())} exceeds 200",
                code=ErrorCode.DISCUSSION_TOPIC_TOO_LONG,
                context={"max_length": 200},
            )

        # Validate persona agents (内部状態: エージェント生成が先に失敗している)
        if not persona_agents:
            raise AgentDiscussionManagerError(
                "persona agent list is empty",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

        if len(persona_agents) < 2:
            raise AgentDiscussionManagerError(
                f"{len(persona_agents)} persona agents given, minimum is 2",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

        # Validate facilitator
        if not facilitator:
            raise AgentDiscussionManagerError(
                "facilitator agent is not set",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

    def _validate_discussion_results(
        self, discussion: Discussion, original_personas: List[Persona]
    ) -> None:
        """
        Validate discussion results after execution.

        Args:
            discussion: Generated discussion
            original_personas: Original personas that participated

        Raises:
            DiscussionFlowError: If validation fails
        """
        if not discussion:
            raise DiscussionFlowError(
                "generated discussion is falsy",
                code=ErrorCode.DISCUSSION_RESULT_INVALID,
            )

        if not discussion.messages or len(discussion.messages) < 2:
            raise DiscussionFlowError(
                f"generated discussion has {len(discussion.messages)} messages, "
                "minimum is 2",
                code=ErrorCode.DISCUSSION_RESULT_INVALID,
            )

        # Check that personas have messages
        persona_message_count: dict[str, int] = {}
        for message in discussion.messages:
            if message.message_type == "statement":
                persona_message_count[message.persona_id] = (
                    persona_message_count.get(message.persona_id, 0) + 1
                )

        for persona in original_personas:
            if persona_message_count.get(persona.id, 0) == 0:
                self.logger.warning(
                    f"ペルソナ {persona.name} の発言が見つかりませんでした"
                )

    def _create_agent_with_integrations(
        self,
        persona: Persona,
        system_prompt: str,
        enable_memory: bool,
        session_id: Optional[str],
        memory_mode: str,
        enable_dataset: bool,
        enable_kb: bool,
        model_id: Optional[str] = None,
    ) -> PersonaAgent:
        """統合機能（KB、データセット）付きペルソナエージェントを作成。"""
        from .shared.agent_integration import prepare_integration_tools_and_prompt

        enhanced_prompt, additional_tools = prepare_integration_tools_and_prompt(
            agent_service=self.agent_service,
            database_service=self.database_service,
            persona_id=persona.id,
            base_prompt=system_prompt,
            enable_kb=enable_kb,
            enable_dataset=enable_dataset,
        )
        return self.agent_service.create_persona_agent(
            persona=persona,
            system_prompt=enhanced_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            additional_tools=additional_tools,
            memory_mode=memory_mode,
            model_id=model_id,
        )

    def start_agent_discussion_streaming(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
        enable_memory: bool = False,
        document_ids: Optional[List[str]] = None,
        persona_models: Optional[Dict[str, str]] = None,
        facilitator_model: Optional[str] = None,
    ) -> Any:
        """
        Start and execute an AI agent mode discussion with streaming.
        Yields each message as it's generated.

        Args:
            personas: List of participating personas
            topic: Discussion topic
            persona_agents: List of persona agents
            facilitator: Facilitator agent
            enable_memory: Whether long-term memory is enabled for this discussion
            document_ids: Optional list of document IDs to include in discussion
            persona_models: persona_id -> model_id のマップ（agent_config保存・入力サイズ検証用）
            facilitator_model: facilitatorのmodel_id（agent_config保存用）

        Yields:
            tuple: (message_type, message_or_discussion)
                - ("message", Message): Individual message
                - ("complete", Discussion): Final discussion object

        Raises:
            AgentDiscussionManagerError: If discussion execution fails

        Requirements:
            - 6.3: Pass memory configuration through discussion flow
        """
        try:
            # Validate input（トピック長等の検証失敗時もfinallyでagentを解放するためtry内で行う）
            self._validate_discussion_input(
                personas, topic, persona_agents, facilitator
            )

            # Load documents if provided
            documents_metadata, document_context, document_contents = (
                self._load_and_attach_documents(
                    document_ids, persona_agents, persona_models
                )
            )

            self.logger.info(
                f"Starting streaming agent discussion with {len(persona_agents)} agents "
                f"(enable_memory={enable_memory}, documents={len(documents_metadata) if documents_metadata else 0})"
            )

            # Create agent_config with facilitator settings and memory configuration
            agent_config = {
                "rounds": facilitator.rounds,
                "additional_instructions": facilitator.additional_instructions,
                "enable_memory": enable_memory,
                "persona_models": persona_models,
                "facilitator_model": facilitator_model,
            }

            # Create new discussion instance with documents
            discussion = Discussion.create_new(
                topic=topic.strip(),
                participants=[persona.id for persona in personas],
                mode="agent",
                agent_config=agent_config,
                documents=documents_metadata,
            )

            # Add document context to topic if documents present
            discussion_topic = topic
            if document_context:
                discussion_topic = f"{topic}\n{document_context}"

            # Start discussion with facilitator
            start_message = facilitator.start_discussion(
                discussion_topic, persona_agents
            )
            self.logger.info(f"Facilitator started discussion: {start_message}")

            # Execute discussion rounds
            all_messages: list[Any] = []
            round_summaries: list[str] = []

            total_rounds = facilitator.rounds
            for current_round in range(1, total_rounds + 1):
                self.logger.info(f"Starting round {current_round}/{total_rounds}")

                # ラウンド開始時: 全エージェントの会話履歴をクリア（コンテキスト膨張防止）
                if current_round > 1:
                    for agent in persona_agents:
                        agent.clear_conversation_history()
                        if document_contents:
                            agent.set_document_contents(document_contents.copy())
                    facilitator.clear_conversation_history()
                    self.logger.info(
                        f"Cleared conversation history for round {current_round}"
                    )

                # Track who has spoken in this round and round messages
                spoken_in_round: list[str] = []
                round_messages = []

                # Each persona speaks once per round
                for _ in range(len(persona_agents)):
                    speaker = self._select_next_speaker(persona_agents, spoken_in_round)

                    if speaker is None:
                        break

                    prompt = self._build_persona_prompt(
                        speaker,
                        topic,
                        all_messages[-10:],
                        current_round,
                        total_rounds,
                        round_summaries=round_summaries if round_summaries else None,
                        latest_facilitator_message=round_summaries[-1]
                        if round_summaries
                        else None,
                    )

                    # Get persona's response with token streaming
                    try:
                        # Signal message start
                        yield (
                            "message_start",
                            {
                                "persona_id": speaker.get_persona_id(),
                                "persona_name": speaker.get_persona_name(),
                                "message_type": "statement",
                                "round_number": current_round,
                            },
                        )

                        # Stream tokens
                        full_text = ""
                        for token in speaker.respond_streaming(prompt, None):
                            full_text += token
                            yield (
                                "message_delta",
                                {
                                    "persona_id": speaker.get_persona_id(),
                                    "content": token,
                                },
                            )

                        # Create message and signal end
                        message = Message.create_new(
                            persona_id=speaker.get_persona_id(),
                            persona_name=speaker.get_persona_name(),
                            content=full_text,
                            message_type="statement",
                            round_number=current_round,
                        )
                        all_messages.append(message)
                        round_messages.append(message)

                        yield ("message_end", message)

                        self.logger.info(
                            f"Persona {speaker.get_persona_name()} spoke in round {current_round}"
                        )

                        # Mark this persona as having spoken
                        spoken_in_round.append(speaker.get_persona_id())

                    except AgentCommunicationError as e:
                        self.logger.error(
                            f"Failed to get response from {speaker.get_persona_name()}: {e}"
                        )
                        spoken_in_round.append(speaker.get_persona_id())
                        continue

                # ラウンド終了後にファシリテータがラウンド全体を要約（ストリーミング）
                if round_messages:
                    try:
                        # Signal facilitator message start
                        yield (
                            "message_start",
                            {
                                "persona_id": "facilitator",
                                "persona_name": "ファシリテータ",
                                "message_type": "summary",
                                "round_number": current_round,
                            },
                        )

                        # Stream facilitator summary tokens
                        summary_prompt = self._build_summary_prompt(
                            current_round,
                            round_messages,
                            topic,
                            total_rounds,
                            previous_summaries=round_summaries
                            if round_summaries
                            else None,
                        )
                        round_summary = ""
                        for token in facilitator.invoke_streaming(summary_prompt):
                            round_summary += token
                            yield (
                                "message_delta",
                                {
                                    "persona_id": "facilitator",
                                    "content": token,
                                },
                            )

                        # 要約を蓄積（次ラウンドのコンテキストとして使用）
                        round_summaries.append(round_summary)

                        # Create message for round summary
                        summary_message = Message.create_new(
                            persona_id="facilitator",
                            persona_name="ファシリテータ",
                            content=round_summary,
                            message_type="summary",
                            round_number=current_round,
                        )
                        all_messages.append(summary_message)

                        yield ("message_end", summary_message)

                        self.logger.info(
                            f"Facilitator summarized round {current_round}"
                        )

                    except AgentCommunicationError as e:
                        self.logger.warning(
                            f"Failed to summarize round {current_round}: {e}"
                        )

                self.logger.info(
                    f"Completed round {current_round}/{facilitator.rounds}"
                )

            # Add all messages to discussion
            for message in all_messages:
                discussion = discussion.add_message(message)

            self.logger.info(
                f"Streaming agent discussion completed: {discussion.id} with {len(all_messages)} messages"
            )

            # Yield the complete discussion
            yield ("complete", discussion)

        except AgentDiscussionManagerError:
            # 入力検証・ドキュメント検証（VALIDATION/CAPACITY等）が投げたコード付き例外は
            # そのまま再送出する（DISCUSSION_OPERATION_FAILEDに丸めるとユーザーが取れる
            # 行動の情報が失われる）
            raise
        except Exception as e:
            self.logger.error("Error during streaming agent discussion", exc_info=True)
            raise AgentDiscussionManagerError(
                f"streaming agent discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e

        finally:
            # エージェントリソースを確実に解放
            self.cleanup_agents(persona_agents, facilitator)

    def _validate_discussion_for_save(self, discussion: Discussion) -> None:
        """
        Validate a discussion object before saving.

        Args:
            discussion: Discussion object to validate

        Raises:
            AgentDiscussionManagerError: If validation fails
        """
        if not discussion:
            raise AgentDiscussionManagerError(
                "discussion object is falsy",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        if not discussion.id:
            raise AgentDiscussionManagerError(
                "discussion has no id", code=ErrorCode.DISCUSSION_OPERATION_FAILED
            )

        if not discussion.topic or not discussion.topic.strip():
            raise AgentDiscussionManagerError(
                "discussion has no topic", code=ErrorCode.DISCUSSION_OPERATION_FAILED
            )

        if not discussion.participants or len(discussion.participants) < 2:
            raise AgentDiscussionManagerError(
                f"discussion has {len(discussion.participants or [])} participants",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        if not discussion.created_at:
            raise AgentDiscussionManagerError(
                "discussion has no created_at",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        if discussion.mode != "agent":
            raise AgentDiscussionManagerError(
                f"invalid discussion mode: {discussion.mode!r}, expected 'agent'",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            )

        # Validate messages if present
        if discussion.messages:
            for i, message in enumerate(discussion.messages):
                if not message.persona_id or not message.content:
                    raise AgentDiscussionManagerError(
                        f"message at index {i} has no persona_id or content",
                        code=ErrorCode.DISCUSSION_OPERATION_FAILED,
                    )

    def cleanup_agents(
        self,
        persona_agents: List[PersonaAgent],
        facilitator: Optional[FacilitatorAgent] = None,
    ) -> None:
        """
        エージェントリソースを解放してメモリリークを防ぐ

        Args:
            persona_agents: ペルソナエージェントリスト
            facilitator: ファシリテータエージェント（未作成の場合はNone。
                create_persona_agents内の失敗時はfacilitatorがまだ存在しないため）
        """
        try:
            # ペルソナエージェントのリソース解放
            dispose_agents(
                persona_agents,
                self.logger,
                error_message=lambda _agent,
                e: f"ペルソナエージェントの解放中にエラー: {e}",
            )

            # ファシリテータエージェントのリソース解放
            if facilitator is not None:
                dispose_agents(
                    [facilitator],
                    self.logger,
                    error_message=lambda _agent, e: (
                        f"ファシリテータエージェントの解放中にエラー: {e}"
                    ),
                )

            self.logger.info("全エージェントのリソース解放が完了しました")

        except Exception as e:
            self.logger.error(f"エージェントリソース解放中に予期しないエラー: {e}")

    # =========================================================================
    # ワークフロー制御メソッド（agent_service.py FacilitatorAgent から移動）
    # =========================================================================

    def _select_next_speaker(
        self,
        persona_agents: List[PersonaAgent],
        spoken_in_round: List[str],
    ) -> Optional[PersonaAgent]:
        """
        次の発言者をランダムに選択する。

        Args:
            persona_agents: 参加ペルソナエージェントリスト
            spoken_in_round: 現在のラウンドで既に発言したペルソナIDリスト

        Returns:
            選択されたペルソナエージェント（全員発言済みの場合はNone）
        """
        import random

        available_agents = [
            agent
            for agent in persona_agents
            if agent.get_persona_id() not in spoken_in_round
        ]

        if not available_agents:
            return None

        selected = random.choice(available_agents)
        self.logger.info(f"次の発言者を選択: {selected.get_persona_name()}")
        return selected

    def _build_persona_prompt(
        self,
        persona_agent: PersonaAgent,
        topic: str,
        context: List[Message],
        current_round: int,
        total_rounds: int,
        round_summaries: Optional[List[str]] = None,
        latest_facilitator_message: Optional[str] = None,
    ) -> str:
        """
        ペルソナエージェントへの発言促進プロンプトを生成する。

        Args:
            persona_agent: 対象ペルソナエージェント
            topic: 議論テーマ
            context: 直近の発言メッセージ
            current_round: 現在のラウンド番号
            total_rounds: 総ラウンド数
            round_summaries: 各ラウンドの要約リスト
            latest_facilitator_message: ファシリテータの最新要約
        """
        persona_id = persona_agent.get_persona_id()
        is_first_round = current_round == 1

        if not context and not round_summaries:
            return (
                f"議論テーマ「{topic}」について話し合います。\n\n"
                f"まず、あなたの日常生活の中でこのテーマに関連する具体的な場面を一つ挙げて、"
                f"そこで感じたこと・困ったこと・考えたことを率直に話してください。"
            )

        parts = [f"「{topic}」についての議論を続けてください。\n"]

        if round_summaries:
            past_summaries = (
                round_summaries[:-1] if latest_facilitator_message else round_summaries
            )
            if past_summaries:
                parts.append("## これまでの議論の要約")
                for i, summary in enumerate(past_summaries, 1):
                    parts.append(f"ラウンド{i}: {summary}")
                parts.append("")

        if latest_facilitator_message:
            parts.append("## ファシリテータからの問いかけ")
            parts.append(latest_facilitator_message)
            parts.append("")

        if context:
            own_previous = [msg for msg in context if msg.persona_id == persona_id]
            if own_previous:
                parts.append("## あなたの前回の発言")
                parts.append(own_previous[-1].content)
                parts.append("")

        if context:
            recent_others = [
                msg
                for msg in context
                if msg.persona_id != "facilitator" and msg.persona_id != persona_id
            ][-3:]
            if recent_others:
                parts.append("## 直近の他の参加者の発言")
                for msg in recent_others:
                    parts.append(f"- {msg.persona_name}: {msg.content}")
                parts.append("")

        if is_first_round:
            parts.append(
                "このラウンドでは、まずあなた自身の体験を共有してください。"
                "このテーマに関連する日常の具体的な場面を挙げて、そこで感じたこと・困ったことを話し、"
                "他の参加者の体験も踏まえて意見を述べてください。"
            )
        elif current_round < total_rounds:
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
            parts.append("\nファシリテータの問いかけの観点にも着目してください。")

        return "\n".join(parts)

    def _build_summary_prompt(
        self,
        round_number: int,
        round_messages: List[Message],
        topic: str,
        total_rounds: int,
        previous_summaries: Optional[List[str]] = None,
    ) -> str:
        """
        ラウンド要約用プロンプトを構築する。

        Args:
            round_number: ラウンド番号
            round_messages: そのラウンドのメッセージリスト
            topic: 議論トピック
            total_rounds: 総ラウンド数
            previous_summaries: 過去ラウンドの要約リスト
        """
        statements = [
            msg
            for msg in round_messages
            if msg.message_type == "statement" and msg.persona_id != "facilitator"
        ]

        if not statements:
            return f"ラウンド{round_number}では発言がありませんでした。"

        statements_text = "\n".join(
            [f"- {msg.persona_name}: {msg.content}" for msg in statements]
        )

        parts = [
            f"議論テーマ「{topic}」のラウンド{round_number}/{total_rounds}が完了しました。\n"
        ]

        if previous_summaries:
            parts.append("## これまでの議論の流れ")
            for i, summary in enumerate(previous_summaries, 1):
                parts.append(f"ラウンド{i}: {summary}")
            parts.append("")

        parts.append(f"## ラウンド{round_number}の発言")
        parts.append(statements_text)
        parts.append("")

        if round_number < total_rounds:
            parts.append(
                "以下の観点で簡潔に要約してください:\n"
                "- 各参加者の主要な意見や立場\n"
                "- 参加者間の共通点や対立点\n"
                "- まだ掘り下げられていない重要な観点\n"
                "- 各ペルソナに次のラウンドで答えてほしい具体的な問い（1-2個）\n"
                f"残り{total_rounds - round_number}ラウンドです。"
            )
            if total_rounds - round_number <= 2:
                parts.append("論点を絞り込み、結論に向けて議論を収束させてください。")
            parts.append("3-5文で要約し、最後に問いかけで締めてください。")
        else:
            parts.append(
                "最終ラウンドが完了しました。以下の観点で議論全体をまとめてください:\n"
                "- 議論を通じて明らかになった主要な結論\n"
                "- 参加者間で合意に至った点と残った対立点\n"
                "- 議論テーマの目的に対する具体的な示唆\n"
                "5-7文で最終まとめを作成してください。"
            )

        return "\n".join(parts)

    # =========================================================================
    # フルフローAPI（Router層のワークフローロジックをManager層に統合）
    # =========================================================================

    def run_agent_discussion_streaming(
        self,
        personas: List[Persona],
        topic: str,
        rounds: int = 3,
        additional_instructions: str = "",
        enable_memory: bool = False,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
        categories: Optional[List[InsightCategory]] = None,
        document_ids: Optional[List[str]] = None,
        persona_models: Optional[Dict[str, str]] = None,
        facilitator_model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """agentモード議論のフルフロー（ストリーミング）。

        1. 入力バリデーション（memory_mode含む）
        2. ペルソナエージェント作成
        3. ファシリテーターエージェント作成
        4. 議論実行（メッセージイベントをSSE文字列としてyield）
        5. インサイト生成
        6. カテゴリー保存
        7. DB保存
        8. 完了イベントをyield

        Args:
            persona_models: persona_id -> model_id のマップ（省略時は既定モデル）
            facilitator_model: facilitatorのmodel_id（省略時は既定モデル）

        Yields:
            str: SSE data行（"data: {...}\\n\\n" 形式）

        Raises:
            AgentDiscussionManagerError: バリデーション失敗、エージェント/DB例外
        """
        self._validate_memory_mode(memory_mode)

        temp_discussion = Discussion.create_new(
            topic=topic, participants=[p.id for p in personas], mode="agent"
        )
        session_id = temp_discussion.id

        persona_agents = self.create_persona_agents(
            personas,
            {},
            enable_memory=enable_memory,
            session_id=session_id,
            memory_mode=memory_mode,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=persona_models,
        )

        try:
            facilitator = self.create_facilitator_agent(
                rounds, additional_instructions, facilitator_model=facilitator_model
            )
        except Exception:
            # facilitator作成失敗時、start_agent_discussion_streamingのfinallyに
            # 到達しないため、作成済みのpersona_agentsをここで解放する
            self.cleanup_agents(persona_agents)
            raise

        discussion = None
        message_count = 0

        for event_type, data in self.start_agent_discussion_streaming(
            personas=personas,
            topic=topic,
            persona_agents=persona_agents,
            facilitator=facilitator,
            enable_memory=enable_memory,
            document_ids=document_ids,
            persona_models=persona_models,
            facilitator_model=facilitator_model,
        ):
            if event_type == "message_start":
                yield f"data: {json.dumps({'type': 'message_start', **data}, ensure_ascii=False)}\n\n"
            elif event_type == "message_delta":
                yield f"data: {json.dumps({'type': 'message_delta', **data}, ensure_ascii=False)}\n\n"
            elif event_type == "message_end":
                message_count += 1
                msg_data = {
                    "type": "message_end",
                    "persona_id": data.persona_id,
                    "persona_name": data.persona_name,
                    "content": data.content,
                    "message_type": data.message_type,
                }
                yield f"data: {json.dumps(msg_data, ensure_ascii=False)}\n\n"
            elif event_type == "complete":
                discussion = data

        if discussion:
            discussion = self._attach_insights(discussion, categories)
            self.save_agent_discussion(discussion)

            complete_data = {
                "type": "complete",
                "discussion_id": discussion.id,
                "message_count": message_count,
                "insight_count": len(discussion.insights) if discussion.insights else 0,
            }
            yield f"data: {json.dumps(complete_data, ensure_ascii=False)}\n\n"

    def run_agent_discussion_full(
        self,
        personas: List[Persona],
        topic: str,
        rounds: int = 3,
        additional_instructions: str = "",
        enable_memory: bool = False,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
        categories: Optional[List[InsightCategory]] = None,
        document_ids: Optional[List[str]] = None,
        persona_models: Optional[Dict[str, str]] = None,
        facilitator_model: Optional[str] = None,
    ) -> Discussion:
        """agentモード議論のフルフロー（非ストリーミング）。

        Args:
            persona_models: persona_id -> model_id のマップ（省略時は既定モデル）
            facilitator_model: facilitatorのmodel_id（省略時は既定モデル）

        Returns:
            Discussion: インサイト付き保存済み議論オブジェクト

        Raises:
            AgentDiscussionManagerError: バリデーション失敗、エージェント/DB例外
        """
        self._validate_memory_mode(memory_mode)

        temp_discussion = Discussion.create_new(
            topic=topic, participants=[p.id for p in personas], mode="agent"
        )
        session_id = temp_discussion.id

        persona_agents = self.create_persona_agents(
            personas,
            {},
            enable_memory=enable_memory,
            session_id=session_id,
            memory_mode=memory_mode,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=persona_models,
        )

        try:
            facilitator = self.create_facilitator_agent(
                rounds, additional_instructions, facilitator_model=facilitator_model
            )
        except Exception:
            # facilitator作成失敗時、start_agent_discussionのfinallyに
            # 到達しないため、作成済みのpersona_agentsをここで解放する
            self.cleanup_agents(persona_agents)
            raise

        discussion = self.start_agent_discussion(
            personas=personas,
            topic=topic,
            persona_agents=persona_agents,
            facilitator=facilitator,
            enable_memory=enable_memory,
            document_ids=document_ids,
            persona_models=persona_models,
            facilitator_model=facilitator_model,
        )

        discussion = self._attach_insights(discussion, categories)
        self.save_agent_discussion(discussion)
        return discussion

    def _validate_memory_mode(self, memory_mode: str) -> None:
        """memory_modeのバリデーション。"""
        valid_modes = ["full", "retrieve_only", "disabled"]
        if memory_mode not in valid_modes:
            raise AgentDiscussionManagerError(
                f"invalid memory_mode {memory_mode!r}, expected one of {valid_modes}",
                code=ErrorCode.DISCUSSION_MEMORY_MODE_INVALID,
            )

    def _attach_insights(
        self,
        discussion: Discussion,
        categories: Optional[List[InsightCategory]],
    ) -> Discussion:
        """インサイト生成・カテゴリー保存の共通処理。"""
        from .shared.insight_utils import attach_insights_to_discussion

        return attach_insights_to_discussion(
            discussion=discussion,
            categories=categories,
            ai_service=self._get_ai_service(),
            logger=self.logger,
        )

    def _get_ai_service(self) -> Any:
        """インサイト生成用にAIServiceを取得する。"""
        return service_factory.get_ai_service()
