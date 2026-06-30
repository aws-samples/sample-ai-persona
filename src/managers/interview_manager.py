"""
Interview Manager for AI Persona System.
Handles interview session setup, execution, and persistence.
"""

import logging
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime


from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.message import Message
from ..models.interview_session import InterviewSession
from ..services.agent_service import (
    AgentService,
    PersonaAgent,
    AgentInitializationError,
    AgentCommunicationError,
)
from ..services.database_service import DatabaseService, DatabaseError


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


class InterviewManager:
    """
    Manager class for handling interview operations.
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
        from ..services.service_factory import service_factory

        self.logger = logging.getLogger(__name__)
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )

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

    def send_user_message_streaming(
        self,
        session_id: str,
        message: str,
        document_contents: Optional[List[Dict[str, Any]]] = None,
        document_metadata: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        Send a user message and stream persona responses token by token.

        Yields:
            tuple: (event_type, data)
                - ("message_start", dict): New persona bubble
                - ("message_delta", dict): Token chunk
                - ("message_end", Message): Complete message

        Raises:
            InterviewSessionNotFoundError: If session not found
            InterviewValidationError: If message validation fails
            InterviewAgentError: If all agents fail
        """
        self._validate_session_exists(session_id)
        self._validate_message_input(message)

        session = self._active_sessions[session_id]

        if session.is_saved:
            error_msg = f"保存済みのセッション {session_id} では新しいメッセージを送信できません"
            self.logger.error(error_msg)
            raise InterviewSessionError(error_msg)

        persona_agents = self._session_agents.get(session_id, [])

        if not persona_agents:
            error_msg = (
                f"セッション {session_id} にペルソナエージェントが見つかりません"
            )
            self.logger.error(error_msg)
            raise InterviewAgentError(error_msg)

        # Add user message to session
        session = session.add_user_message(message.strip())

        # ドキュメントメタデータ追加
        if document_metadata:
            for doc_meta in document_metadata:
                existing_docs = session.documents or []
                is_duplicate = any(
                    d.get("filename") == doc_meta.get("filename")
                    and d.get("file_size") == doc_meta.get("file_size")
                    for d in existing_docs
                )
                if not is_duplicate:
                    session = session.add_document(doc_meta)

        # ドキュメントコンテンツをエージェントに設定
        if document_contents:
            for persona_agent in persona_agents:
                persona_agent.set_document_contents(document_contents.copy())

        failed_agents: list[str] = []
        response_count = 0

        for persona_agent in persona_agents:
            agent_name = persona_agent.get_persona_name()
            try:
                prompt = self._create_interview_prompt(
                    persona_agent, message, session.messages
                )

                yield (
                    "message_start",
                    {
                        "persona_id": persona_agent.get_persona_id(),
                        "persona_name": agent_name,
                        "message_type": "statement",
                    },
                )

                full_text = ""
                for token in persona_agent.respond_streaming(prompt, session.messages):
                    full_text += token
                    yield (
                        "message_delta",
                        {
                            "persona_id": persona_agent.get_persona_id(),
                            "content": token,
                        },
                    )

                if not full_text.strip():
                    raise AgentCommunicationError("Empty response from agent")

                session = session.add_persona_response(
                    persona_agent.get_persona_id(),
                    agent_name,
                    full_text,
                )

                response_message = Message.create_new(
                    persona_id=persona_agent.get_persona_id(),
                    persona_name=agent_name,
                    content=full_text,
                    message_type="statement",
                )
                yield ("message_end", response_message)
                response_count += 1

            except AgentCommunicationError as e:
                self.logger.error(f"ペルソナ {agent_name} からの応答取得に失敗: {e}")
                failed_agents.append(agent_name)
                continue
            except Exception as e:
                self.logger.error(
                    f"ペルソナ {agent_name} の処理中に予期しないエラー: {e}"
                )
                failed_agents.append(agent_name)
                continue

        if response_count == 0 and failed_agents:
            raise InterviewAgentError(
                f"すべてのペルソナエージェントが応答に失敗しました: {', '.join(failed_agents)}"
            )

        # Update stored session
        self._active_sessions[session_id] = session

        self.logger.info(
            f"Streaming message processed, generated {response_count} responses"
        )

    def save_interview_session(
        self, session_id: str, session_name: str | None = None
    ) -> str:
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
                from ..prompts.discussion_interview_prompts import (
                    build_persona_system_prompt,
                )

                system_prompt = system_prompts.get(
                    persona.id,
                    build_persona_system_prompt(persona),
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
        """統合機能（KB、データセット）付きペルソナエージェントを作成。"""
        return self.agent_service.create_persona_agent_with_integrations(
            persona=persona,
            system_prompt=system_prompt,
            enable_memory=enable_memory,
            session_id=session_id,
            memory_mode=memory_mode,
            enable_kb=enable_kb,
            enable_dataset=enable_dataset,
        )

    def _generate_interview_system_prompt(self, persona: Persona) -> str:
        """インタビュー用システムプロンプトを生成する。"""
        from ..prompts.discussion_interview_prompts import build_interview_system_prompt

        return build_interview_system_prompt(persona)

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

    # =========================================================================
    # ファイル処理統合（Router層のContentBlock構築ロジックをManager層に統合）
    # =========================================================================

    def _validate_and_convert_files(
        self,
        raw_files: List[tuple],
    ) -> tuple:
        """ファイルのバリデーションとContentBlock変換。

        Args:
            raw_files: [(bytes, filename, mime_type), ...] のリスト

        Returns:
            (document_contents, document_metadata) のタプル

        Raises:
            InterviewValidationError: サイズ超過、未サポートMIMEタイプ
        """
        from ..config import config
        from .shared.document_loader import (
            build_content_block,
            is_supported_mime_type,
            is_image_type,
        )

        document_contents: List[Dict[str, Any]] = []
        document_metadata: List[Dict[str, Any]] = []

        for file_bytes, filename, mime_type in raw_files:
            if not file_bytes or not filename:
                continue

            # MIMEタイプバリデーション
            if not is_supported_mime_type(mime_type):
                raise InterviewValidationError(
                    f"ファイル '{filename}' のタイプ '{mime_type}' はサポートされていません"
                )

            # サイズバリデーション
            size_limit = (
                config.MAX_IMAGE_SIZE
                if is_image_type(mime_type)
                else config.MAX_FILE_SIZE
            )
            if len(file_bytes) > size_limit:
                limit_mb = size_limit // (1024 * 1024)
                raise InterviewValidationError(
                    f"ファイル '{filename}' が大きすぎます（最大{limit_mb}MB）"
                )

            # ContentBlock変換
            content_block = build_content_block(file_bytes, mime_type, filename)
            if content_block:
                document_contents.append(content_block)
                document_metadata.append(
                    {
                        "filename": filename,
                        "mime_type": mime_type,
                        "file_size": len(file_bytes),
                        "uploaded_at": datetime.now().isoformat(),
                    }
                )

        return document_contents, document_metadata

    def send_user_message_with_files(
        self,
        session_id: str,
        message: str,
        raw_files: Optional[List[tuple]] = None,
    ) -> List[Message]:
        """メッセージ送信（ファイル処理統合版、非ストリーミング）。

        Router層から生バイナリを受け取り、バリデーション + ContentBlock変換を
        Manager内部で実施する。

        Args:
            session_id: セッションID
            message: メッセージ本文
            raw_files: [(bytes, filename, mime_type), ...] のリスト

        Returns:
            List[Message]: ペルソナ応答リスト

        Raises:
            InterviewSessionNotFoundError: セッション未存在
            InterviewValidationError: バリデーション失敗
            InterviewAgentError: エージェント通信失敗
        """
        document_contents = None
        document_metadata = None

        if raw_files:
            document_contents, document_metadata = self._validate_and_convert_files(
                raw_files
            )

        return self.send_user_message(
            session_id=session_id,
            message=message,
            document_contents=document_contents or None,
            document_metadata=document_metadata or None,
        )

    def send_user_message_streaming_with_files(
        self,
        session_id: str,
        message: str,
        raw_files: Optional[List[tuple]] = None,
    ) -> Any:
        """メッセージ送信（ファイル処理統合版、ストリーミング）。

        Router層から生バイナリを受け取り、バリデーション + ContentBlock変換を
        Manager内部で実施する。

        Args:
            session_id: セッションID
            message: メッセージ本文
            raw_files: [(bytes, filename, mime_type), ...] のリスト

        Yields:
            tuple: (event_type, data)

        Raises:
            InterviewSessionNotFoundError: セッション未存在
            InterviewValidationError: バリデーション失敗
            InterviewAgentError: エージェント通信失敗
        """
        document_contents = None
        document_metadata = None

        if raw_files:
            document_contents, document_metadata = self._validate_and_convert_files(
                raw_files
            )

        yield from self.send_user_message_streaming(
            session_id=session_id,
            message=message,
            document_contents=document_contents or None,
            document_metadata=document_metadata or None,
        )
