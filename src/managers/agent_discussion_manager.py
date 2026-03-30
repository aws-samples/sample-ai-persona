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

    def _prepare_document_contents(
        self, documents_metadata: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        ドキュメントメタデータからStrands Agent SDK用のContentBlockリストを準備

        Args:
            documents_metadata: ドキュメントメタデータのリスト
                各辞書は以下のキーを含む:
                - file_path: ファイルパス（ローカルまたはs3://）
                - mime_type: MIMEタイプ
                - filename: ファイル名

        Returns:
            List[Dict[str, Any]]: Strands Agent SDK用のContentBlockリスト
        """
        content_list = []

        for doc in documents_metadata:
            try:
                file_path = doc.get("file_path", "")
                mime_type = doc.get("mime_type", "")
                filename = doc.get("filename", "document")

                # ファイルを読み込み（S3またはローカル）
                if file_path.startswith("s3://"):
                    # S3から読み込み
                    s3_service = service_factory.get_s3_service()
                    if not s3_service:
                        self.logger.warning(f"S3サービスが利用できません: {file_path}")
                        continue
                    file_bytes = s3_service.download_file(file_path)
                else:
                    # ローカルファイルから読み込み
                    from pathlib import Path

                    if not Path(file_path).exists():
                        self.logger.warning(f"ファイルが見つかりません: {file_path}")
                        continue
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()

                # MIMEタイプに応じてContentBlock形式を決定
                if mime_type.startswith("image/"):
                    # 画像の場合
                    image_format = mime_type.split("/")[-1]
                    # webpはサポートされているか確認
                    if image_format not in ["png", "jpeg", "gif", "webp"]:
                        image_format = "png"  # フォールバック
                    content_list.append(
                        {
                            "image": {
                                "format": image_format,
                                "source": {"bytes": file_bytes},
                            }
                        }
                    )
                    self.logger.info(f"画像を追加しました: {filename} ({image_format})")

                elif mime_type == "application/pdf":
                    # PDFの場合
                    # ファイル名から拡張子を除去し、英数字とアンダースコアのみに
                    import re

                    safe_name = re.sub(
                        r"[^a-zA-Z0-9_]", "_", filename.rsplit(".", 1)[0]
                    )[:100]
                    content_list.append(
                        {
                            "document": {
                                "name": safe_name,
                                "format": "pdf",
                                "source": {"bytes": file_bytes},
                            }
                        }
                    )
                    self.logger.info(f"PDFを追加しました: {filename}")

                elif mime_type in [
                    "text/plain",
                    "text/csv",
                    "text/html",
                    "text/markdown",
                ]:
                    # テキスト系ドキュメントの場合
                    format_map = {
                        "text/plain": "txt",
                        "text/csv": "csv",
                        "text/html": "html",
                        "text/markdown": "md",
                    }
                    doc_format = format_map.get(mime_type, "txt")
                    import re

                    safe_name = re.sub(
                        r"[^a-zA-Z0-9_]", "_", filename.rsplit(".", 1)[0]
                    )[:100]
                    content_list.append(
                        {
                            "document": {
                                "name": safe_name,
                                "format": doc_format,
                                "source": {"bytes": file_bytes},
                            }
                        }
                    )
                    self.logger.info(
                        f"テキストドキュメントを追加しました: {filename} ({doc_format})"
                    )

                else:
                    self.logger.warning(
                        f"サポートされていないMIMEタイプ: {mime_type} ({filename})"
                    )

            except Exception as e:
                self.logger.error(f"ドキュメント処理エラー ({filename}): {e}")
                continue

        return content_list

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
        documents_metadata = None
        document_context = None
        document_contents = []
        if document_ids:
            from src.managers.file_manager import FileManager
            from src.services.service_factory import service_factory

            file_manager = FileManager(
                db_service=self.database_service,
                s3_service=service_factory.get_s3_service(),
            )

            # Load document metadata
            documents_metadata = []
            document_descriptions = []
            for doc_id in document_ids:
                file_metadata = file_manager.get_file_metadata(doc_id)
                if file_metadata:
                    documents_metadata.append(
                        {
                            "id": file_metadata.file_id,
                            "filename": file_metadata.original_filename,
                            "file_path": file_metadata.file_path,
                            "file_size": file_metadata.file_size,
                            "mime_type": file_metadata.mime_type,
                            "uploaded_at": file_metadata.uploaded_at.isoformat()
                            if file_metadata.uploaded_at
                            else None,
                        }
                    )
                    document_descriptions.append(
                        f"- {file_metadata.original_filename} ({file_metadata.mime_type})"
                    )

            if documents_metadata:
                document_context = "\n".join(
                    [
                        "\n以下のドキュメントを参照しながら議論を進めてください:",
                        *document_descriptions,
                    ]
                )
                self.logger.info(
                    f"Loaded {len(documents_metadata)} documents for agent discussion"
                )

                # ドキュメントコンテンツをStrands Agent SDK用に準備
                document_contents = self._prepare_document_contents(documents_metadata)
                if document_contents:
                    self.logger.info(
                        f"Prepared {len(document_contents)} document contents for agents"
                    )
                    # 各ペルソナエージェントにドキュメントコンテンツを設定
                    for agent in persona_agents:
                        agent.set_document_contents(document_contents.copy())

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

            while facilitator.should_continue():
                facilitator.increment_round()
                current_round = facilitator.current_round

                self.logger.info(f"Starting round {current_round}/{facilitator.rounds}")

                # ラウンド開始時: 全エージェントの会話履歴をクリア（コンテキスト膨張防止）
                if current_round > 1:
                    for agent in persona_agents:
                        agent.clear_conversation_history()
                    facilitator.clear_conversation_history()
                    self.logger.info(
                        f"Cleared conversation history for round {current_round}"
                    )

                # Track who has spoken in this round and round messages
                spoken_in_round: list[str] = []
                round_messages = []

                # Each persona speaks once per round
                for _ in range(len(persona_agents)):
                    # Select next speaker
                    speaker = facilitator.select_next_speaker(
                        persona_agents, spoken_in_round
                    )

                    if speaker is None:
                        break

                    # Create prompt with round summaries + recent messages
                    prompt = facilitator.create_prompt_for_persona(
                        speaker, topic, all_messages[-3:],
                        round_summaries=round_summaries if round_summaries else None,
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
                        round_summary = facilitator.summarize_round(
                            current_round, round_messages, topic,
                            previous_summaries=round_summaries if round_summaries else None,
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
            self._cleanup_agents(persona_agents, facilitator)

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
        documents_metadata = None
        document_context = None
        document_contents = []
        if document_ids:
            from src.managers.file_manager import FileManager
            from src.services.service_factory import service_factory

            file_manager = FileManager(
                db_service=self.database_service,
                s3_service=service_factory.get_s3_service(),
            )

            # Load document metadata
            documents_metadata = []
            document_descriptions = []
            for doc_id in document_ids:
                file_metadata = file_manager.get_file_metadata(doc_id)
                if file_metadata:
                    documents_metadata.append(
                        {
                            "id": file_metadata.file_id,
                            "filename": file_metadata.original_filename,
                            "file_path": file_metadata.file_path,
                            "file_size": file_metadata.file_size,
                            "mime_type": file_metadata.mime_type,
                            "uploaded_at": file_metadata.uploaded_at.isoformat()
                            if file_metadata.uploaded_at
                            else None,
                        }
                    )
                    document_descriptions.append(
                        f"- {file_metadata.original_filename} ({file_metadata.mime_type})"
                    )

            if documents_metadata:
                document_context = "\n".join(
                    [
                        "\n以下のドキュメントを参照しながら議論を進めてください:",
                        *document_descriptions,
                    ]
                )
                self.logger.info(
                    f"Loaded {len(documents_metadata)} documents for streaming agent discussion"
                )

                # ドキュメントコンテンツをStrands Agent SDK用に準備
                document_contents = self._prepare_document_contents(documents_metadata)
                if document_contents:
                    self.logger.info(
                        f"Prepared {len(document_contents)} document contents for streaming agents"
                    )
                    # 各ペルソナエージェントにドキュメントコンテンツを設定
                    for agent in persona_agents:
                        agent.set_document_contents(document_contents.copy())

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

            while facilitator.should_continue():
                facilitator.increment_round()
                current_round = facilitator.current_round

                self.logger.info(f"Starting round {current_round}/{facilitator.rounds}")

                # ラウンド開始時: 全エージェントの会話履歴をクリア（コンテキスト膨張防止）
                if current_round > 1:
                    for agent in persona_agents:
                        agent.clear_conversation_history()
                    facilitator.clear_conversation_history()
                    self.logger.info(
                        f"Cleared conversation history for round {current_round}"
                    )

                # Track who has spoken in this round and round messages
                spoken_in_round: list[str] = []
                round_messages = []

                # Each persona speaks once per round
                for _ in range(len(persona_agents)):
                    # Select next speaker
                    speaker = facilitator.select_next_speaker(
                        persona_agents, spoken_in_round
                    )

                    if speaker is None:
                        break

                    # Create prompt with round summaries + recent messages
                    prompt = facilitator.create_prompt_for_persona(
                        speaker, topic, all_messages[-3:],
                        round_summaries=round_summaries if round_summaries else None,
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

                        # Yield the message immediately
                        yield ("message", message)

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

                # ラウンド終了後にファシリテータがラウンド全体を要約
                if round_messages:
                    try:
                        round_summary = facilitator.summarize_round(
                            current_round, round_messages, topic,
                            previous_summaries=round_summaries if round_summaries else None,
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

                        # Yield the summary message
                        yield ("message", summary_message)

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
            self._cleanup_agents(persona_agents, facilitator)

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

    def _cleanup_agents(
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
