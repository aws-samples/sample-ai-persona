"""
Agent Discussion Manager for AI Persona System.
Handles AI agent mode discussion setup, execution, and persistence.

v1.0.0 U6 (``functional-design/rules.md``):

- BR2.8: running needs ``ai:execute``; the participants were read with the
  decision before the run (``PersonaManager.get_personas_for_use``).
- BR6.1: each participant's agent is built with an ``ExecutionContext``; the
  runner writes long-term memory only for the personas they own.
- BR6.2: the discussion ID is decided at the start and is also the memory
  session ID.
- BR7.1: right before every persona statement the participants are checked
  again (one ``ResourceLookup.get_many``); an unreadable participant stops the
  run, nothing is saved, and the stream sends ``stopped`` (M12).
- The result is saved through ``DiscussionRecords`` (ADR-006).
"""

import json
import logging
import uuid
from typing import (
    Any,
    Dict,
    Generator,
    List,
    Mapping,
    Optional,
    TYPE_CHECKING,
)

from ..models.authz import (
    AccessTarget,
    ExecutionContext,
    Permission,
    Principal,
    ResourceType,
)
from ..models.discussion import Discussion, DocumentRef
from ..models.errors import CodedError, ErrorCode, ParticipantNotFoundError
from ..models.message import Message
from ..models.persona import Persona
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
from ..services.service_factory import service_factory
from .components import discussion_validation
from .components.access_policy import AccessPolicy
from .components.discussion_records import DiscussionRecords
from .components.file_attachments import FileAttachments
from .components.participant_reader import ParticipantReader
from .components.persona_agent_integration import PersonaAgentIntegration
from .components.resource_lookup import ResourceLookup
from .components.sharing_runtime import (
    AccessParts,
    DiscussionParts,
    access_parts_from,
    discussion_parts_from,
)
from .shared.agent_cleanup import dispose_agents
from .shared.model_validation import (
    resolve_effective_persona_models,
    validate_document_size_for_models,
    validate_model_selection,
)

if TYPE_CHECKING:
    from ..services.dataset_analysis.service import DatasetAnalysisService
    from ..services.persona_binding_store import PersonaBindingStore
    from ..services.dataset_store import DatasetStore
    from ..services.knowledge_base_store import KnowledgeBaseStore


class AgentDiscussionManagerError(CodedError):
    """Base exception for agent discussion manager related errors."""

    pass


class DiscussionFlowError(AgentDiscussionManagerError):
    """Discussion flow related errors."""

    pass


#: An event of the round loop: ``(kind, payload)``.
_Event = tuple[str, Any]


def _sse(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _access_parts() -> AccessParts:
    return access_parts_from(service_factory)


def _discussion_parts() -> DiscussionParts:
    return discussion_parts_from(service_factory)


class AgentDiscussionManager:
    """
    Manager class for handling AI agent mode discussion operations.
    Orchestrates agent creation, discussion execution, and persistence.
    """

    def __init__(
        self,
        agent_service: AgentService | None = None,
        dataset_store: Optional["DatasetStore"] = None,
        dataset_analysis_service: Optional["DatasetAnalysisService"] = None,
        knowledge_base_store: Optional["KnowledgeBaseStore"] = None,
        file_attachments: Optional[FileAttachments] = None,
        binding_store: Optional["PersonaBindingStore"] = None,
        discussion_records: Optional[DiscussionRecords] = None,
        participants: Optional[ParticipantReader] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        ai_service: Any = None,
    ):
        """
        Initialize agent discussion manager.

        Args:
            agent_service: Agent service (the singleton when omitted)
            dataset_store: Datasets for the dataset tools (the singleton when omitted)
            dataset_analysis_service: Dataset tools (the singleton when omitted)
            knowledge_base_store: Registered knowledge bases for the KB tool
                (the singleton when omitted)
            file_attachments: The per-task ``FileAttachments`` (U5)
            binding_store: ``PersonaBindings`` for KB / dataset tools (U5)
            discussion_records: The per-task ``DiscussionRecords`` (U6)
            participants: The per-task ``ParticipantReader`` (U6)
            resource_lookup: Usage scopes of organization assets (U6)
            ai_service: Extracts insights (the singleton when omitted)
        """
        self.logger = logging.getLogger(__name__)
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.dataset_store = dataset_store or service_factory.get_dataset_store()
        self.dataset_analysis_service = (
            dataset_analysis_service or service_factory.get_dataset_analysis_service()
        )
        self._agent_integration = PersonaAgentIntegration(
            dataset_store=self.dataset_store,
            knowledge_base_store=knowledge_base_store
            or service_factory.get_knowledge_base_store(),
            agent_service=self.agent_service,
            dataset_analysis_service=self.dataset_analysis_service,
            binding_store=binding_store or service_factory.get_persona_binding_store(),
            resource_lookup=resource_lookup,
        )
        self._resource_lookup = resource_lookup
        self._attachments = file_attachments
        self._records = discussion_records
        self._reader = participants
        self._ai_service = ai_service

    @property
    def _file_attachments(self) -> FileAttachments:
        if self._attachments is None:
            self._attachments = _access_parts().files
        return self._attachments

    @property
    def _discussion_records(self) -> DiscussionRecords:
        if self._records is None:
            self._records = _discussion_parts().records
        return self._records

    @property
    def _participant_reader(self) -> ParticipantReader:
        if self._reader is None:
            self._reader = _discussion_parts().participants
        return self._reader

    def _lookup_for_assets(self) -> None:
        if self._resource_lookup is None:
            self._resource_lookup = _access_parts().lookup
            self._agent_integration.bind_lookup(self._resource_lookup)

    # =========================================================================
    # エージェントの組み立て
    # =========================================================================

    def _create_persona_agents(
        self,
        personas: List[Persona],
        system_prompts: Dict[str, str],
        enable_memory: bool = False,
        session_id: Optional[str] = None,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
        persona_models: Optional[Dict[str, str]] = None,
        *,
        principal: Principal,
    ) -> List[PersonaAgent]:
        """
        Create one agent per persona for ``principal`` (BR6.1).

        Args:
            personas: List of Persona objects (read with the decision)
            system_prompts: Dictionary mapping persona_id to system_prompt
            enable_memory: Whether to use long-term memory
            session_id: Memory session (the discussion ID, BR6.2)
            memory_mode: The user's choice; personas the user does not own only
                retrieve (``ExecutionContext.write_memory``)
            enable_dataset: Whether to enable external dataset access
            enable_kb: Whether to enable knowledge base access
            persona_models: persona_id -> model_id のマップ（省略時は既定モデル）
            principal: The runner

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

        if enable_memory and not session_id:
            self.logger.warning(
                "Long-term memory requested but session_id is not provided. "
                "Agents will be created without memory."
            )
            enable_memory = False

        persona_agents: List[PersonaAgent] = []
        failed_personas: List[str] = []

        for persona in personas:
            try:
                system_prompt = system_prompts.get(
                    persona.id,
                    build_persona_system_prompt(persona),
                )
                persona_agent = self._agent_integration.build_agent(
                    ExecutionContext.for_persona(principal, _target_of(persona)),
                    persona,
                    system_prompt,
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
                self.logger.error(
                    f"Configuration error creating agent for persona {persona.name}: "
                    f"{type(e).__name__}"
                )
                self._cleanup_agents(persona_agents)
                raise AgentDiscussionManagerError(
                    f"model configuration error ({type(e).__name__})",
                    code=ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
                ) from e
            except AgentInitializationError as e:
                self.logger.error(
                    f"Failed to create agent for persona {persona.name}: "
                    f"{type(e).__name__}"
                )
                failed_personas.append(persona.name)
            except Exception as e:
                self.logger.error(
                    f"Unexpected error creating agent for persona {persona.name}: "
                    f"{type(e).__name__}"
                )
                failed_personas.append(persona.name)

        if len(persona_agents) < 2:
            self.logger.error(
                "Failed to create enough persona agents. Failed: %s",
                ", ".join(failed_personas),
            )
            self._cleanup_agents(persona_agents)
            raise AgentDiscussionManagerError(
                f"only {len(persona_agents)} persona agents created, minimum is 2",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

        if failed_personas:
            self.logger.warning(
                f"Some persona agents failed to initialize: {', '.join(failed_personas)}"
            )
        return persona_agents

    def _create_facilitator_agent(
        self,
        rounds: int,
        additional_instructions: str = "",
        facilitator_model: Optional[str] = None,
    ) -> FacilitatorAgent:
        """
        Create facilitator agent for discussion management.

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

        try:
            return self.agent_service.create_facilitator_agent(
                rounds, additional_instructions, model_id=facilitator_model
            )
        except AgentConfigurationError as e:
            self.logger.error("Facilitator model configuration error", exc_info=True)
            raise AgentDiscussionManagerError(
                f"facilitator model configuration error ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
            ) from e
        except Exception as e:
            self.logger.error("Failed to create facilitator agent", exc_info=True)
            raise AgentDiscussionManagerError(
                f"facilitator agent creation failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            ) from e

    def _validate_model_selection(
        self, model_ids: Optional[Mapping[str, Optional[str]]]
    ) -> None:
        """選択されたmodel_idが利用可能か検証する。"""
        validate_model_selection(
            model_ids,
            AgentDiscussionManagerError,
            ErrorCode.DISCUSSION_MODEL_UNSUPPORTED,
            ErrorCode.DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED,
        )

    # =========================================================================
    # ラウンドの実行（BR7.1）
    # =========================================================================

    def _rounds(
        self,
        principal: Principal,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
        *,
        streaming: bool,
        document_ids: Optional[List[str]] = None,
        persona_models: Optional[Dict[str, str]] = None,
    ) -> Generator[_Event, None, None]:
        """Run the rounds; always disposes the agents.

        Yields:
            ``("documents", list[DocumentRef] | None)`` first, then
            ``("message_start", dict)`` / ``("message_delta", dict)`` /
            ``("message_end", Message)``, and finally ``("complete",
            list[Message])`` or ``("stopped", None)``.
        """
        try:
            self._validate_discussion_input(
                personas, topic, persona_agents, facilitator
            )
            documents, document_context, document_contents = (
                self._load_and_attach_documents(
                    document_ids, persona_agents, persona_models, principal=principal
                )
            )
            yield ("documents", documents)
            discussion_topic = (
                f"{topic}\n{document_context}" if document_context else topic
            )
            facilitator.start_discussion(discussion_topic, persona_agents)
            participant_ids = [p.id for p in personas]

            all_messages: List[Message] = []
            round_summaries: List[str] = []
            total_rounds = facilitator.rounds
            for current_round in range(1, total_rounds + 1):
                if current_round > 1:
                    for agent in persona_agents:
                        agent.clear_conversation_history()
                        if document_contents:
                            agent.set_document_contents(document_contents.copy())
                    facilitator.clear_conversation_history()

                spoken_in_round: List[str] = []
                round_messages: List[Message] = []
                for _ in range(len(persona_agents)):
                    speaker = self._select_next_speaker(persona_agents, spoken_in_round)
                    if speaker is None:
                        break
                    # BR7.1: 発言の直前に参加者全員を確かめ直す
                    if not self._participant_reader.can_read_all(
                        principal, participant_ids
                    ):
                        self.logger.info(
                            "Agent discussion stopped: a participant is gone"
                        )
                        yield ("stopped", None)
                        return
                    prompt = self._build_persona_prompt(
                        speaker,
                        topic,
                        all_messages[-10:],
                        current_round,
                        total_rounds,
                        round_summaries=round_summaries or None,
                        latest_facilitator_message=round_summaries[-1]
                        if round_summaries
                        else None,
                    )
                    try:
                        message = yield from self._statement(
                            speaker, prompt, current_round, streaming
                        )
                    except AgentCommunicationError as e:
                        self.logger.error(
                            f"Failed to get response from {speaker.get_persona_name()}: "
                            f"{type(e).__name__}"
                        )
                        spoken_in_round.append(speaker.get_persona_id())
                        continue
                    all_messages.append(message)
                    round_messages.append(message)
                    spoken_in_round.append(speaker.get_persona_id())

                if round_messages:
                    summary = yield from self._summary(
                        facilitator,
                        current_round,
                        round_messages,
                        topic,
                        total_rounds,
                        round_summaries,
                        streaming,
                    )
                    if summary is not None:
                        round_summaries.append(summary.content)
                        all_messages.append(summary)

            yield ("complete", all_messages)

        except AgentDiscussionManagerError:
            raise
        except CodedError:
            raise
        except Exception as e:
            self.logger.error("Error during agent discussion", exc_info=True)
            raise AgentDiscussionManagerError(
                f"agent discussion failed ({type(e).__name__})",
                code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            ) from e
        finally:
            self._cleanup_agents(persona_agents, facilitator)

    def _statement(
        self,
        speaker: PersonaAgent,
        prompt: str,
        current_round: int,
        streaming: bool,
    ) -> Generator[_Event, None, Message]:
        persona_id = speaker.get_persona_id()
        if streaming:
            yield (
                "message_start",
                {
                    "persona_id": persona_id,
                    "persona_name": speaker.get_persona_name(),
                    "message_type": "statement",
                    "round_number": current_round,
                },
            )
            text = ""
            for token in speaker.respond_streaming(prompt, None):
                text += token
                yield ("message_delta", {"persona_id": persona_id, "content": token})
        else:
            text = speaker.respond(prompt, None)
        message = Message.create_new(
            persona_id=persona_id,
            persona_name=speaker.get_persona_name(),
            content=text,
            message_type="statement",
            round_number=current_round,
        )
        if streaming:
            yield ("message_end", message)
        return message

    def _summary(
        self,
        facilitator: FacilitatorAgent,
        current_round: int,
        round_messages: List[Message],
        topic: str,
        total_rounds: int,
        round_summaries: List[str],
        streaming: bool,
    ) -> Generator[_Event, None, Optional[Message]]:
        prompt = self._build_summary_prompt(
            current_round,
            round_messages,
            topic,
            total_rounds,
            previous_summaries=round_summaries or None,
        )
        try:
            if streaming:
                yield (
                    "message_start",
                    {
                        "persona_id": "facilitator",
                        "persona_name": "ファシリテータ",
                        "message_type": "summary",
                        "round_number": current_round,
                    },
                )
                text = ""
                for token in facilitator.invoke_streaming(prompt):
                    text += token
                    yield (
                        "message_delta",
                        {"persona_id": "facilitator", "content": token},
                    )
            else:
                text = facilitator.invoke(prompt)
        except AgentCommunicationError as e:
            self.logger.warning(
                f"Failed to summarize round {current_round}: {type(e).__name__}"
            )
            return None
        message = Message.create_new(
            persona_id="facilitator",
            persona_name="ファシリテータ",
            content=text,
            message_type="summary",
            round_number=current_round,
        )
        if streaming:
            yield ("message_end", message)
        return message

    def _load_and_attach_documents(
        self,
        document_ids: Optional[List[str]],
        persona_agents: List[PersonaAgent],
        persona_models: Optional[Dict[str, str]] = None,
        *,
        principal: Optional[Principal] = None,
    ) -> tuple:
        """ドキュメントを読み込みエージェントに添付する。

        U5（R-02）: 読むのは、保存時の紐付けと同じ条件（``FileAttachments.
        attachable_file``: 本人の・期限内の・まだどこにも紐付いていない資料）を満たす
        ものだけ。1 つでも満たさなければ、議論を走らせる前に ``FILE_NOT_FOUND`` で止める。

        Returns:
            ``(documents, document_context, document_contents)``; documents are
            :class:`DocumentRef` (no S3 key, BR5.2).
        """
        documents: Optional[List[DocumentRef]] = None
        document_context = None
        document_contents: List[Dict[str, Any]] = []

        if document_ids:
            from .shared.document_loader import (
                build_document_context,
                load_documents_metadata,
                prepare_document_contents,
            )

            def read_file(file_id: str) -> Any:
                try:
                    if principal is None:
                        raise CodedError("no user", code=ErrorCode.FILE_NOT_FOUND)
                    return self._file_attachments.attachable_file(principal, file_id)
                except CodedError as e:
                    if e.code is not ErrorCode.FILE_NOT_FOUND:
                        raise AgentDiscussionManagerError(
                            f"document read failed ({type(e).__name__})",
                            code=ErrorCode.DISCUSSION_OPERATION_FAILED,
                        ) from e
                    raise AgentDiscussionManagerError(
                        "document not attachable", code=ErrorCode.FILE_NOT_FOUND
                    ) from e

            metadata = load_documents_metadata(document_ids, read_file)

            if metadata:
                # 環境既定モデル（Gemma4 等の Mantle 系）にも添付の大きさの検証を当てる
                effective_persona_models = resolve_effective_persona_models(
                    (agent.get_persona_id() for agent in persona_agents),
                    persona_models,
                )
                total_size = sum(doc.get("file_size", 0) for doc in metadata)
                validate_document_size_for_models(
                    total_size,
                    effective_persona_models,
                    AgentDiscussionManagerError,
                    ErrorCode.DISCUSSION_MODEL_INPUT_TOO_LARGE,
                )

                document_context = build_document_context(metadata)
                documents = [DocumentRef.from_dict(doc) for doc in metadata]
                s3_service = service_factory.get_optional_s3_service()
                document_contents = prepare_document_contents(metadata, s3_service)
                for agent in persona_agents:
                    if document_contents:
                        agent.set_document_contents(document_contents.copy())

        return documents, document_context, document_contents

    def _validate_discussion_input(
        self,
        personas: List[Persona],
        topic: str,
        persona_agents: List[PersonaAgent],
        facilitator: FacilitatorAgent,
    ) -> None:
        """
        Validate input parameters for discussion start.

        Raises:
            AgentDiscussionManagerError: If validation fails
        """
        discussion_validation.validate_personas_and_topic(
            personas, topic, error_cls=AgentDiscussionManagerError
        )
        if not persona_agents or len(persona_agents) < 2:
            raise AgentDiscussionManagerError(
                f"{len(persona_agents or [])} persona agents given, minimum is 2",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )
        if not facilitator:
            raise AgentDiscussionManagerError(
                "facilitator agent is not set",
                code=ErrorCode.DISCUSSION_AGENT_SETUP_FAILED,
            )

    def _cleanup_agents(
        self,
        persona_agents: List[PersonaAgent],
        facilitator: Optional[FacilitatorAgent] = None,
    ) -> None:
        """エージェントリソースを解放してメモリリークを防ぐ。"""
        try:
            dispose_agents(
                persona_agents,
                self.logger,
                error_message=lambda _agent, e: (
                    f"ペルソナエージェントの解放中にエラー: {type(e).__name__}"
                ),
            )
            if facilitator is not None:
                dispose_agents(
                    [facilitator],
                    self.logger,
                    error_message=lambda _agent, e: (
                        f"ファシリテータエージェントの解放中にエラー: {type(e).__name__}"
                    ),
                )
        except Exception as e:
            self.logger.error(
                f"エージェントリソース解放中に予期しないエラー: {type(e).__name__}"
            )

    # =========================================================================
    # ワークフロー制御メソッド（agent_service.py FacilitatorAgent から移動）
    # =========================================================================

    def _select_next_speaker(
        self,
        persona_agents: List[PersonaAgent],
        spoken_in_round: List[str],
    ) -> Optional[PersonaAgent]:
        """次の発言者をランダムに選択する（全員発言済みなら None）。"""
        import random

        available_agents = [
            agent
            for agent in persona_agents
            if agent.get_persona_id() not in spoken_in_round
        ]
        if not available_agents:
            return None
        return random.choice(available_agents)

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
    # フルフローAPI（Router から呼ぶ入口）
    # =========================================================================

    def _prepare_run(
        self,
        principal: Principal,
        personas: List[Persona],
        rounds: int,
        additional_instructions: str,
        enable_memory: bool,
        memory_mode: str,
        enable_dataset: bool,
        enable_kb: bool,
        persona_models: Optional[Dict[str, str]],
        facilitator_model: Optional[str],
    ) -> tuple[str, List[PersonaAgent], FacilitatorAgent]:
        AccessPolicy.require(
            principal, Permission.AI_EXECUTE, resource_type=ResourceType.DISCUSSION
        )
        self._validate_memory_mode(memory_mode)
        self._lookup_for_assets()
        discussion_id = str(uuid.uuid4())  # BR6.2: 記憶のセッション ID = 議論の ID
        persona_agents = self._create_persona_agents(
            personas,
            {},
            enable_memory=enable_memory,
            session_id=discussion_id,
            memory_mode=memory_mode,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=persona_models,
            principal=principal,
        )
        try:
            facilitator = self._create_facilitator_agent(
                rounds, additional_instructions, facilitator_model=facilitator_model
            )
        except Exception:
            # facilitator 作成失敗時は、作成済みの persona_agents をここで解放する
            self._cleanup_agents(persona_agents)
            raise
        return discussion_id, persona_agents, facilitator

    def run_agent_discussion_streaming(
        self,
        principal: Principal,
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

        Yields:
            str: SSE data行（``message_start`` / ``message_delta`` /
            ``message_end``、最後に ``complete`` か ``stopped``）

        Raises:
            AccessDeniedError: ``ai:execute`` がない
            AgentDiscussionManagerError: バリデーション失敗、エージェント/DB例外
        """
        discussion_id, persona_agents, facilitator = self._prepare_run(
            principal,
            personas,
            rounds,
            additional_instructions,
            enable_memory,
            memory_mode,
            enable_dataset,
            enable_kb,
            persona_models,
            facilitator_model,
        )
        documents: Optional[List[DocumentRef]] = None
        for kind, data in self._rounds(
            principal,
            personas,
            topic,
            persona_agents,
            facilitator,
            streaming=True,
            document_ids=document_ids,
            persona_models=persona_models,
        ):
            if kind == "documents":
                documents = data
            elif kind in ("message_start", "message_delta"):
                yield _sse({"type": kind, **data})
            elif kind == "message_end":
                yield _sse(
                    {
                        "type": "message_end",
                        "persona_id": data.persona_id,
                        "persona_name": data.persona_name,
                        "content": data.content,
                        "message_type": data.message_type,
                    }
                )
            elif kind == "stopped":
                yield _sse({"type": "stopped"})
                return
            elif kind == "complete":
                discussion = self._save(
                    principal,
                    discussion_id,
                    personas,
                    topic,
                    data,
                    documents,
                    categories,
                    facilitator,
                    enable_memory=enable_memory,
                    persona_models=persona_models,
                    facilitator_model=facilitator_model,
                )
                yield _sse(
                    {
                        "type": "complete",
                        "discussion_id": discussion.id,
                        "message_count": len(data),
                        "insight_count": len(discussion.insights),
                    }
                )

    def run_agent_discussion_full(
        self,
        principal: Principal,
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

        Raises:
            AccessDeniedError: ``ai:execute`` がない
            CodedError: ``PERSONA_NOT_FOUND``（実行中に参加者が読めなくなった。保存しない）
            AgentDiscussionManagerError: バリデーション失敗、エージェント/DB例外
        """
        discussion_id, persona_agents, facilitator = self._prepare_run(
            principal,
            personas,
            rounds,
            additional_instructions,
            enable_memory,
            memory_mode,
            enable_dataset,
            enable_kb,
            persona_models,
            facilitator_model,
        )
        documents: Optional[List[DocumentRef]] = None
        messages: List[Message] = []
        for kind, data in self._rounds(
            principal,
            personas,
            topic,
            persona_agents,
            facilitator,
            streaming=False,
            document_ids=document_ids,
            persona_models=persona_models,
        ):
            if kind == "documents":
                documents = data
            elif kind == "stopped":
                raise ParticipantNotFoundError("a participant became unreadable")
            elif kind == "complete":
                messages = data
        discussion_validation.validate_results(
            messages,
            personas,
            error_cls=DiscussionFlowError,
            require_min_content=False,
            count_statements_only=True,
        )
        return self._save(
            principal,
            discussion_id,
            personas,
            topic,
            messages,
            documents,
            categories,
            facilitator,
            enable_memory=enable_memory,
            persona_models=persona_models,
            facilitator_model=facilitator_model,
        )

    def _save(
        self,
        principal: Principal,
        discussion_id: str,
        personas: List[Persona],
        topic: str,
        messages: List[Message],
        documents: Optional[List[DocumentRef]],
        categories: Optional[List[InsightCategory]],
        facilitator: FacilitatorAgent,
        *,
        enable_memory: bool,
        persona_models: Optional[Dict[str, str]],
        facilitator_model: Optional[str],
    ) -> Discussion:
        discussion = Discussion.create_new(
            topic=topic.strip(),
            participants=[p.id for p in personas],
            mode="agent",
            agent_config={
                "rounds": facilitator.rounds,
                "additional_instructions": facilitator.additional_instructions,
                "enable_memory": enable_memory,
                "persona_models": persona_models,
                "facilitator_model": facilitator_model,
            },
            documents=documents,
            participant_names={p.id: p.name for p in personas},
            discussion_id=discussion_id,
        )
        discussion = self._attach_insights(discussion, messages, categories)
        discussion_validation.validate_for_save(
            discussion,
            error_cls=AgentDiscussionManagerError,
            code=ErrorCode.DISCUSSION_OPERATION_FAILED,
            require_mode="agent",
            messages=messages,
        )
        return self._discussion_records.save_new(principal, discussion, messages)

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
        messages: List[Message],
        categories: Optional[List[InsightCategory]],
    ) -> Discussion:
        """インサイト生成・カテゴリー保存の共通処理（100 件まで、BR2.6）。"""
        from .shared.insight_utils import attach_insights_to_discussion

        return attach_insights_to_discussion(
            discussion=discussion,
            messages=messages,
            categories=categories,
            ai_service=self._ai_service or service_factory.get_ai_service(),
            logger=self.logger,
        )


def _target_of(persona: Persona) -> AccessTarget:
    """The persona's owner and shares, from its body read with the decision."""
    return AccessTarget(
        resource_type=ResourceType.PERSONA,
        resource_id=persona.id,
        owner_sub=persona.owner_sub,
        shared_group_ids=persona.shared_group_ids,
        display_name=persona.name,
    )
