"""
REST API router (JSON responses)

Provides REST APIs for personas, discussions, interviews and insight generation.
Also usable by external AI agents through AgentCore Gateway.
"""

import logging
from dataclasses import replace
from typing import Annotated, Any, Literal, Optional, List

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.managers.persona_manager import PersonaManager
from src.managers.discussion_manager import DiscussionManager
from src.managers.agent_discussion_manager import AgentDiscussionManager
from src.managers.interview_manager import InterviewManager
from src.managers.job_manager import JobManager
from src.managers.persona_generation_manager import PersonaGenerationManager
from src.models.authz import Principal
from src.models.persona_candidate import CandidateSaveResult
from src.models.errors import CodedError
from src.models.insight_category import InsightCategory
from web.error_messages import user_message_for, user_message_for_code

logger = logging.getLogger(__name__)

router = APIRouter()

# Singleton manager instances (shared at module level)
_persona_manager: PersonaManager | None = None
_discussion_manager: DiscussionManager | None = None
_agent_discussion_manager: AgentDiscussionManager | None = None
_interview_manager: InterviewManager | None = None
_job_manager: JobManager | None = None
_persona_generation_manager: PersonaGenerationManager | None = None


def get_persona_manager() -> PersonaManager:
    """Get the PersonaManager singleton instance."""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_discussion_manager() -> DiscussionManager:
    """Get the DiscussionManager singleton instance."""
    global _discussion_manager
    if _discussion_manager is None:
        _discussion_manager = DiscussionManager()
    return _discussion_manager


def get_agent_discussion_manager() -> AgentDiscussionManager:
    global _agent_discussion_manager
    if _agent_discussion_manager is None:
        _agent_discussion_manager = AgentDiscussionManager()
    return _agent_discussion_manager


def get_interview_manager() -> InterviewManager:
    global _interview_manager
    if _interview_manager is None:
        _interview_manager = InterviewManager()
    return _interview_manager


def get_job_manager() -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager


def get_persona_generation_manager() -> PersonaGenerationManager:
    global _persona_generation_manager
    if _persona_generation_manager is None:
        _persona_generation_manager = PersonaGenerationManager()
    return _persona_generation_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request."""
    principal: Principal = request.state.principal
    return principal


# ---------------------------------------------------------------------------
# Response / Request models
# ---------------------------------------------------------------------------


class PersonaResponse(BaseModel):
    id: str
    name: str
    age: int
    occupation: str
    background: str
    values: List[str]
    pain_points: List[str]
    goals: List[str]
    gender: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    tags: List[str] = []


class InsightResponse(BaseModel):
    category: str
    description: str
    supporting_messages: List[str]
    confidence_score: float


class MessageResponse(BaseModel):
    persona_id: str
    persona_name: str
    content: str
    timestamp: str
    message_type: str


class DiscussionDetailResponse(BaseModel):
    id: str
    topic: str
    mode: str
    participants: List[str]
    messages: List[MessageResponse]
    insights: List[InsightResponse]
    created_at: str


class JobResponse(BaseModel):
    job_id: str
    status: str
    result: Any = None
    error: Optional[str] = None
    #: The stable code of a failed job (never its text)
    error_code: Optional[str] = None
    #: Persona generation only (additive): candidates not saved
    failed_count: Optional[int] = None
    failed_names: Optional[List[str]] = None


# --- MCP Request models ---


class GeneratePersonasRequest(BaseModel):
    """Persona generation request."""

    data_type: str = Field(
        description="データ種別: interview, market_report, review, purchase, other",
    )
    file_contents: List[Annotated[str, Field(max_length=100_000)]] = Field(
        default_factory=list,
        max_length=10,
        description="テキストデータのリスト（各要素がファイル内容に相当、最大10件・各10万文字）",
    )
    count: int = Field(default=3, ge=1, le=10, description="生成するペルソナ数")
    description: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="データの説明（data_type=other時に使用）",
    )
    custom_prompt: Optional[str] = Field(
        default=None, max_length=2000, description="カスタムプロンプト（最大2000文字）"
    )


class CategoryInput(BaseModel):
    """Insight category input."""

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)


class RunDiscussionRequest(BaseModel):
    """Discussion run request."""

    persona_ids: List[str] = Field(
        min_length=2, description="参加ペルソナIDリスト（2名以上）"
    )
    topic: str = Field(min_length=1, max_length=500, description="議論トピック")
    mode: Literal["classic", "agent"] = Field(
        default="classic",
        description="議論モード: classic または agent",
    )
    rounds: int = Field(
        default=5, ge=1, le=10, description="議論ラウンド数（agentモードのみ、1-10）"
    )
    categories: Optional[List[CategoryInput]] = Field(
        default=None,
        max_length=10,
        description="カスタムインサイトカテゴリ（最大10件）",
    )


class GenerateInsightsRequest(BaseModel):
    """Insight generation request."""

    categories: Optional[List[CategoryInput]] = Field(
        default=None,
        max_length=10,
        description="カスタムインサイトカテゴリ（最大10件）",
    )


class RunInterviewRequest(BaseModel):
    """Interview run request."""

    persona_ids: List[str] = Field(
        min_length=1, max_length=5, description="参加ペルソナIDリスト（1-5名）"
    )
    question: str = Field(min_length=1, max_length=5000, description="質問内容")


@router.get("/personas", response_model=List[PersonaResponse])
async def list_personas(request: Request, search: Optional[str] = None) -> Any:
    """Persona list API (only the caller's own and shared ones)."""
    try:
        persona_manager = get_persona_manager()
        personas = await anyio.to_thread.run_sync(
            persona_manager.list_accessible_personas, _principal(request), search
        )

        return [
            PersonaResponse(
                id=p.id,
                name=p.name,
                age=p.age,
                occupation=p.occupation,
                background=p.background,
                values=p.values,
                pain_points=p.pain_points,
                goals=p.goals,
                gender=p.gender,
                country=p.country,
                city=p.city,
                tags=p.tags,
            )
            for p in personas
        ]
    except CodedError:
        # Lookup failures (503 lookup_unavailable) and the like are left to the shared exception handler
        raise
    except Exception as e:
        logger.error(f"Failed to fetch persona list: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="ペルソナ一覧の取得に失敗しました"),
        )


@router.get("/personas/{persona_id}", response_model=PersonaResponse)
async def get_persona(request: Request, persona_id: str) -> Any:
    """Persona detail API (readable users only; 404 if unreadable)."""
    try:
        persona_manager = get_persona_manager()
        detail = await anyio.to_thread.run_sync(
            persona_manager.get_persona_detail, _principal(request), persona_id
        )
        persona = detail.persona

        return PersonaResponse(
            id=persona.id,
            name=persona.name,
            age=persona.age,
            occupation=persona.occupation,
            background=persona.background,
            values=persona.values,
            pain_points=persona.pain_points,
            goals=persona.goals,
            gender=persona.gender,
            country=persona.country,
            city=persona.city,
            tags=persona.tags,
        )
    except (HTTPException, CodedError):
        # Unreadable/nonexistent (the same 404) and 503 are turned into JSON by the shared exception handler
        raise
    except Exception as e:
        logger.error(f"Failed to fetch persona: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="ペルソナの取得に失敗しました"),
        )


@router.get("/discussions")
async def list_discussions(request: Request) -> Any:
    """Discussion list API (the caller's own and shared ones; admin sees all)."""
    try:
        discussions = await anyio.to_thread.run_sync(
            get_discussion_manager().list_accessible_discussions, _principal(request)
        )

        return [
            {
                "id": d.id,
                "topic": d.topic,
                "mode": d.mode,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in discussions
        ]
    except CodedError:
        # Lookup failures (503) and the like are left to the shared exception handler
        raise
    except Exception as e:
        logger.error(f"Failed to fetch discussion list: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="議論一覧の取得に失敗しました"),
        )


@router.get("/health")
async def health() -> Any:
    """Health check API."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# MCP Tool Endpoints
# ---------------------------------------------------------------------------


def _persona_to_response(p: Any) -> PersonaResponse:
    return PersonaResponse(
        id=p.id,
        name=p.name,
        age=p.age,
        occupation=p.occupation,
        background=p.background,
        values=p.values,
        pain_points=p.pain_points,
        goals=p.goals,
    )


async def _resolve_personas(principal: Principal, persona_ids: List[str]) -> list:
    """Check the participants, then read.

    403 without ``ai:execute``, 404 if even one cannot be read (in that order; the shared
    exception handler turns it into ``{detail, error_code}``).
    """
    personas: list = await anyio.to_thread.run_sync(
        get_persona_manager().get_personas_for_use, principal, persona_ids
    )
    return personas


# --- generate_personas (async job) ---


def _run_generate_personas(
    principal: Principal,
    file_contents: list[tuple[bytes, str]],
    data_type: str,
    persona_count: int,
    data_description: str | None,
    custom_prompt: str | None,
) -> dict[str, Any]:
    """Persona generation run in a background thread.

    Like the screen, it just calls generate -> hold -> save -> clean up (always called) in that order.
    The ID returned is the saved persona's ID (not the candidate's ID).

    The result stored on the job is ``{"personas", "failed_count", "failed_names"}``.
    ``GET /api/jobs/{job_id}`` still returns ``result`` as a persona array and adds the count and
    names of the ones that could not be saved (the caller's own candidates) as fields (compatible).
    """
    gen_manager = get_persona_generation_manager()
    generated = gen_manager.generate(
        principal,
        file_contents=file_contents,
        data_type=data_type,
        persona_count=persona_count,
        data_description=data_description,
        custom_prompt=custom_prompt,
    )
    claim = gen_manager.claim_candidates(principal, [p.id for p in generated.personas])
    result: CandidateSaveResult | None = None
    try:
        result = get_persona_manager().save_generated_personas(principal, claim)
    finally:
        gen_manager.finish_candidates(claim, result)
    saved = dict(zip(result.saved_ids, result.saved_persona_ids))
    return {
        "personas": [
            _persona_to_response(replace(p, id=saved[p.id])).model_dump()
            for p in generated.personas
            if p.id in saved
        ],
        "failed_count": len(result.failed_ids),
        "failed_names": list(result.failed_names),
    }


@router.post(
    "/personas/generate",
    response_model=JobResponse,
    status_code=202,
    summary="AIペルソナを生成する",
    description="テキストデータからAIペルソナを生成します。処理は非同期で実行され、ジョブIDが返されます。GET /api/jobs/{job_id} でステータスと結果を確認してください。",
)
async def generate_personas(request: Request, req: GeneratePersonasRequest) -> Any:
    try:
        file_contents: list[tuple[bytes, str]] = [
            (text.encode("utf-8"), f"input_{i}.txt")
            for i, text in enumerate(req.file_contents)
        ]
        jm = get_job_manager()
        job_id = jm.submit(
            _principal(request),
            "persona_generation",
            _run_generate_personas,
            file_contents,
            req.data_type,
            req.count,
            req.description,
            req.custom_prompt,
        )
        return JobResponse(job_id=job_id, status="pending")
    except CodedError:
        # No permission (ACCESS_DENIED) and the like are left to the shared exception handler
        raise
    except Exception as e:
        logger.error(f"Failed to submit persona generation job: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="ペルソナ生成の開始に失敗しました"),
        )


# --- run_discussion (async job) ---


def _run_classic_discussion(
    principal: Principal,
    personas: list,
    topic: str,
    categories: list | None,
    rounds: int = 3,
) -> dict:
    """Run a classic discussion in the background (run and saved as the submitting user)."""
    dm = get_discussion_manager()
    cats = (
        [InsightCategory.from_dict(c.model_dump()) for c in categories]
        if categories
        else None
    )
    discussion = dm.run_classic_discussion(principal, personas, topic, categories=cats)
    return _discussion_detail(dm.get_discussion(principal, discussion.id))


def _run_agent_discussion(
    principal: Principal,
    personas: list,
    topic: str,
    categories: list | None,
    rounds: int = 5,
) -> dict:
    """Run an agent discussion in the background."""
    adm = get_agent_discussion_manager()
    cats = (
        [InsightCategory.from_dict(c.model_dump()) for c in categories]
        if categories
        else None
    )
    discussion = adm.run_agent_discussion_full(
        principal, personas, topic, rounds=rounds, categories=cats
    )
    return _discussion_detail(
        get_discussion_manager().get_discussion(principal, discussion.id)
    )


def _discussion_detail(discussion: Any) -> dict:
    """``DiscussionView`` -> the JSON of ``DiscussionDetailResponse``."""
    return DiscussionDetailResponse(
        id=discussion.id,
        topic=discussion.topic,
        mode=discussion.mode,
        participants=discussion.participant_ids,
        messages=[
            MessageResponse(
                persona_id=m.persona_id,
                persona_name=m.persona_name,
                content=m.content,
                timestamp=m.timestamp.isoformat(),
                message_type=m.message_type,
            )
            for m in discussion.messages
        ],
        insights=[
            InsightResponse(
                category=i.category,
                description=i.description,
                supporting_messages=i.supporting_messages,
                confidence_score=i.confidence_score,
            )
            for i in discussion.insights
        ],
        created_at=discussion.created_at.isoformat(),
    ).model_dump()


@router.post(
    "/discussions",
    response_model=JobResponse,
    status_code=202,
    summary="ペルソナ間の議論を実行する",
    description="指定されたペルソナ間で議論を実行します。処理は非同期で実行され、ジョブIDが返されます。GET /api/jobs/{job_id} でステータスと結果を確認してください。",
)
async def run_discussion(request: Request, req: RunDiscussionRequest) -> Any:
    try:
        principal = _principal(request)
        personas = await _resolve_personas(principal, req.persona_ids)
        jm = get_job_manager()
        runner = (
            _run_classic_discussion if req.mode == "classic" else _run_agent_discussion
        )
        job_id = jm.submit(
            principal,
            "discussion",
            runner,
            personas,
            req.topic,
            req.categories,
            req.rounds,
        )
        return JobResponse(job_id=job_id, status="pending")
    except (HTTPException, CodedError):
        raise
    except Exception as e:
        logger.error(f"Failed to submit discussion job: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="議論の開始に失敗しました"),
        )


# --- get_discussion ---


@router.get(
    "/discussions/{discussion_id}",
    response_model=DiscussionDetailResponse,
    summary="議論結果を取得する",
    description="指定されたIDの議論結果（メッセージ、インサイト含む）を取得します。",
)
async def get_discussion_detail(request: Request, discussion_id: str) -> Any:
    # Unreadable/nonexistent gives DISCUSSION_NOT_FOUND (the shared exception handler turns it into 404)
    try:
        view = await anyio.to_thread.run_sync(
            get_discussion_manager().get_discussion, _principal(request), discussion_id
        )
        return _discussion_detail(view)
    except CodedError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch discussion: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="議論の取得に失敗しました"),
        )


# --- generate_insights ---


@router.post(
    "/discussions/{discussion_id}/insights",
    response_model=JobResponse,
    status_code=202,
    summary="議論結果からインサイトを生成する",
    description="保存済みの議論結果からインサイトを生成します。処理は非同期で実行され、ジョブIDが返されます。GET /api/jobs/{job_id} でステータスと結果を確認してください。",
)
async def generate_insights(
    request: Request, discussion_id: str, req: GenerateInsightsRequest
) -> Any:
    try:
        dm = get_discussion_manager()
        principal = _principal(request)
        # Before submitting, check ai:execute and that the caller can manage it (403 / 404)
        await anyio.to_thread.run_sync(
            dm.check_insights_allowed, principal, discussion_id
        )

        cats = (
            [InsightCategory.from_dict(c.model_dump()) for c in req.categories]
            if req.categories
            else None
        )

        def _run(principal: Principal) -> list[dict]:
            # Save the generated insights to the discussion (same as regeneration; the report and documents remain)
            insights = dm.regenerate_insights(principal, discussion_id, cats)
            return [
                InsightResponse(
                    category=i.category,
                    description=i.description,
                    supporting_messages=i.supporting_messages,
                    confidence_score=i.confidence_score,
                ).model_dump()
                for i in insights
            ]

        jm = get_job_manager()
        job_id = jm.submit(principal, "insights", _run)
        return JobResponse(job_id=job_id, status="pending")
    except (HTTPException, CodedError):
        raise
    except Exception as e:
        logger.error(f"Failed to generate insight: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="インサイト生成に失敗しました"),
        )


# --- run_interview ---


@router.post(
    "/interviews",
    response_model=List[MessageResponse],
    summary="ペルソナにインタビューする",
    description="指定されたペルソナに質問を送り、回答を取得します。1回の呼び出しで1ターン（質問→回答）のみ実行され、会話コンテキストは保持されません。",
)
async def run_interview(request: Request, req: RunInterviewRequest) -> Any:
    try:
        principal = _principal(request)
        personas = await _resolve_personas(principal, req.persona_ids)
        im = get_interview_manager()
        session = await anyio.to_thread.run_sync(
            im.start_interview_session, principal, personas
        )
        try:
            reply = await anyio.to_thread.run_sync(
                im.send_user_message, principal, session.id, req.question
            )
            return [
                MessageResponse(
                    persona_id=m.persona_id,
                    persona_name=m.persona_name,
                    content=m.content,
                    timestamp=m.timestamp.isoformat(),
                    message_type=m.message_type,
                )
                for m in reply.responses
            ]
        finally:
            await anyio.to_thread.run_sync(
                im.end_interview_session, principal, session.id
            )
    except (HTTPException, CodedError):
        raise
    except Exception as e:
        logger.error(f"Interview error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(e, default="インタビューの実行に失敗しました"),
        )


# --- job status ---


# Passive request: repeated status checks. Listed in PASSIVE_REQUESTS of web/auth_middleware.py
@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="非同期ジョブのステータスを確認する",
    description="非同期ジョブ（ペルソナ生成、議論実行）のステータスと結果を取得します。statusがcompletedの場合、resultに結果が含まれます。",
)
async def get_job(request: Request, job_id: str) -> Any:
    # Only the submitting user. Someone else's, no submitter, or nonexistent makes JobManager
    # raise JOB_NOT_FOUND, and LOOKUP_UNAVAILABLE if Jobs is unreachable.
    # Both become {detail, error_code} in the shared handler (not caught here).
    jm = get_job_manager()
    job = await anyio.to_thread.run_sync(jm.get, _principal(request), job_id)
    response = JobResponse(
        job_id=job.id,
        status=job.status.value,
        result=job.result,
        # A failure is kept as a code and the wording comes from the catalog (the exception text is not returned)
        error=user_message_for_code(job.error_code) if job.error_code else None,
        error_code=job.error_code,
    )
    stored = job.result
    if (
        job.job_type == "persona_generation"
        and isinstance(stored, dict)
        and "personas" in stored
    ):
        # Generation job: result stays a persona array, with the ones that could not be saved added
        response.result = stored["personas"]
        response.failed_count = stored.get("failed_count")
        response.failed_names = stored.get("failed_names")
    return response
