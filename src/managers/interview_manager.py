"""
Interview Manager for AI Persona System.
Handles interview session setup, execution, and persistence.

v1.0.0 U6（``functional-design/rules.md`` BR3.1〜BR3.8、ADR-004）:

- セッションと発言はアプリの外（``InterviewSessionStore``）に置き、どの台でも会話を続けられる
- 送信・参照・保存・終了は持ち主だけ。他人・期限切れ・存在しないは区別せず
  ``INTERVIEW_SESSION_NOT_FOUND``（admin でも他人のセッションは使えない）
- 送信は (a) 利用者の発言と応答中の印、(b) ペルソナ 1 体ごとの応答を、それぞれ
  ``last_seq`` を条件にしたトランザクションで書く。応答中の別の送信と保存は
  ``CONCURRENT_UPDATE``。印は AI の失敗でもジェネレータのクローズでも外す
- 手元のエージェントは ``(session_id, owner_sub)`` で引き、ない・番号がずれていれば
  保存した発言から作り直す。8 時間使わなければ捨てる
- 添付は ID で持ち、紐付け前は ``attachable_file``、保存した議論に紐付いたあとは
  ``readable_file`` で読む。読めないファイルは外して名前を返す
- 保存は ``DiscussionRecords``（初回 ``save_new``、2 回目以降 ``append_messages``）
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
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
from ..models.errors import CodedError, ConcurrentUpdateError, ErrorCode
from ..models.interview_session import (
    MEMORY_MODES,
    SESSION_IDLE_SECONDS,
    USER_PERSONA_ID,
    InterviewMessage,
    InterviewReply,
    InterviewSaveResult,
    InterviewSession,
    InterviewView,
)
from ..models.message import Message
from ..models.persona import Persona
from ..models.uploaded_file import UploadedFile
from ..services.agent_service import (
    AgentCommunicationError,
    AgentConfigurationError,
    AgentInitializationError,
    AgentService,
    PersonaAgent,
)
from ..services.database_service import DatabaseService
from ..services.interview_session_store import (
    InterviewConflict,
    InterviewSessionStore,
    InterviewSessionStoreError,
)
from ..services.service_factory import service_factory
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
from .shared.structured_log import log_event

if TYPE_CHECKING:
    from ..services.dataset_analysis.service import DatasetAnalysisService
    from ..services.knowledge_base_store import KnowledgeBaseStore
    from ..services.s3_service import S3Service


class InterviewManagerError(CodedError):
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


#: 利用者の発言の表示名。
USER_NAME = "ユーザー"

#: セッション ID から保存先の議論の ID を決める名前空間（固定の値。変えると再保存で別の議論になる）
_SAVED_DISCUSSION_NAMESPACE = uuid.UUID("6f4b8a52-2d0e-4c1a-9b7e-3a5d1c8e0f21")


def _discussion_id_for(session_id: str) -> str:
    """The discussion a session is saved into, the same on every attempt.

    A save whose discussion was written but not recorded on the session can
    then find and reuse that discussion instead of making a second one.
    """
    return str(uuid.uuid5(_SAVED_DISCUSSION_NAMESPACE, session_id))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _LocalAgents:
    """This task's agents of one session (BR3.4)."""

    agents: List[PersonaAgent]
    known_seq: int
    messages: List[Message] = field(default_factory=list)
    last_used: datetime = field(default_factory=_utc_now)


def _discussion_parts() -> DiscussionParts:
    return discussion_parts_from(service_factory)


def _access_parts() -> AccessParts:
    return access_parts_from(service_factory)


def _history_for(persona_id: str, messages: Sequence[Message]) -> List[dict[str, Any]]:
    """The conversation one persona's agent is rebuilt with (BR3.4 (3)).

    The user's messages and the persona's own replies, in order. Consecutive
    messages of the same side are merged (the model needs alternating turns);
    a trailing unanswered user message is left to the next prompt's context.
    """
    turns: List[dict[str, Any]] = []
    for message in messages:
        if message.persona_id == USER_PERSONA_ID:
            role = "user"
        elif message.persona_id == persona_id:
            role = "assistant"
        else:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].append({"text": message.content})
        else:
            turns.append({"role": role, "content": [{"text": message.content}]})
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    return turns


def _target_of(persona: Persona) -> AccessTarget:
    return AccessTarget(
        resource_type=ResourceType.PERSONA,
        resource_id=persona.id,
        owner_sub=persona.owner_sub,
        shared_group_ids=persona.shared_group_ids,
        display_name=persona.name,
    )


def _document_ref(uploaded: UploadedFile) -> DocumentRef:
    return DocumentRef(
        id=uploaded.id,
        filename=uploaded.original_filename,
        mime_type=uploaded.mime_type or "text/plain",
        file_size=uploaded.file_size,
        uploaded_at=uploaded.uploaded_at.isoformat(),
    )


class InterviewManager:
    """
    Manager class for handling interview operations.
    """

    #: インタビュー参加ペルソナ数の上限。
    MAX_PERSONAS = 5
    #: 1メッセージあたりの最大文字数。
    MAX_MESSAGE_LENGTH = 2000

    def __init__(
        self,
        agent_service: AgentService | None = None,
        database_service: Optional[DatabaseService] = None,
        dataset_analysis_service: Optional["DatasetAnalysisService"] = None,
        knowledge_base_store: Optional["KnowledgeBaseStore"] = None,
        binding_store: Any = None,
        session_store: Optional[InterviewSessionStore] = None,
        discussion_records: Optional[DiscussionRecords] = None,
        participants: Optional[ParticipantReader] = None,
        file_attachments: Optional[FileAttachments] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        s3_service: Optional["S3Service"] = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        """
        Initialize interview manager.

        Args:
            agent_service: Agent service (the singleton when omitted)
            database_service: Database service (the singleton when omitted)
            dataset_analysis_service: Dataset tools (the singleton when omitted)
            knowledge_base_store: Registered knowledge bases for the KB tool
                (the singleton when omitted)
            binding_store: ``PersonaBindings`` for the KB / dataset tools (U5)
            session_store: Sessions and messages outside the app (U6)
            discussion_records: Saves the interview as a discussion (U6)
            participants: Checks the participants on every send and save (U6)
            file_attachments: Reads the attachments by ID (U5)
            resource_lookup: Usage scopes of organization assets (U6)
            s3_service: Reads attachments kept on S3 (the singleton when omitted)
            clock: Aware UTC now (tests replace it)
        """
        self.logger = logging.getLogger(__name__)
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self.dataset_analysis_service = (
            dataset_analysis_service or service_factory.get_dataset_analysis_service()
        )
        self._resource_lookup = resource_lookup
        self._agent_integration = PersonaAgentIntegration(
            database_service=self.database_service,
            knowledge_base_store=knowledge_base_store
            or service_factory.get_knowledge_base_store(),
            agent_service=self.agent_service,
            dataset_analysis_service=self.dataset_analysis_service,
            binding_store=binding_store or service_factory.get_persona_binding_store(),
            resource_lookup=resource_lookup,
        )
        self._store = session_store or service_factory.get_interview_session_store()
        self._records = discussion_records
        self._reader = participants
        self._attachments = file_attachments
        self._s3 = s3_service
        self._clock = clock

        # This task's agents, keyed by (session_id, owner_sub) (BR3.4)
        self._local: Dict[tuple[str, str], _LocalAgents] = {}
        self._local_lock = threading.Lock()

        self.logger.info("Interview Manager initialized")

    # --- 部品（遅延取得） ------------------------------------------------------------

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

    @property
    def _file_attachments(self) -> FileAttachments:
        if self._attachments is None:
            self._attachments = _access_parts().files
        return self._attachments

    def _lookup_for_assets(self) -> None:
        if self._resource_lookup is None:
            self._resource_lookup = _access_parts().lookup
            self._agent_integration.bind_lookup(self._resource_lookup)

    def _now(self) -> datetime:
        return self._clock()

    # =========================================================================
    # 開始（BR3.1）
    # =========================================================================

    def start_interview_session(
        self,
        principal: Principal,
        personas: List[Persona],
        enable_memory: bool = False,
        memory_mode: str = "full",
        enable_dataset: bool = False,
        enable_kb: bool = False,
        persona_models: Optional[Dict[str, str]] = None,
    ) -> InterviewSession:
        """
        Start a new interview session owned by ``principal``.

        Args:
            principal: The user who starts (and alone may use) the session
            personas: Participants read with the decision
                (``PersonaManager.get_personas_for_use``)
            enable_memory: Whether to use long-term memory
            memory_mode: ``full`` / ``retrieve_only`` / ``disabled``; personas the
                user does not own only retrieve (BR6.1)
            enable_dataset: Whether to enable dataset tools
            enable_kb: Whether to enable knowledge base tools
            persona_models: persona_id -> model_id のマップ（省略時は既定モデル）

        Returns:
            InterviewSession: The stored session

        Raises:
            AccessDeniedError: ``ai:execute`` がない
            InterviewValidationError: If input validation fails
            InterviewAgentError: If agent creation fails
            InterviewSessionError: If the session could not be stored
        """
        AccessPolicy.require(principal, Permission.AI_EXECUTE)
        self._validate_session_creation_input(personas)
        if memory_mode not in MEMORY_MODES:
            raise InterviewValidationError(
                f"invalid memory_mode {memory_mode!r}, expected one of "
                f"{list(MEMORY_MODES)}",
                code=ErrorCode.INTERVIEW_MEMORY_MODE_INVALID,
                context={"field": "memory_mode"},
            )
        self._validate_model_selection(persona_models)
        self._evict_idle()

        now = self._now()
        session = InterviewSession.create_new(
            owner_sub=principal.sub,
            persona_ids=[p.id for p in personas],
            persona_names={p.id: p.name for p in personas},
            memory_mode=memory_mode if enable_memory else "disabled",
            now=now,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=persona_models,
        )
        agents = self._create_interview_persona_agents(principal, session, personas)
        try:
            self._store.create(session)
        except InterviewSessionStoreError as e:
            self._dispose(agents)
            self.logger.error("Storing the interview session failed", exc_info=True)
            raise InterviewSessionError(
                f"interview session could not be stored ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e
        self._keep_local(principal, session.id, _LocalAgents(agents, 0, [], now))
        self.logger.info(
            "Interview session created: %s (%d personas)", session.id, len(personas)
        )
        return session

    def _validate_model_selection(
        self, model_ids: Optional[Mapping[str, Optional[str]]]
    ) -> None:
        """選択されたmodel_idが利用可能か検証する。

        未対応のmodel_idはVALIDATION、追加ペルソナベースモデルだが
        ENABLE_ADDITIONAL_PERSONA_MODELS無効時はCONFIG。
        """
        validate_model_selection(
            model_ids,
            InterviewValidationError,
            ErrorCode.INTERVIEW_MODEL_UNSUPPORTED,
            ErrorCode.INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED,
            unsupported_message=(
                lambda persona_id, model_id: (
                    f"unsupported model_id {model_id!r} for persona {persona_id!r}"
                )
            ),
        )

    def _validate_document_size(
        self, session: InterviewSession, documents: Sequence[DocumentRef]
    ) -> None:
        """選択モデルのmax_request_bytes（Gemma4等）に対しドキュメント合計サイズを検証する。

        インタビューはターンを跨いでドキュメントを蓄積するため、セッションに既に
        添付済みのドキュメントも合計に含める。
        """
        total_size = sum(d.file_size for d in list(session.documents) + list(documents))
        validate_document_size_for_models(
            total_size,
            resolve_effective_persona_models(
                session.persona_ids, session.persona_models
            ),
            InterviewValidationError,
            ErrorCode.INTERVIEW_MODEL_INPUT_TOO_LARGE,
        )

    # =========================================================================
    # 持ち主と期限（BR3.2、BR3.5）
    # =========================================================================

    def _owned_session(self, principal: Principal, session_id: str) -> InterviewSession:
        """The session, if ``principal`` owns it and it has not expired.

        Raises:
            InterviewSessionNotFoundError: Someone else's, expired or missing
                (not told apart).
            InterviewSessionError: The store could not be read.
        """
        if not session_id:
            raise InterviewSessionNotFoundError(
                "session_id is blank", code=ErrorCode.INTERVIEW_SESSION_NOT_FOUND
            )
        try:
            session = self._store.get(session_id)
        except InterviewSessionStoreError as e:
            raise InterviewSessionError(
                f"interview session unreadable ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e
        # admin でも他人のセッションは使えない（実行中の会話は持ち主だけのもの、BR3.2）。
        # 存在しない・期限切れは「持ち主なし」として同じ照合に通し、他人のものと同じ
        # 応答と同じ 1 行の authz.denied にする（NFR1.56、NFR8.22）
        alive = session if session and not session.is_expired(self._now()) else None
        try:
            AccessPolicy.ensure_owner(
                principal,
                alive.owner_sub if alive else None,
                ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
            )
        except CodedError as e:
            raise InterviewSessionNotFoundError(
                "interview session not found",
                code=ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
            ) from e
        if alive is None:  # 持ち主なしは上で断っている。型のための確認
            raise InterviewSessionNotFoundError(
                "interview session not found",
                code=ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
            )
        return alive

    def _fresh_session(self, session: InterviewSession) -> InterviewSession:
        """``session`` read again (strongly consistent), ownership already checked."""
        try:
            fresh = self._store.get(session.id)
        except InterviewSessionStoreError as e:
            raise InterviewSessionError(
                f"interview session unreadable ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e
        if fresh is None:
            raise InterviewSessionNotFoundError(
                "interview session not found",
                code=ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
            )
        return fresh

    def _messages(self, session: InterviewSession) -> List[InterviewMessage]:
        if session.last_seq == 0:
            return []
        try:
            return self._store.query_messages(session.id, up_to=session.last_seq)
        except InterviewSessionStoreError as e:
            raise InterviewSessionError(
                f"interview messages unreadable ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e

    def _conflict(self, session_id: str, phase: str) -> ConcurrentUpdateError:
        log_event(
            "interview.message_conflict",
            interview_session_id=session_id,
            phase=phase,
        )
        return ConcurrentUpdateError(
            f"a reply is in progress ({phase})", code=ErrorCode.CONCURRENT_UPDATE
        )

    # =========================================================================
    # 送信（BR3.3、BR3.6、BR7.2）
    # =========================================================================

    def send_user_message(
        self,
        principal: Principal,
        session_id: str,
        message: str,
        document_ids: Optional[Sequence[str]] = None,
    ) -> InterviewReply:
        """
        Send the owner's message and get every persona's reply.

        Args:
            principal: The session's owner
            session_id: ID of the interview session
            message: User message content
            document_ids: Files uploaded for this send (``FileManager``)

        Returns:
            InterviewReply: The replies and the names of files left out

        Raises:
            InterviewSessionNotFoundError: Not the owner's, expired or missing
            InterviewValidationError: Message or attachment size validation
            CodedError: ``PERSONA_NOT_FOUND`` (a participant is unreadable),
                ``FILE_NOT_FOUND`` (a new attachment), ``CONCURRENT_UPDATE``
            InterviewAgentError: Every persona failed to reply
        """
        responses: List[Message] = []
        excluded: List[str] = []
        for kind, data in self._send(
            principal, session_id, message, document_ids, streaming=False
        ):
            if kind == "files_excluded":
                excluded = list(data)
            elif kind == "message_end":
                responses.append(data)
        return InterviewReply(responses=responses, excluded_files=excluded)

    def send_user_message_streaming(
        self,
        principal: Principal,
        session_id: str,
        message: str,
        document_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[tuple[str, Any]]:
        """
        Send the owner's message and stream persona replies token by token.

        Yields:
            tuple: (event_type, data)
                - ("files_excluded", list[str]): Attachments left out
                - ("message_start", dict): New persona bubble
                - ("message_delta", dict): Token chunk
                - ("message_end", Message): Complete (and saved) message

        Raises:
            The same as :meth:`send_user_message`.
        """
        yield from self._send(
            principal, session_id, message, document_ids, streaming=True
        )

    def _send(
        self,
        principal: Principal,
        session_id: str,
        message: str,
        document_ids: Optional[Sequence[str]],
        *,
        streaming: bool,
    ) -> Iterator[tuple[str, Any]]:
        self._validate_message_input(message)
        self._evict_idle()
        session = self._owned_session(principal, session_id)
        now = self._now()
        if session.reply_in_progress(now):
            raise self._conflict(session.id, "a")
        self._participant_reader.ensure_all_readable(principal, session.persona_ids)

        new_documents, new_contents = self._read_new_attachments(
            principal, session, document_ids or []
        )
        local, excluded = self._local_agents(principal, session)
        if excluded:
            yield ("files_excluded", excluded)

        token = str(uuid.uuid4())
        user_message = InterviewMessage.create_new(
            session_id=session.id,
            seq=session.last_seq + 1,
            speaker="user",
            persona_id=USER_PERSONA_ID,
            persona_name_at_time=USER_NAME,
            content=message.strip(),
            now=now,
            document_ids=tuple(d.id for d in new_documents),
        )
        with_documents = replace(
            session,
            document_ids=list(session.document_ids) + [d.id for d in new_documents],
            documents=list(session.documents) + new_documents,
        )
        try:
            session = self._store.append_user_message(
                with_documents, user_message, token=token, now=now
            )
        except InterviewConflict as e:
            raise self._conflict(session.id, "a") from e
        except InterviewSessionStoreError as e:
            raise InterviewSessionError(
                f"user message could not be written ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e

        local.messages.append(user_message.to_message())
        local.known_seq = session.last_seq
        if new_contents:
            for agent in local.agents:
                agent.set_document_contents(list(new_contents))

        held = True
        replied = 0
        failed: List[str] = []
        seq = session.last_seq
        try:
            for index, agent in enumerate(local.agents):
                is_last = index == len(local.agents) - 1
                name = agent.get_persona_name()
                # これまでの会話は 1 体ごとに作り直す。今回の送信で先に答えたペルソナの
                # 応答も入る（今の会話の進め方と同じ、BR3.4）
                prompt = self._create_interview_prompt(
                    agent, message.strip(), list(local.messages)
                )
                try:
                    text = yield from self._reply(agent, prompt, streaming)
                except AgentCommunicationError as e:
                    self.logger.error(
                        f"agent {name} failed to respond: {type(e).__name__}"
                    )
                    failed.append(name)
                    if not is_last:
                        self._renew(session.id, token)
                    continue
                reply = InterviewMessage.create_new(
                    session_id=session.id,
                    seq=seq + 1,
                    speaker="persona",
                    persona_id=agent.get_persona_id(),
                    persona_name_at_time=name,
                    content=text,
                    now=self._now(),
                )
                try:
                    self._store.append_reply(
                        session.id,
                        reply,
                        expected_seq=seq,
                        token=token,
                        now=self._now(),
                        last=is_last,
                    )
                except InterviewConflict as e:
                    held = False
                    self._discard_local(principal, session.id)
                    raise self._conflict(session.id, "b") from e
                except InterviewSessionStoreError as e:
                    raise InterviewSessionError(
                        f"reply could not be written ({type(e).__name__})",
                        code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
                    ) from e
                if is_last:
                    held = False
                seq += 1
                replied += 1
                local.known_seq = seq
                local.messages.append(reply.to_message())
                local.last_used = self._now()
                yield ("message_end", reply.to_message())
        finally:
            if held:
                self._release(session.id, token)

        if replied == 0 and failed:
            raise InterviewAgentError(
                f"all persona agents failed to respond ({len(failed)})",
                code=ErrorCode.INTERVIEW_AGENT_UNAVAILABLE,
            )
        if failed:
            self.logger.warning(f"some persona agents failed: {', '.join(failed)}")

    def _reply(
        self, agent: PersonaAgent, prompt: str, streaming: bool
    ) -> Generator[tuple[str, Any], None, str]:
        """One persona's reply text (generator: streaming events go through)."""
        if not streaming:
            text = agent.respond(prompt, None)
        else:
            persona_id = agent.get_persona_id()
            yield (
                "message_start",
                {
                    "persona_id": persona_id,
                    "persona_name": agent.get_persona_name(),
                    "message_type": "statement",
                },
            )
            text = ""
            for token in agent.respond_streaming(prompt, None):
                text += token
                yield ("message_delta", {"persona_id": persona_id, "content": token})
        if not text or not text.strip():
            raise AgentCommunicationError("Empty response from agent")
        return text

    def _renew(self, session_id: str, token: str) -> None:
        try:
            self._store.renew_reply(session_id, token, self._now())
        except InterviewSessionStoreError:
            self.logger.warning("Renewing the reply mark failed", exc_info=True)

    def _release(self, session_id: str, token: str) -> None:
        try:
            self._store.release_reply(session_id, token)
        except InterviewSessionStoreError:
            # 外せなくても 5 分で無効になる（BR3.3）
            self.logger.warning("Releasing the reply mark failed", exc_info=True)

    # --- 添付（BR3.6） ---------------------------------------------------------------

    def _read_new_attachments(
        self,
        principal: Principal,
        session: InterviewSession,
        document_ids: Sequence[str],
    ) -> tuple[List[DocumentRef], List[Dict[str, Any]]]:
        known = set(session.document_ids)
        uploads: List[UploadedFile] = []
        for file_id in dict.fromkeys(document_ids):
            if file_id in known:
                continue
            uploads.append(self._file_attachments.attachable_file(principal, file_id))
        documents = [_document_ref(u) for u in uploads]
        if documents:
            self._validate_document_size(session, documents)
        return documents, self._contents(uploads)

    def _readable_attachment(
        self, principal: Principal, file_id: str
    ) -> Optional[UploadedFile]:
        """Before the save the user's own upload; after it, the discussion's file."""
        try:
            return self._file_attachments.attachable_file(principal, file_id)
        except CodedError as e:
            if e.code is not ErrorCode.FILE_NOT_FOUND:
                raise
        try:
            return self._file_attachments.readable_file(principal, file_id)
        except CodedError as e:
            if e.code is not ErrorCode.FILE_NOT_FOUND:
                raise
        return None

    def _contents(self, uploads: Sequence[UploadedFile]) -> List[Dict[str, Any]]:
        if not uploads:
            return []
        from .shared.document_loader import prepare_document_contents

        metadata = [
            {
                "file_path": u.file_path,
                "mime_type": u.mime_type or "text/plain",
                "filename": u.original_filename,
            }
            for u in uploads
        ]
        s3 = self._s3 or service_factory.get_optional_s3_service()
        return prepare_document_contents(metadata, s3)

    # --- 手元のエージェント（BR3.4、BR3.5） ------------------------------------------

    def _keep_local(
        self, principal: Principal, session_id: str, local: _LocalAgents
    ) -> None:
        with self._local_lock:
            old = self._local.pop((session_id, principal.sub), None)
            self._local[(session_id, principal.sub)] = local
        if old is not None and old is not local:
            self._dispose(old.agents)

    def _discard_local(self, principal: Principal, session_id: str) -> None:
        with self._local_lock:
            old = self._local.pop((session_id, principal.sub), None)
        if old is not None:
            self._dispose(old.agents)

    def _evict_idle(self) -> None:
        """Drop the agents not used for eight hours (BR3.5)."""
        limit = self._now() - timedelta(seconds=SESSION_IDLE_SECONDS)
        with self._local_lock:
            idle = [k for k, v in self._local.items() if v.last_used <= limit]
            dropped = [self._local.pop(k) for k in idle]
        for local in dropped:
            self._dispose(local.agents)

    def _local_agents(
        self, principal: Principal, session: InterviewSession
    ) -> tuple[_LocalAgents, List[str]]:
        """This task's agents, rebuilt from the stored messages if needed.

        Returns:
            ``(agents, excluded_file_names)``.
        """
        with self._local_lock:
            local = self._local.get((session.id, principal.sub))
        if local is not None and local.known_seq == session.last_seq:
            local.last_used = self._now()
            return local, []
        if local is not None:
            # 同じ台で送信が重なったとき、先の送信が使っているエージェントを捨てない。
            # 作り直す前にセッションを読み直し、応答中なら衝突として断る
            session = self._fresh_session(session)
            if session.reply_in_progress(self._now()):
                raise self._conflict(session.id, "a")
            if local.known_seq == session.last_seq:
                local.last_used = self._now()
                return local, []
        reason = "missing" if local is None else "seq_mismatch"

        stored = self._messages(session)
        if [m.seq for m in stored] != list(range(1, session.last_seq + 1)):
            log_event(
                "interview.agents_rebuild_failed",
                interview_session_id=session.id,
                reason="gap",
            )
            raise InterviewSessionError(
                "interview messages have a gap",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            )
        messages = [m.to_message() for m in stored]
        personas = self._participant_reader.read_for_use(principal, session.persona_ids)

        uploads: List[UploadedFile] = []
        excluded: List[str] = []
        for document in session.documents:
            uploaded = self._readable_attachment(principal, document.id)
            if uploaded is None:
                excluded.append(document.filename)
            else:
                uploads.append(uploaded)
        contents = self._contents(uploads)

        agents = self._create_interview_persona_agents(
            principal, session, personas, history=messages
        )
        if contents:
            for agent in agents:
                agent.set_document_contents(list(contents))
        rebuilt = _LocalAgents(agents, session.last_seq, messages, self._now())
        self._keep_local(principal, session.id, rebuilt)
        log_event(
            "interview.agents_rebuilt",
            interview_session_id=session.id,
            reason=reason,
            persona_count=len(agents),
            message_count=len(messages),
        )
        return rebuilt, excluded

    def _create_interview_persona_agents(
        self,
        principal: Principal,
        session: InterviewSession,
        personas: List[Persona],
        history: Optional[Sequence[Message]] = None,
    ) -> List[PersonaAgent]:
        """
        One agent per persona for ``principal`` (allows a single persona).

        Args:
            principal: The session's owner (memory: BR6.1)
            session: Memory, dataset, KB and model settings
            personas: The participants' bodies
            history: Stored messages to rebuild the conversation with (BR3.4)

        Returns:
            List[PersonaAgent]: Created persona agents

        Raises:
            InterviewAgentError: If agent creation fails
        """
        if not personas:
            raise InterviewManagerError(
                "persona list is empty", code=ErrorCode.INTERVIEW_PERSONAS_REQUIRED
            )
        self._lookup_for_assets()
        persona_agents: List[PersonaAgent] = []
        failed_personas: List[str] = []
        for persona in personas:
            try:
                persona_agents.append(
                    self._agent_integration.build_agent(
                        ExecutionContext.for_persona(principal, _target_of(persona)),
                        persona,
                        self._generate_interview_system_prompt(persona),
                        enable_memory=session.enable_memory,
                        session_id=session.id,
                        memory_mode=session.memory_mode,
                        enable_dataset=session.enable_dataset,
                        enable_kb=session.enable_kb,
                        model_id=(session.persona_models or {}).get(persona.id),
                        initial_messages=(
                            _history_for(persona.id, history)
                            if history is not None
                            else None
                        ),
                    )
                )
            except AgentConfigurationError as e:
                # 追加ペルソナベースモデル無効等の設定不足は CONFIG のまま即時通知する
                self.logger.error(
                    f"Configuration error creating agent for persona {persona.name}: "
                    f"{type(e).__name__}"
                )
                self._dispose(persona_agents)
                raise InterviewAgentError(
                    f"model configuration error ({type(e).__name__})",
                    code=ErrorCode.INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED,
                ) from e
            except (AgentInitializationError, CodedError) as e:
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

        if not persona_agents:
            raise InterviewAgentError(
                f"no persona agent could be created ({len(failed_personas)} failed)",
                code=ErrorCode.INTERVIEW_AGENT_SETUP_FAILED,
            )
        if failed_personas:
            self.logger.warning(
                f"Some persona agents failed to initialize: {', '.join(failed_personas)}"
            )
        return persona_agents

    def _generate_interview_system_prompt(self, persona: Persona) -> str:
        """インタビュー用システムプロンプトを生成する。"""
        from ..prompts.discussion_interview_prompts import build_interview_system_prompt

        return build_interview_system_prompt(persona)

    def _dispose(self, agents: Sequence[PersonaAgent]) -> None:
        dispose_agents(
            list(agents),
            self.logger,
            error_message=lambda _agent, e: (
                f"Error disposing interview agent: {type(e).__name__}"
            ),
        )

    # =========================================================================
    # 保存（BR3.7、BR3.8）
    # =========================================================================

    def save_interview_session(
        self, principal: Principal, session_id: str, session_name: str | None = None
    ) -> InterviewSaveResult:
        """
        Save the interview as a ``mode=interview`` discussion (or add to it).

        Args:
            principal: The session's owner
            session_id: ID of the interview session to save
            session_name: Name for the session (used as topic)

        Returns:
            InterviewSaveResult: The discussion ID and the files left out

        Raises:
            InterviewSessionNotFoundError: Not the owner's, expired or missing
            InterviewValidationError: Nothing to save yet
            CodedError: ``CONCURRENT_UPDATE``, ``PERSONA_NOT_FOUND``,
                ``DISCUSSION_NOT_FOUND``, ``DISCUSSION_TOO_LARGE``,
                ``DISCUSSION_OPERATION_FAILED``
        """
        self._evict_idle()
        session = self._owned_session(principal, session_id)
        now = self._now()
        if session.reply_in_progress(now):
            raise self._conflict(session.id, "save")
        messages = [m.to_message() for m in self._messages(session)]
        self._validate_session_for_save(messages)
        self._participant_reader.ensure_all_readable(principal, session.persona_ids)

        usable: List[DocumentRef] = []
        excluded: List[str] = []
        for document in session.documents:
            if self._readable_attachment(principal, document.id) is None:
                excluded.append(document.filename)
            else:
                usable.append(document)

        saved_id = session.saved_discussion_id
        created = False
        if saved_id is None:
            saved_id, created = self._save_first(
                principal, session, messages, usable, session_name, now
            )
        if not created:
            self._discussion_records.append_messages(
                principal, saved_id, messages, usable, skip_saved=True
            )
        discussion_id = saved_id

        try:
            self._store.extend(session.touched(now))
        except InterviewSessionStoreError:
            self.logger.warning("Extending the interview session failed", exc_info=True)
        self.logger.info(f"Interview session saved: {session.id} -> {discussion_id}")
        return InterviewSaveResult(discussion_id=discussion_id, excluded_files=excluded)

    def _save_first(
        self,
        principal: Principal,
        session: InterviewSession,
        messages: List[Message],
        usable: List[DocumentRef],
        session_name: Optional[str],
        now: datetime,
    ) -> tuple[str, bool]:
        """Run the first save of a session; only one request at a time (BR3.7).

        Returns:
            The discussion ID, and whether this call wrote the discussion
            (``False``: it already existed and the caller appends to it).

        Raises:
            CodedError: ``CONCURRENT_UPDATE`` while another request is saving.
        """
        token = uuid.uuid4().hex
        try:
            took = self._store.begin_save(session.id, token, now=now)
        except InterviewSessionStoreError as e:
            raise InterviewPersistenceError(
                f"the save could not start ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SAVE_FAILED,
            ) from e
        if not took:
            # 別の要求が初回の保存を終えていればそれに合流し、保存中なら断る
            fresh = self._store.get(session.id)
            if fresh is not None and fresh.saved_discussion_id:
                return fresh.saved_discussion_id, False
            raise self._conflict(session.id, "save")

        discussion_id = _discussion_id_for(session.id)
        try:
            if self._discussion_records.can_manage(principal, discussion_id):
                # 前回の保存で議論はできたが、セッションへの記録だけ失敗した。
                # 同じ議論を記録し直して書き足す（別の議論を作らない）
                self._record_saved(session.id, discussion_id, now)
                return discussion_id, False
            topic = (
                session_name.strip()
                if session_name and session_name.strip()
                else "Interview Session"
            )
            discussion = Discussion.create_new(
                topic=topic,
                participants=list(session.persona_ids),
                mode="interview",
                documents=usable,
                agent_config={"persona_models": session.persona_models},
                participant_names=dict(session.persona_names),
                discussion_id=discussion_id,
                now=session.created_at,
            )
            self._discussion_records.save_new(principal, discussion, messages)
            self._record_saved(session.id, discussion_id, now)
            return discussion_id, True
        except BaseException:
            try:
                self._store.release_save(session.id, token)
            except InterviewSessionStoreError:
                # 外せなくても 5 分で解ける
                self.logger.warning("Releasing the save mark failed", exc_info=True)
            raise

    def _record_saved(self, session_id: str, discussion_id: str, now: datetime) -> None:
        try:
            self._store.mark_saved(session_id, discussion_id, now=now)
        except InterviewConflict as e:
            raise self._conflict(session_id, "save") from e
        except InterviewSessionStoreError as e:
            raise InterviewPersistenceError(
                f"saved discussion could not be recorded ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SAVE_FAILED,
            ) from e

    # =========================================================================
    # 参照・状態・終了
    # =========================================================================

    def get_interview_session(
        self, principal: Principal, session_id: str
    ) -> InterviewView:
        """
        The owner's session with its messages.

        Raises:
            InterviewSessionNotFoundError: Not the owner's, expired or missing
        """
        session = self._owned_session(principal, session_id)
        return InterviewView(
            session=session, messages=[m.to_message() for m in self._messages(session)]
        )

    def end_interview_session(self, principal: Principal, session_id: str) -> None:
        """
        End the owner's session; it can no longer be used.

        Raises:
            InterviewSessionNotFoundError: Not the owner's, expired or missing
            InterviewSessionError: If the store failed
        """
        session = self._owned_session(principal, session_id)
        try:
            self._store.end(session.id)
        except InterviewSessionStoreError as e:
            raise InterviewSessionError(
                f"interview session could not be ended ({type(e).__name__})",
                code=ErrorCode.INTERVIEW_SESSION_OPERATION_FAILED,
            ) from e
        self._discard_local(principal, session.id)
        self.logger.info(f"Interview session ended: {session.id}")

    def get_session_status(
        self, principal: Principal, session_id: str
    ) -> Dict[str, Any]:
        """
        Status of the owner's session.

        Raises:
            InterviewSessionNotFoundError: Not the owner's, expired or missing
        """
        session = self._owned_session(principal, session_id)
        view = InterviewView(
            session=session, messages=[m.to_message() for m in self._messages(session)]
        )
        with self._local_lock:
            local = self._local.get((session.id, principal.sub))
        return {
            "session_id": session.id,
            "participants": list(session.persona_ids),
            "message_count": session.last_seq,
            "created_at": session.created_at.isoformat(),
            "is_saved": view.is_saved,
            "agents_active": local is not None and bool(local.agents),
            "has_user_messages": any(
                m.message_type == "user_message" for m in view.messages
            ),
            "has_persona_responses": any(
                m.message_type == "statement" for m in view.messages
            ),
            "last_activity": session.last_active_at.isoformat(),
            "enable_memory": session.enable_memory,
            "memory_mode": session.memory_mode,
            "enable_dataset": session.enable_dataset,
        }

    def get_active_sessions_count(self) -> int:
        """The number of sessions whose agents this task holds."""
        self._evict_idle()
        with self._local_lock:
            return len(self._local)

    # =========================================================================
    # 検証と文面
    # =========================================================================

    def _validate_session_creation_input(self, personas: List[Persona]) -> None:
        """
        Validate input for session creation.

        Raises:
            InterviewValidationError: If validation fails
        """
        if not personas:
            raise InterviewValidationError(
                "persona list is empty",
                code=ErrorCode.INTERVIEW_PERSONAS_REQUIRED,
                context={"field": "persona_ids"},
            )
        if len(personas) > self.MAX_PERSONAS:
            raise InterviewValidationError(
                f"persona count {len(personas)} exceeds max {self.MAX_PERSONAS}",
                code=ErrorCode.INTERVIEW_TOO_MANY_PERSONAS,
                context={"max_personas": self.MAX_PERSONAS, "field": "persona_ids"},
            )
        for i, persona in enumerate(personas):
            if not persona or not getattr(persona, "id", None):
                raise InterviewValidationError(
                    f"persona at index {i} has no id",
                    code=ErrorCode.INTERVIEW_PERSONA_INVALID,
                )
            if not getattr(persona, "name", None):
                raise InterviewValidationError(
                    f"persona at index {i} has no name",
                    code=ErrorCode.INTERVIEW_PERSONA_INVALID,
                )

    def _validate_message_input(self, message: str) -> None:
        """
        Validate message input.

        Raises:
            InterviewValidationError: If validation fails
        """
        if not message or not message.strip():
            raise InterviewValidationError(
                "message is blank",
                code=ErrorCode.INTERVIEW_MESSAGE_REQUIRED,
            )
        if len(message.strip()) > self.MAX_MESSAGE_LENGTH:
            raise InterviewValidationError(
                f"message length {len(message.strip())} exceeds max "
                f"{self.MAX_MESSAGE_LENGTH}",
                code=ErrorCode.INTERVIEW_MESSAGE_TOO_LONG,
                context={"max_length": self.MAX_MESSAGE_LENGTH},
            )

    def _validate_session_for_save(self, messages: Sequence[Message]) -> None:
        """
        Validate the conversation before saving.

        Raises:
            InterviewValidationError: No user message or no persona reply yet
        """
        has_user = any(m.message_type == "user_message" for m in messages)
        has_reply = any(m.message_type == "statement" for m in messages)
        if not has_user or not has_reply:
            raise InterviewValidationError(
                "session has no user message or no persona response",
                code=ErrorCode.INTERVIEW_SAVE_PRECONDITION_NOT_MET,
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
        recent_context = context[-5:]
        if not recent_context:
            return (
                f"ユーザーからの質問: 「{user_message}」\n\n"
                f"この質問に対して、{persona_name}として答えてください。"
            )
        context_text = "\n".join(
            f"{'ユーザー' if msg.persona_id == USER_PERSONA_ID else msg.persona_name}: "
            f"{msg.content}"
            for msg in recent_context
        )
        return (
            f"これまでの会話:\n{context_text}\n\n"
            f"ユーザーからの新しい質問: 「{user_message}」\n\n"
            f"この質問に対して、{persona_name}として答えてください。"
        )
