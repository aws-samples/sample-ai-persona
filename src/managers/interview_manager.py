"""
Interview Manager for AI Persona System.
Handles interview session setup, execution, and persistence.
Extends AgentDiscussionManager for interview functionality.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime

from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.message import Message
from ..services.agent_service import (
    AgentService,
    PersonaAgent,
    AgentInitializationError,
    AgentCommunicationError,
)
from ..services.database_service import DatabaseService, DatabaseError
from .agent_discussion_manager import AgentDiscussionManager


class InterviewManagerError(Exception):
    """Base exception for interview manager related errors."""

    pass


class InterviewSessionError(InterviewManagerError):
    """Interview session related errors."""

    pass


class InterviewSessionNotFoundError(InterviewManagerError):
    """Interview session not found error."""

    pass


class InterviewValidationError(InterviewManagerError):
    """Interview validation related errors."""

    pass


class InterviewAgentError(InterviewManagerError):
    """Interview agent related errors."""

    pass


class InterviewPersistenceError(InterviewManagerError):
    """Interview persistence related errors."""

    pass


@dataclass
class InterviewSession:
    """
    Represents an active interview session.
    """

    id: str
    participants: List[str]  # persona_ids
    messages: List[Message]
    created_at: datetime
    is_saved: bool = False
    enable_memory: bool = False
    documents: Optional[List[Dict[str, Any]]] = None  # Attached documents metadata
    memory_mode: str = "full"  # "full", "retrieve_only", or "disabled"
    enable_dataset: bool = False  # Whether external dataset access is enabled

    def add_user_message(self, content: str) -> "InterviewSession":
        """
        Add a user message to the interview session.

        Args:
            content: User message content

        Returns:
            New InterviewSession instance with the user message added
        """
        user_message = Message.create_new(
            persona_id="user",
            persona_name="User",
            content=content,
            message_type="user_message",
        )

        new_messages = self.messages + [user_message]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=new_messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=self.documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )

    def add_persona_response(
        self, persona_id: str, persona_name: str, content: str
    ) -> "InterviewSession":
        """
        Add a persona response to the interview session.

        Args:
            persona_id: ID of the responding persona
            persona_name: Name of the responding persona
            content: Persona response content

        Returns:
            New InterviewSession instance with the persona response added
        """
        response_message = Message.create_new(
            persona_id=persona_id,
            persona_name=persona_name,
            content=content,
            message_type="statement",
        )

        new_messages = self.messages + [response_message]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=new_messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=self.documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )

    def add_document(self, document_metadata: Dict[str, Any]) -> "InterviewSession":
        """
        Add a document to the interview session.

        Args:
            document_metadata: Document metadata (filename, mime_type, file_size, etc.)

        Returns:
            New InterviewSession instance with the document added
        """
        current_docs = self.documents or []
        new_documents = current_docs + [document_metadata]
        return InterviewSession(
            id=self.id,
            participants=self.participants,
            messages=self.messages,
            created_at=self.created_at,
            is_saved=self.is_saved,
            enable_memory=self.enable_memory,
            documents=new_documents,
            memory_mode=self.memory_mode,
            enable_dataset=self.enable_dataset,
        )


class InterviewManager(AgentDiscussionManager):
    """
    Manager class for handling interview operations.
    Extends AgentDiscussionManager to provide interview-specific functionality.
    """

    def __init__(
        self,
        agent_service: AgentService | None = None,
        database_service: Optional[DatabaseService] = None,
    ):
        """
        Initialize interview manager.

        Args:
            agent_service: Agent service instance for agent management (optional, uses singleton if not provided)
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
        """
        super().__init__(agent_service, database_service)
        self.logger = logging.getLogger(__name__)

        # Active interview sessions (temporary storage)
        self._active_sessions: Dict[str, InterviewSession] = {}
        self._session_agents: Dict[str, List[PersonaAgent]] = {}

        self.logger.info("Interview Manager initialized")

    def start_interview_session(
        self,
        personas: List[Persona],
        user_id: str = "user",
        enable_memory: bool = False,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
    ) -> InterviewSession:
        """
        Start a new interview session with selected personas.

        Args:
            personas: List of personas to participate in the interview
            user_id: ID of the user conducting the interview
            enable_memory: Whether to enable long-term memory for persona agents
            memory_mode: Memory mode (default: "full")
                - "full": 検索 + 保存
                - "retrieve_only": 検索のみ（保存しない）
                - "disabled": メモリ機能無効
            enable_dataset: Whether to enable external dataset access (default: False)

        Returns:
            InterviewSession: Created interview session

        Raises:
            InterviewValidationError: If input validation fails
            InterviewAgentError: If agent creation fails
            InterviewSessionError: If session creation fails
        """
        # Enhanced input validation
        self._validate_session_creation_input(personas, user_id)

        # Validate memory_mode
        valid_memory_modes = ["full", "retrieve_only", "disabled"]
        if memory_mode not in valid_memory_modes:
            raise InterviewValidationError(
                f"無効なmemory_modeです: {memory_mode}。有効な値: {', '.join(valid_memory_modes)}"
            )

        self.logger.info(
            f"Starting interview session with {len(personas)} personas for user: {user_id} (enable_memory={enable_memory}, memory_mode={memory_mode}, enable_dataset={enable_dataset}, enable_kb={enable_kb})"
        )

        session_id = None
        persona_agents = []

        try:
            # Create interview session
            session = InterviewSession(
                id=str(uuid.uuid4()),
                participants=[persona.id for persona in personas],
                messages=[],
                created_at=datetime.now(),
                is_saved=False,
                enable_memory=enable_memory,
                memory_mode=memory_mode,
                enable_dataset=enable_dataset,
            )
            session_id = session.id

            # Create persona agents for the interview with enhanced error handling
            try:
                system_prompts = {}
                for persona in personas:
                    try:
                        # Generate interview-specific system prompt
                        system_prompt = self._generate_interview_system_prompt(persona)
                        system_prompts[persona.id] = system_prompt
                    except Exception as e:
                        error_msg = f"Failed to generate system prompt for persona {persona.name}: {e}"
                        self.logger.error(error_msg)
                        raise InterviewAgentError(error_msg)

                # Create persona agents for interview (allows single persona)
                # Pass memory and dataset configuration
                persona_agents = self._create_interview_persona_agents(
                    personas,
                    system_prompts,
                    enable_memory=enable_memory,
                    session_id=session_id,
                    memory_mode=memory_mode,
                    enable_dataset=enable_dataset,
                    enable_kb=enable_kb,
                )

            except AgentInitializationError as e:
                error_msg = f"エージェントの初期化に失敗しました: {e}"
                self.logger.error(error_msg)
                raise InterviewAgentError(error_msg)
            except InterviewAgentError:
                # Re-raise InterviewAgentError as-is
                raise
            except Exception as e:
                error_msg = (
                    f"ペルソナエージェントの作成中に予期しないエラーが発生しました: {e}"
                )
                self.logger.error(error_msg)
                raise InterviewAgentError(error_msg)

            # Store active session and agents
            self._active_sessions[session.id] = session
            self._session_agents[session.id] = persona_agents

            self.logger.info(f"Interview session created successfully: {session.id}")
            return session

        except (InterviewValidationError, InterviewAgentError):
            # Clean up any partially created resources
            self._cleanup_failed_session(session_id, persona_agents)
            raise
        except Exception as e:
            # Clean up any partially created resources
            self._cleanup_failed_session(session_id, persona_agents)
            error_msg = (
                f"インタビューセッションの作成中に予期しないエラーが発生しました: {e}"
            )
            self.logger.error(error_msg)
            raise InterviewSessionError(error_msg)

    def send_user_message(
        self,
        session_id: str,
        message: str,
        document_contents: Optional[List[Dict[str, Any]]] = None,
        document_metadata: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Message]:
        """
        Send a user message to the interview session and get persona responses.

        Args:
            session_id: ID of the interview session
            message: User message content
            document_contents: Optional list of document ContentBlocks for multimodal input
                画像の場合: {"image": {"format": "png", "source": {"bytes": bytes}}}
                ドキュメントの場合: {"document": {"name": str, "format": str, "source": {"bytes": bytes}}}
            document_metadata: Optional list of document metadata for display
                {"filename": str, "mime_type": str, "file_size": int, "uploaded_at": str}

        Returns:
            List[Message]: List of persona responses

        Raises:
            InterviewSessionNotFoundError: If session not found
            InterviewValidationError: If message validation fails
            InterviewAgentError: If agent communication fails
        """
        # Enhanced session validation
        self._validate_session_exists(session_id)
        self._validate_message_input(message)

        session = self._active_sessions[session_id]

        # Check if session is already saved (agents may have been cleaned up)
        if session.is_saved:
            error_msg = f"保存済みのセッション {session_id} では新しいメッセージを送信できません"
            self.logger.error(error_msg)
            raise InterviewSessionError(error_msg)

        persona_agents = self._session_agents.get(session_id, [])

        if not persona_agents:
            error_msg = f"セッション {session_id} にペルソナエージェントが見つかりません（既に保存済みの可能性があります）"
            self.logger.error(error_msg)
            raise InterviewAgentError(error_msg)

        self.logger.info(
            f"Processing user message in session {session_id}: '{message[:50]}...' (documents: {len(document_contents) if document_contents else 0})"
        )

        try:
            # Add user message to session
            session = session.add_user_message(message.strip())

            # ドキュメントメタデータがある場合、セッションに追加（重複チェック）
            if document_metadata:
                for doc_meta in document_metadata:
                    # 重複チェック（同じファイル名とサイズの組み合わせ）
                    existing_docs = session.documents or []
                    is_duplicate = any(
                        d.get("filename") == doc_meta.get("filename")
                        and d.get("file_size") == doc_meta.get("file_size")
                        for d in existing_docs
                    )
                    if not is_duplicate:
                        session = session.add_document(doc_meta)
                        self.logger.info(
                            f"Added document metadata: {doc_meta.get('filename')}"
                        )

            # ドキュメントコンテンツがある場合、各エージェントに設定
            if document_contents:
                for persona_agent in persona_agents:
                    persona_agent.set_document_contents(document_contents.copy())
                self.logger.info(
                    f"Set {len(document_contents)} document contents to {len(persona_agents)} agents"
                )

            # Generate responses from all participating personas with enhanced error handling
            responses = []
            failed_agents = []

            for persona_agent in persona_agents:
                agent_name = persona_agent.get_persona_name()
                try:
                    # Create interview-specific prompt for persona
                    prompt = self._create_interview_prompt(
                        persona_agent, message, session.messages
                    )

                    # Get persona response with timeout handling
                    response_content = self._get_agent_response_with_retry(
                        persona_agent, prompt, session.messages
                    )

                    # Add persona response to session
                    session = session.add_persona_response(
                        persona_agent.get_persona_id(),
                        persona_agent.get_persona_name(),
                        response_content,
                    )

                    # Create response message for return
                    response_message = Message.create_new(
                        persona_id=persona_agent.get_persona_id(),
                        persona_name=persona_agent.get_persona_name(),
                        content=response_content,
                        message_type="statement",
                    )
                    responses.append(response_message)

                    self.logger.info(f"Generated response from {agent_name}")

                except AgentCommunicationError as e:
                    error_msg = f"ペルソナ {agent_name} からの応答取得に失敗: {e}"
                    self.logger.error(error_msg)
                    failed_agents.append(agent_name)
                    continue
                except Exception as e:
                    error_msg = f"ペルソナ {agent_name} の処理中に予期しないエラー: {e}"
                    self.logger.error(error_msg)
                    failed_agents.append(agent_name)
                    continue

            # Check if we got at least some responses
            if not responses and failed_agents:
                error_msg = f"すべてのペルソナエージェントが応答に失敗しました: {', '.join(failed_agents)}"
                self.logger.error(error_msg)
                raise InterviewAgentError(error_msg)

            # Update stored session
            self._active_sessions[session_id] = session

            # Log warnings for failed agents
            if failed_agents:
                self.logger.warning(
                    f"一部のペルソナエージェントが失敗しました: {', '.join(failed_agents)}"
                )

            self.logger.info(
                f"Processed user message, generated {len(responses)} responses"
            )
            return responses

        except (
            InterviewSessionNotFoundError,
            InterviewValidationError,
            InterviewAgentError,
        ):
            # Re-raise specific errors as-is
            raise
        except Exception as e:
            error_msg = f"メッセージ処理中に予期しないエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise InterviewAgentError(error_msg)

    def save_interview_session(self, session_id: str, session_name: str | None = None) -> str:
        """
        Save an interview session to the database with enhanced validation and error handling.

        Args:
            session_id: ID of the interview session to save
            session_name: Name for the session (used as topic)

        Returns:
            str: The discussion ID

        Raises:
            InterviewSessionNotFoundError: If session not found
            InterviewValidationError: If session validation fails
            InterviewPersistenceError: If database save fails
        """
        # Enhanced session validation
        self._validate_session_exists(session_id)
        session = self._active_sessions[session_id]

        # Enhanced validation with specific error messages
        try:
            self._validate_session_for_save(session)
        except Exception as e:
            raise InterviewValidationError(f"セッション保存の検証に失敗しました: {e}")

        # Check if already saved
        if session.is_saved:
            self.logger.warning(f"Interview session already saved: {session_id}")
            try:
                existing_discussion_id = self._find_existing_discussion(session)
                if existing_discussion_id:
                    return existing_discussion_id
            except Exception as e:
                self.logger.warning(f"Failed to find existing discussion: {e}")

        self.logger.info(
            f"Saving interview session: {session_id} with {len(session.messages)} messages"
        )

        try:
            # Create Discussion object from interview session with preserved timestamps
            discussion = self._create_discussion_from_session(session, session_name)

            # Save to database with retry logic
            discussion_id = self._save_discussion_with_retry(discussion)

            # Mark session as saved
            session.is_saved = True
            self._active_sessions[session_id] = session

            # Clean up persona agents after successful save to free resources
            self._cleanup_session_agents(session_id)

            self.logger.info(
                f"Interview session saved successfully: {session_id} -> {discussion_id}"
            )
            return discussion_id

        except DatabaseError as e:
            error_msg = f"データベースエラーによりセッションの保存に失敗しました: {e}"
            self.logger.error(error_msg)
            raise InterviewPersistenceError(error_msg)
        except Exception as e:
            error_msg = f"セッション保存中に予期しないエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise InterviewPersistenceError(error_msg)

    def get_interview_session(self, session_id: str) -> InterviewSession:
        """
        Get an active interview session.

        Args:
            session_id: ID of the interview session

        Returns:
            InterviewSession: The interview session

        Raises:
            InterviewSessionNotFoundError: If session not found
        """
        self._validate_session_exists(session_id)
        return self._active_sessions[session_id]

    def end_interview_session(self, session_id: str) -> None:
        """
        End an interview session and clean up resources.

        Args:
            session_id: ID of the interview session to end

        Raises:
            InterviewSessionNotFoundError: If session not found
            InterviewSessionError: If cleanup fails
        """
        self._validate_session_exists(session_id)

        self.logger.info(f"Ending interview session: {session_id}")

        cleanup_errors = []

        try:
            # Clean up persona agents with detailed error tracking
            persona_agents = self._session_agents.get(session_id, [])
            for i, agent in enumerate(persona_agents):
                try:
                    agent_name = (
                        agent.get_persona_name()
                        if hasattr(agent, "get_persona_name")
                        else f"Agent-{i}"
                    )
                    agent.dispose()
                    self.logger.debug(f"Successfully disposed agent: {agent_name}")
                except Exception as e:
                    error_msg = f"Error disposing agent {agent_name}: {e}"
                    self.logger.warning(error_msg)
                    cleanup_errors.append(error_msg)

            # Remove from active sessions
            try:
                del self._active_sessions[session_id]
                self.logger.debug(f"Removed session from active sessions: {session_id}")
            except KeyError as e:
                self.logger.warning(
                    f"Session not found in active sessions during cleanup: {e}"
                )

            try:
                if session_id in self._session_agents:
                    del self._session_agents[session_id]
                    self.logger.debug(f"Removed session agents: {session_id}")
            except KeyError as e:
                self.logger.warning(f"Session agents not found during cleanup: {e}")

            if cleanup_errors:
                self.logger.warning(
                    f"Session ended with {len(cleanup_errors)} cleanup warnings: {session_id}"
                )
            else:
                self.logger.info(f"Interview session ended successfully: {session_id}")

        except Exception as e:
            error_msg = f"セッション終了中に予期しないエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise InterviewSessionError(error_msg)

    def _create_interview_persona_agents(
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
        Create persona agents for interview (allows single persona unlike discussion mode).

        Args:
            personas: List of Persona objects
            system_prompts: Dictionary mapping persona_id to system_prompt
            enable_memory: Whether to enable long-term memory for agents
            session_id: Interview session ID for memory association
            memory_mode: Memory mode (default: "full")
                - "full": 検索 + 保存
                - "retrieve_only": 検索のみ（保存しない）
                - "disabled": メモリ機能無効
            enable_dataset: Whether to enable external dataset access (default: False)
            enable_kb: Whether to enable knowledge base access (default: False)

        Returns:
            List[PersonaAgent]: Created persona agents

        Raises:
            InterviewManagerError: If agent creation fails
        """
        if not personas:
            raise InterviewManagerError("ペルソナリストが空です")

        self.logger.info(
            f"Creating {len(personas)} persona agents for interview (enable_memory={enable_memory}, memory_mode={memory_mode}, enable_dataset={enable_dataset}, enable_kb={enable_kb})"
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

                # Create persona agent with memory, dataset, and KB configuration
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

                self.logger.info(f"Created persona agent for interview: {persona.name}")

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

        # Check if we have at least one agent (interview allows single persona)
        if len(persona_agents) == 0:
            error_msg = f"Failed to create any persona agents. Failed: {', '.join(failed_personas)}"
            raise InterviewManagerError(error_msg)

        if failed_personas:
            self.logger.warning(
                f"Some persona agents failed to initialize: {', '.join(failed_personas)}"
            )

        self.logger.info(
            f"Successfully created {len(persona_agents)} persona agents for interview"
        )
        return persona_agents

    def _create_agent_with_integrations(
        self,
        persona: Any,
        system_prompt: str,
        enable_memory: bool,
        session_id: Any,
        memory_mode: str,
        enable_dataset: bool,
        enable_kb: bool,
    ) -> Any:
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
                    from src.services.knowledge_base.kb_tools import create_kb_retrieval_tool
                    from src.config import config

                    kb_tool = create_kb_retrieval_tool(
                        knowledge_base_id=kb.knowledge_base_id,
                        metadata_filters=kb_binding.metadata_filters,
                        region=config.AWS_REGION,
                    )
                    additional_tools.append(kb_tool)
                    enhanced_prompt = self.agent_service._enhance_prompt_with_kb_info(
                        enhanced_prompt, kb.name, kb.description, kb_binding.metadata_filters
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

    def _generate_interview_system_prompt(self, persona: Persona) -> str:
        """
        Generate interview-specific system prompt for a persona.

        Args:
            persona: Persona object

        Returns:
            str: Generated system prompt
        """
        base_prompt = self.agent_service.generate_persona_system_prompt(persona)

        interview_instructions = f"""

# インタビューでの振る舞い
- あなたはユーザーとの1対1のインタビューに参加しています
- ユーザーの質問に対して、あなたの価値観、経験、考え方に基づいて誠実に答えてください
- 具体的な例や体験談を交えて、説得力のある回答を心がけてください
- 他のペルソナの発言も参考にしながら、自分独自の視点を提供してください
- 自然で親しみやすい会話を心がけ、ユーザーとの対話を楽しんでください
- 回答は簡潔で分かりやすく、2-3文程度で答えてください

# ツール使用について
- あなたにはデータ参照用のツールが提供されている場合があります
- 購買履歴、過去の経験、具体的な商品について質問された場合は、**必ずツールを使用してデータを確認してから回答してください**
- ツールを使用する際は、まず認証設定（CREATE SECRET）を実行し、その後データ取得クエリを実行してください
- ツールでエラーが発生した場合は、認証設定を再実行してから再試行してください

# 重要な注意事項
- あなたは{persona.name}として一貫した人格を維持してください
- ユーザーの質問に対して建設的で有益な回答を提供してください
- 不適切な質問には丁寧に回答を控える旨を伝えてください
- データセットが利用可能な場合、具体的な情報は必ずツールで確認してから回答してください
"""

        return base_prompt + interview_instructions

    def _validate_session_data(self, session: InterviewSession) -> None:
        """
        Validate interview session data for persistence.

        Args:
            session: Interview session to validate

        Raises:
            InterviewManagerError: If session data is invalid
        """
        if not session.messages:
            raise InterviewManagerError("セッションにメッセージがありません")

        # Validate message order by timestamp
        for i in range(1, len(session.messages)):
            if session.messages[i].timestamp < session.messages[i - 1].timestamp:
                self.logger.warning(
                    f"Message order inconsistency detected in session {session.id}"
                )
                # Sort messages by timestamp to fix order
                session.messages.sort(key=lambda msg: msg.timestamp)
                break

        # Validate that all messages have timestamps
        for i, message in enumerate(session.messages):
            if not message.timestamp:
                raise InterviewManagerError(f"Message {i} missing timestamp")

        # Validate participants
        if not session.participants:
            raise InterviewManagerError("セッションに参加者がいません")

        self.logger.debug(f"Session validation passed for {session.id}")

    def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """
        Get detailed status information about an interview session.

        Args:
            session_id: ID of the interview session

        Returns:
            Dictionary with session status information

        Raises:
            InterviewManagerError: If session not found
        """
        if session_id not in self._active_sessions:
            raise InterviewManagerError(f"Interview session not found: {session_id}")

        session = self._active_sessions[session_id]

        # Check if agents are still active
        agents_active = (
            session_id in self._session_agents
            and len(self._session_agents[session_id]) > 0
        )

        return {
            "session_id": session.id,
            "participants": session.participants,
            "message_count": len(session.messages),
            "created_at": session.created_at.isoformat(),
            "is_saved": session.is_saved,
            "agents_active": agents_active,
            "has_user_messages": any(
                msg.message_type == "user_message" for msg in session.messages
            ),
            "has_persona_responses": any(
                msg.message_type == "statement" for msg in session.messages
            ),
            "last_activity": session.messages[-1].timestamp.isoformat()
            if session.messages
            else None,
            "enable_memory": session.enable_memory,
            "memory_mode": session.memory_mode,
            "enable_dataset": session.enable_dataset,
        }

    def cleanup_inactive_sessions(self, max_age_hours: int = 24) -> int:
        """
        Clean up inactive interview sessions that are older than specified age.

        Args:
            max_age_hours: Maximum age in hours for keeping sessions

        Returns:
            Number of sessions cleaned up
        """
        current_time = datetime.now()
        sessions_to_remove = []

        for session_id, session in self._active_sessions.items():
            age_hours = (current_time - session.created_at).total_seconds() / 3600

            if age_hours > max_age_hours and not session.is_saved:
                sessions_to_remove.append(session_id)
                self.logger.info(
                    f"Marking session for cleanup: {session_id} (age: {age_hours:.1f}h)"
                )

        # Clean up sessions and their agents
        cleaned_count = 0
        for session_id in sessions_to_remove:
            try:
                self.end_interview_session(session_id)
                cleaned_count += 1
            except Exception as e:
                self.logger.error(f"Error cleaning up session {session_id}: {e}")

        if cleaned_count > 0:
            self.logger.info(f"Cleaned up {cleaned_count} inactive sessions")

        return cleaned_count

    def get_active_sessions_count(self) -> int:
        """
        Get the number of currently active interview sessions.

        Returns:
            Number of active sessions
        """
        return len(self._active_sessions)

    # Enhanced validation methods

    def _validate_session_creation_input(
        self, personas: List[Persona], user_id: str
    ) -> None:
        """
        Validate input for session creation.

        Args:
            personas: List of personas to validate
            user_id: User ID to validate

        Raises:
            InterviewValidationError: If validation fails
        """
        if not personas:
            raise InterviewValidationError(
                "インタビューには最低1つのペルソナが必要です"
            )

        if len(personas) > 5:
            raise InterviewValidationError(
                "インタビューには最大5つのペルソナまで参加できます"
            )

        if not user_id or not isinstance(user_id, str) or not user_id.strip():
            raise InterviewValidationError("有効なユーザーIDが必要です")

        # Validate each persona
        for i, persona in enumerate(personas):
            if not persona or not hasattr(persona, "id") or not persona.id:
                raise InterviewValidationError(f"ペルソナ {i + 1} が無効です")

            if not hasattr(persona, "name") or not persona.name:
                raise InterviewValidationError(
                    f"ペルソナ {i + 1} の名前が設定されていません"
                )

    def _validate_session_exists(self, session_id: str) -> None:
        """
        Validate that a session exists.

        Args:
            session_id: Session ID to validate

        Raises:
            InterviewSessionNotFoundError: If session not found
        """
        if not session_id:
            raise InterviewSessionNotFoundError("セッションIDが指定されていません")

        if session_id not in self._active_sessions:
            raise InterviewSessionNotFoundError(
                f"インタビューセッションが見つかりません: {session_id}"
            )

    def _validate_message_input(self, message: str) -> None:
        """
        Validate message input.

        Args:
            message: Message to validate

        Raises:
            InterviewValidationError: If validation fails
        """
        if not message:
            raise InterviewValidationError("メッセージが指定されていません")

        if not message.strip():
            raise InterviewValidationError("メッセージが空です")

        if len(message.strip()) > 2000:
            raise InterviewValidationError("メッセージが長すぎます（最大2000文字）")

    def _validate_session_for_save(self, session: InterviewSession) -> None:
        """
        Validate session before saving.

        Args:
            session: Session to validate

        Raises:
            InterviewValidationError: If validation fails
        """
        if not session:
            raise InterviewValidationError("セッションが無効です")

        if not session.messages:
            raise InterviewValidationError("保存するメッセージがありません")

        if not session.participants:
            raise InterviewValidationError("セッションに参加者がいません")

        # Validate message integrity
        user_messages = [
            msg for msg in session.messages if msg.message_type == "user_message"
        ]
        persona_messages = [
            msg for msg in session.messages if msg.message_type == "statement"
        ]

        if not user_messages:
            raise InterviewValidationError("ユーザーメッセージが含まれていません")

        if not persona_messages:
            raise InterviewValidationError("ペルソナの応答が含まれていません")

    # Enhanced helper methods

    def _cleanup_session_agents(self, session_id: str) -> None:
        """
        Clean up persona agents for a specific session to free resources.

        Args:
            session_id: ID of the session to clean up agents for
        """
        if session_id not in self._session_agents:
            self.logger.debug(f"No agents found for session cleanup: {session_id}")
            return

        persona_agents = self._session_agents[session_id]
        cleanup_errors = []

        self.logger.info(
            f"Cleaning up {len(persona_agents)} persona agents for session: {session_id}"
        )

        for i, agent in enumerate(persona_agents):
            try:
                agent_name = (
                    agent.get_persona_name()
                    if hasattr(agent, "get_persona_name")
                    else f"Agent-{i}"
                )
                agent.dispose()
                self.logger.debug(f"Successfully disposed agent: {agent_name}")
            except Exception as e:
                error_msg = f"Error disposing agent {agent_name}: {e}"
                self.logger.warning(error_msg)
                cleanup_errors.append(error_msg)

        # Remove agents from tracking
        try:
            del self._session_agents[session_id]
            self.logger.debug(f"Removed session agents from tracking: {session_id}")
        except KeyError as e:
            self.logger.warning(f"Session agents not found during cleanup: {e}")

        if cleanup_errors:
            self.logger.warning(
                f"Agent cleanup completed with {len(cleanup_errors)} warnings for session: {session_id}"
            )
        else:
            self.logger.info(
                f"All persona agents cleaned up successfully for session: {session_id}"
            )

    def _cleanup_failed_session(
        self, session_id: Optional[str], persona_agents: List[PersonaAgent]
    ) -> None:
        """
        Clean up resources from a failed session creation.

        Args:
            session_id: Session ID (may be None)
            persona_agents: List of persona agents to clean up
        """
        try:
            # Clean up agents
            for agent in persona_agents:
                try:
                    agent.dispose()
                except Exception as e:
                    self.logger.warning(f"Error disposing agent during cleanup: {e}")

            # Clean up session if it was created
            if session_id and session_id in self._active_sessions:
                del self._active_sessions[session_id]

            if session_id and session_id in self._session_agents:
                del self._session_agents[session_id]

        except Exception as e:
            self.logger.error(f"Error during failed session cleanup: {e}")

    def _get_agent_response_with_retry(
        self,
        persona_agent: PersonaAgent,
        prompt: str,
        context: List[Message],
        max_retries: int = 2,
    ) -> str:
        """
        Get agent response with retry logic.

        Args:
            persona_agent: Persona agent to get response from
            prompt: Prompt to send
            context: Message context
            max_retries: Maximum number of retries

        Returns:
            str: Agent response

        Raises:
            AgentCommunicationError: If all retries fail
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                response = persona_agent.respond(prompt, context)
                if response and response.strip():
                    return response
                else:
                    raise AgentCommunicationError("Empty response from agent")

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    self.logger.warning(
                        f"Agent response attempt {attempt + 1} failed, retrying: {e}"
                    )
                    continue
                else:
                    break

        raise AgentCommunicationError(
            f"Agent failed after {max_retries + 1} attempts: {last_error}"
        )

    def _find_existing_discussion(self, session: InterviewSession) -> Optional[str]:
        """
        Find existing discussion for a session.

        Args:
            session: Session to find discussion for

        Returns:
            Discussion ID if found, None otherwise
        """
        try:
            discussions = self.database_service.get_discussions_by_mode("interview")
            for discussion in discussions:
                if discussion.participants == session.participants and len(
                    discussion.messages
                ) == len(session.messages):
                    return discussion.id
        except Exception as e:
            self.logger.warning(f"Error finding existing discussion: {e}")

        return None

    def _create_discussion_from_session(
        self, session: InterviewSession, session_name: str | None = None
    ) -> Discussion:
        """
        Create Discussion object from interview session.

        Args:
            session: Interview session
            session_name: Name for the session (used as topic)

        Returns:
            Discussion object
        """
        # Use provided session name or default
        topic = (
            session_name
            if session_name and session_name.strip()
            else "Interview Session"
        )

        # Create Discussion object from interview session with preserved timestamps
        discussion = Discussion.create_new(
            topic=topic,
            participants=session.participants,
            mode="interview",
            documents=session.documents,  # Include attached documents metadata
        )

        # Preserve original creation time from session
        discussion.created_at = session.created_at

        # Add all messages to discussion in order (messages already have timestamps)
        for message in session.messages:
            discussion = discussion.add_message(message)

        return discussion

    def _save_discussion_with_retry(
        self, discussion: Discussion, max_retries: int = 2
    ) -> str:
        """
        Save discussion with retry logic.

        Args:
            discussion: Discussion to save
            max_retries: Maximum number of retries

        Returns:
            Discussion ID

        Raises:
            DatabaseError: If all retries fail
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return self.database_service.save_discussion(discussion)

            except DatabaseError as e:
                last_error = e
                if attempt < max_retries:
                    self.logger.warning(
                        f"Database save attempt {attempt + 1} failed, retrying: {e}"
                    )
                    continue
                else:
                    break
            except Exception as e:
                # Don't retry for non-database errors
                raise DatabaseError(f"Unexpected error during save: {e}")

        raise DatabaseError(
            f"Database save failed after {max_retries + 1} attempts: {last_error}"
        )

    def _create_interview_prompt(
        self, persona_agent: PersonaAgent, user_message: str, context: List[Message]
    ) -> str:
        """
        Create interview-specific prompt for persona agent.

        Args:
            persona_agent: Target persona agent
            user_message: User's message/question
            context: Previous messages in the interview

        Returns:
            str: Generated prompt
        """
        persona_name = persona_agent.get_persona_name()

        # Get recent context (last 5 messages)
        recent_context = context[-5:] if len(context) > 5 else context

        if not recent_context:
            # First interaction
            prompt = (
                f"ユーザーからの質問: 「{user_message}」\n\n"
                f"この質問に対して、{persona_name}として答えてください。"
            )
        else:
            # Build context from recent messages
            context_text = "\n".join(
                [
                    f"{'ユーザー' if msg.persona_id == 'user' else msg.persona_name}: {msg.content}"
                    for msg in recent_context
                ]
            )

            prompt = (
                f"これまでの会話:\n{context_text}\n\n"
                f"ユーザーからの新しい質問: 「{user_message}」\n\n"
                f"この質問に対して、{persona_name}として答えてください。"
            )

        return prompt
