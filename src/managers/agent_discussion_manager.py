"""
Agent Discussion Manager for AI Persona System.
Handles AI agent mode discussion setup, execution, and persistence.
"""

import logging
from typing import List, Dict, Optional, Any

from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.message import Message
from ..services.agent_service import (
    AgentService,
    PersonaAgent,
    FacilitatorAgent,
    AgentInitializationError,
    AgentCommunicationError,
)
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory


class AgentDiscussionManagerError(Exception):
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

        Returns:
            List[PersonaAgent]: Created persona agents

        Raises:
            AgentDiscussionManagerError: If agent creation fails
        """
        if not personas:
            raise AgentDiscussionManagerError("ペルソナリストが空です")

        if len(personas) < 2:
            raise AgentDiscussionManagerError("議論には最低2つのペルソナが必要です")

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
                    self.agent_service.generate_persona_system_prompt(persona),
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
                )
                persona_agents.append(persona_agent)

                self.logger.info(f"Created persona agent: {persona.name}")

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
            error_msg = f"Failed to create enough persona agents. Failed: {', '.join(failed_personas)}"
            raise AgentDiscussionManagerError(error_msg)

        if failed_personas:
            self.logger.warning(
                f"Some persona agents failed to initialize: {', '.join(failed_personas)}"
            )

        self.logger.info(f"Successfully created {len(persona_agents)} persona agents")
        return persona_agents

    def create_facilitator_agent(
        self, rounds: int, additional_instructions: str = ""
    ) -> FacilitatorAgent:
        """
        Create facilitator agent for discussion management.

        Args:
            rounds: Number of discussion rounds
            additional_instructions: Additional instructions for facilitator

        Returns:
            FacilitatorAgent: Created facilitator agent

        Raises:
            AgentDiscussionManagerError: If facilitator creation fails
        """
        if rounds < 1:
            raise AgentDiscussionManagerError("ラウンド数は1以上である必要があります")

        if rounds > 10:
            raise AgentDiscussionManagerError("ラウンド数は10以下である必要があります")

        self.logger.info(f"Creating facilitator agent with {rounds} rounds")

        try:
            facilitator = self.agent_service.create_facilitator_agent(
                rounds, additional_instructions
            )

            self.logger.info("Successfully created facilitator agent")
            return facilitator

        except AgentInitializationError as e:
            error_msg = f"Failed to create facilitator agent: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error creating facilitator agent: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)

    def start_agent_discussion(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
        enable_memory: bool = False,
        document_ids: Optional[List[str]] = None,
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

        Returns:
            Discussion: Discussion object with generated messages

        Raises:
            AgentDiscussionManagerError: If discussion execution fails

        Requirements:
            - 6.3: Pass memory configuration through discussion flow
        """
        # Validate input
        self._validate_discussion_input(personas, topic, persona_agents, facilitator)

        # Load documents if provided
        documents_metadata, document_context, document_contents = (
            self._load_and_attach_documents(document_ids, persona_agents)
        )

        self.logger.info(
            f"Starting agent discussion with {len(persona_agents)} agents on topic: '{topic[:50]}...' "
            f"(enable_memory={enable_memory}, documents={len(documents_metadata) if documents_metadata else 0})"
        )

        try:
            # Create agent_config with facilitator settings and memory configuration
            agent_config = {
                "rounds": facilitator.rounds,
                "additional_instructions": facilitator.additional_instructions,
                "enable_memory": enable_memory,
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
            error_msg = f"Agent communication error during discussion: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)
        except DiscussionFlowError as e:
            error_msg = f"Discussion flow error: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during agent discussion: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)
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
            raise AgentDiscussionManagerError("議論オブジェクトが無効です")

        if discussion.mode != "agent":
            raise AgentDiscussionManagerError(
                f"Invalid discussion mode: {discussion.mode}. Expected 'agent'"
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
            error_msg = f"Database error while saving agent discussion: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while saving agent discussion: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)

    def _load_and_attach_documents(
        self,
        document_ids: Optional[List[str]],
        persona_agents: List[PersonaAgent],
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
            raise AgentDiscussionManagerError("議論参加ペルソナが指定されていません")

        if len(personas) < 2:
            raise AgentDiscussionManagerError("議論には最低2つのペルソナが必要です")

        # Validate topic
        if not topic or not topic.strip():
            raise AgentDiscussionManagerError("議論トピックが空です")

        if len(topic.strip()) < 5:
            raise AgentDiscussionManagerError(
                "議論トピックが短すぎます。5文字以上で入力してください"
            )

        if len(topic.strip()) > 200:
            raise AgentDiscussionManagerError(
                "議論トピックが長すぎます。200文字以内で入力してください"
            )

        # Validate persona agents
        if not persona_agents:
            raise AgentDiscussionManagerError(
                "ペルソナエージェントが作成されていません"
            )

        if len(persona_agents) < 2:
            raise AgentDiscussionManagerError(
                "議論には最低2つのペルソナエージェントが必要です"
            )

        # Validate facilitator
        if not facilitator:
            raise AgentDiscussionManagerError(
                "ファシリテータエージェントが作成されていません"
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
            raise DiscussionFlowError("生成された議論が無効です")

        if not discussion.messages:
            raise DiscussionFlowError("議論にメッセージが含まれていません")

        if len(discussion.messages) < 2:
            raise DiscussionFlowError("議論メッセージが少なすぎます")

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
    ) -> PersonaAgent:
        """統合機能（KB、データセット）付きペルソナエージェントを作成。両方同時に有効化可能。"""
        from src.services.service_factory import service_factory

        db_service = service_factory.get_database_service()
        additional_tools = []
        enhanced_prompt = system_prompt

        # KB連携：ツールとプロンプト拡張を準備
        if enable_kb:
            kb_binding = db_service.get_kb_binding_by_persona(persona.id)
            if kb_binding:
                kb = db_service.get_knowledge_base(kb_binding.kb_id)
                if kb:
                    from src.services.knowledge_base.kb_tools import (
                        create_kb_retrieval_tool,
                    )
                    from src.config import config

                    kb_tool = create_kb_retrieval_tool(
                        knowledge_base_id=kb.knowledge_base_id,
                        metadata_filters=kb_binding.metadata_filters,
                        region=config.AWS_REGION,
                    )
                    additional_tools.append(kb_tool)
                    enhanced_prompt = self.agent_service._enhance_prompt_with_kb_info(
                        enhanced_prompt,
                        kb.name,
                        kb.description,
                        kb_binding.metadata_filters,
                    )

        # データセット連携：ツールとプロンプト拡張を準備
        if enable_dataset:
            bindings = db_service.get_bindings_by_persona(persona.id)
            if bindings:
                dataset_ids = list(set(b.dataset_id for b in bindings))
                datasets = [db_service.get_dataset(did) for did in dataset_ids]
                datasets = [d for d in datasets if d is not None]
                bindings_dict = [
                    {"dataset_id": b.dataset_id, "binding_keys": b.binding_keys}
                    for b in bindings
                ]
                enhanced_prompt = self.agent_service._enhance_prompt_with_dataset_info(
                    enhanced_prompt, bindings_dict, datasets
                )
                from src.services.mcp_server_manager import get_mcp_manager

                mcp_manager = get_mcp_manager()
                if not mcp_manager.is_running():
                    mcp_manager.start()
                if mcp_manager.is_running():
                    mcp_tools = mcp_manager.get_tools()
                    if mcp_tools:
                        additional_tools.extend(mcp_tools)

        return self.agent_service.create_persona_agent(
            persona=persona,
            system_prompt=enhanced_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            additional_tools=additional_tools if additional_tools else None,
            memory_mode=memory_mode,
        )

    def start_agent_discussion_streaming(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
        enable_memory: bool = False,
        document_ids: Optional[List[str]] = None,
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

        Yields:
            tuple: (message_type, message_or_discussion)
                - ("message", Message): Individual message
                - ("complete", Discussion): Final discussion object

        Raises:
            AgentDiscussionManagerError: If discussion execution fails

        Requirements:
            - 6.3: Pass memory configuration through discussion flow
        """
        # Validate input
        self._validate_discussion_input(personas, topic, persona_agents, facilitator)

        # Load documents if provided
        documents_metadata, document_context, document_contents = (
            self._load_and_attach_documents(document_ids, persona_agents)
        )

        self.logger.info(
            f"Starting streaming agent discussion with {len(persona_agents)} agents "
            f"(enable_memory={enable_memory}, documents={len(documents_metadata) if documents_metadata else 0})"
        )

        try:
            # Create agent_config with facilitator settings and memory configuration
            agent_config = {
                "rounds": facilitator.rounds,
                "additional_instructions": facilitator.additional_instructions,
                "enable_memory": enable_memory,
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

        except Exception as e:
            error_msg = f"Error during streaming agent discussion: {e}"
            self.logger.error(error_msg)
            raise AgentDiscussionManagerError(error_msg)

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
            raise AgentDiscussionManagerError("議論オブジェクトが無効です")

        if not discussion.id:
            raise AgentDiscussionManagerError("議論IDが設定されていません")

        if not discussion.topic or not discussion.topic.strip():
            raise AgentDiscussionManagerError("議論トピックが設定されていません")

        if not discussion.participants or len(discussion.participants) < 2:
            raise AgentDiscussionManagerError("議論参加者が不足しています")

        if not discussion.created_at:
            raise AgentDiscussionManagerError("議論作成日時が設定されていません")

        if discussion.mode != "agent":
            raise AgentDiscussionManagerError(
                f"Invalid discussion mode: {discussion.mode}. Expected 'agent'"
            )

        # Validate messages if present
        if discussion.messages:
            for i, message in enumerate(discussion.messages):
                if not message.persona_id or not message.content:
                    raise AgentDiscussionManagerError(f"メッセージ {i + 1} が無効です")

    def cleanup_agents(
        self, persona_agents: List[PersonaAgent], facilitator: FacilitatorAgent
    ) -> None:
        """
        エージェントリソースを解放してメモリリークを防ぐ

        Args:
            persona_agents: ペルソナエージェントリスト
            facilitator: ファシリテータエージェント
        """
        try:
            # ペルソナエージェントのリソース解放
            for agent in persona_agents:
                try:
                    agent.dispose()
                except Exception as e:
                    self.logger.warning(f"ペルソナエージェントの解放中にエラー: {e}")

            # ファシリテータエージェントのリソース解放
            try:
                facilitator.dispose()
            except Exception as e:
                self.logger.warning(f"ファシリテータエージェントの解放中にエラー: {e}")

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
