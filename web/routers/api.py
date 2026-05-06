"""
REST API ルーター（JSON応答用）

ペルソナ、議論、インタビュー、インサイト生成のREST APIを提供する。
AgentCore Gateway 経由で外部AIエージェントからも利用可能。
"""

import logging
from dataclasses import replace
from typing import Annotated, Any, Literal, Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.managers.persona_manager import PersonaManager
from src.managers.discussion_manager import DiscussionManager
from src.managers.agent_discussion_manager import AgentDiscussionManager
from src.managers.interview_manager import InterviewManager
from src.managers.job_manager import JobManager
from src.models.insight_category import InsightCategory

logger = logging.getLogger(__name__)

router = APIRouter()

# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager: PersonaManager | None = None
_discussion_manager: DiscussionManager | None = None
_agent_discussion_manager: AgentDiscussionManager | None = None
_interview_manager: InterviewManager | None = None
_job_manager: JobManager | None = None


def get_persona_manager() -> PersonaManager:
    """PersonaManagerのシングルトンインスタンスを取得"""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_discussion_manager() -> DiscussionManager:
    """DiscussionManagerのシングルトンインスタンスを取得"""
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


class DiscussionResponse(BaseModel):
    id: str
    topic: str
    mode: str
    created_at: str


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


# --- MCP Request models ---

class GeneratePersonasRequest(BaseModel):
    """ペルソナ生成リクエスト"""
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
        default=None, max_length=1000, description="データの説明（data_type=other時に使用）"
    )
    custom_prompt: Optional[str] = Field(
        default=None, max_length=2000, description="カスタムプロンプト（最大2000文字）"
    )


class CategoryInput(BaseModel):
    """インサイトカテゴリ入力"""
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)


class RunDiscussionRequest(BaseModel):
    """議論実行リクエスト"""
    persona_ids: List[str] = Field(
        min_length=2, description="参加ペルソナIDリスト（2名以上）"
    )
    topic: str = Field(min_length=1, max_length=500, description="議論トピック")
    mode: Literal["classic", "agent"] = Field(
        default="classic",
        description="議論モード: classic または agent",
    )
    categories: Optional[List[CategoryInput]] = Field(
        default=None,
        max_length=10,
        description="カスタムインサイトカテゴリ（最大10件）",
    )


class GenerateInsightsRequest(BaseModel):
    """インサイト生成リクエスト"""
    categories: Optional[List[CategoryInput]] = Field(
        default=None,
        max_length=10,
        description="カスタムインサイトカテゴリ（最大10件）",
    )


class RunInterviewRequest(BaseModel):
    """インタビュー実行リクエスト"""
    persona_ids: List[str] = Field(
        min_length=1, max_length=5, description="参加ペルソナIDリスト（1-5名）"
    )
    question: str = Field(min_length=1, max_length=5000, description="質問内容")


@router.get("/personas", response_model=List[PersonaResponse])
async def list_personas(search: Optional[str] = None) -> Any:
    """ペルソナ一覧取得API"""
    try:
        persona_manager = get_persona_manager()
        personas = persona_manager.get_all_personas_full()

        if search:
            search_lower = search.lower()
            personas = [
                p
                for p in personas
                if search_lower in p.name.lower()
                or search_lower in p.occupation.lower()
            ]

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
            )
            for p in personas
        ]
    except Exception as e:
        logger.error(f"ペルソナ一覧取得エラー: {e}")
        raise HTTPException(status_code=500, detail="ペルソナ一覧の取得に失敗しました")


@router.get("/personas/{persona_id}", response_model=PersonaResponse)
async def get_persona(persona_id: str) -> Any:
    """ペルソナ詳細取得API"""
    try:
        persona_manager = get_persona_manager()
        persona = persona_manager.get_persona(persona_id)

        if not persona:
            raise HTTPException(status_code=404, detail="ペルソナが見つかりません")

        return PersonaResponse(
            id=persona.id,
            name=persona.name,
            age=persona.age,
            occupation=persona.occupation,
            background=persona.background,
            values=persona.values,
            pain_points=persona.pain_points,
            goals=persona.goals,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ペルソナ取得エラー: {e}")
        raise HTTPException(status_code=500, detail="ペルソナの取得に失敗しました")


@router.get("/discussions")
async def list_discussions() -> Any:
    """議論一覧取得API"""
    try:
        discussion_manager = get_discussion_manager()
        discussions, _ = discussion_manager.get_discussion_history(search_all=True)

        return [
            {
                "id": d.id,
                "topic": d.topic,
                "mode": d.mode,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in discussions
        ]
    except Exception as e:
        logger.error(f"議論一覧取得エラー: {e}")
        raise HTTPException(status_code=500, detail="議論一覧の取得に失敗しました")


@router.get("/health")
async def health() -> Any:
    """ヘルスチェックAPI"""
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


def _resolve_personas(persona_ids: List[str]) -> list:
    """persona_ids からペルソナオブジェクトを取得。見つからなければ HTTPException"""
    pm = get_persona_manager()
    personas = []
    for pid in persona_ids:
        p = pm.get_persona(pid)
        if not p:
            raise HTTPException(status_code=404, detail=f"ペルソナが見つかりません: {pid}")
        personas.append(p)
    return personas


# --- generate_personas (async job) ---

def _run_generate_personas(
    file_contents: list[tuple[bytes, str]],
    data_type: str,
    persona_count: int,
    data_description: str | None,
    custom_prompt: str | None,
) -> list[dict]:
    """バックグラウンドスレッドで実行されるペルソナ生成処理"""
    pm = get_persona_manager()
    personas, _logs = pm.generate_personas(
        file_contents=file_contents,
        data_type=data_type,
        persona_count=persona_count,
        data_description=data_description,
        custom_prompt=custom_prompt,
    )
    # 生成されたペルソナを保存
    for p in personas:
        pm.save_persona(p)
    return [_persona_to_response(p).model_dump() for p in personas]


@router.post(
    "/personas/generate",
    response_model=JobResponse,
    status_code=202,
    summary="AIペルソナを生成する",
    description="テキストデータからAIペルソナを生成します。処理は非同期で実行され、ジョブIDが返されます。GET /api/jobs/{job_id} でステータスと結果を確認してください。",
)
async def generate_personas(req: GeneratePersonasRequest) -> Any:
    try:
        file_contents: list[tuple[bytes, str]] = [
            (text.encode("utf-8"), f"input_{i}.txt")
            for i, text in enumerate(req.file_contents)
        ]
        jm = get_job_manager()
        job_id = jm.submit(
            _run_generate_personas,
            file_contents,
            req.data_type,
            req.count,
            req.description,
            req.custom_prompt,
        )
        return JobResponse(job_id=job_id, status="pending")
    except Exception as e:
        logger.error(f"ペルソナ生成ジョブ投入エラー: {e}")
        raise HTTPException(status_code=500, detail="ペルソナ生成の開始に失敗しました")


# --- run_discussion (async job) ---

def _run_classic_discussion(
    personas: list,
    topic: str,
    categories: list | None,
) -> dict:
    """Classic議論をバックグラウンドで実行"""
    dm = get_discussion_manager()
    discussion = dm.start_discussion(personas=personas, topic=topic)
    cats = (
        [InsightCategory.from_dict(c.model_dump()) for c in categories] if categories else None
    )
    insights = dm.generate_insights(discussion, categories=cats)
    discussion = replace(discussion, insights=insights)
    dm.save_discussion(discussion)
    return _discussion_detail(discussion)


def _run_agent_discussion(
    personas: list,
    topic: str,
    categories: list | None,
) -> dict:
    """Agent議論をバックグラウンドで実行"""
    adm = get_agent_discussion_manager()
    dm = get_discussion_manager()

    system_prompts: dict[str, str] = {}  # デフォルトのシステムプロンプトを使用
    persona_agents = adm.create_persona_agents(personas, system_prompts)
    facilitator = adm.create_facilitator_agent(rounds=3)
    try:
        discussion = adm.start_agent_discussion(
            personas=personas,
            topic=topic,
            persona_agents=persona_agents,
            facilitator=facilitator,
        )
        cats = (
            [InsightCategory.from_dict(c.model_dump()) for c in categories] if categories else None
        )
        insights = dm.generate_insights(discussion, categories=cats)
        discussion = replace(discussion, insights=insights)
        adm.save_agent_discussion(discussion)
        return _discussion_detail(discussion)
    finally:
        adm.cleanup_agents(persona_agents, facilitator)


def _discussion_detail(discussion: Any) -> dict:
    return DiscussionDetailResponse(
        id=discussion.id,
        topic=discussion.topic,
        mode=discussion.mode,
        participants=discussion.participants,
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
async def run_discussion(req: RunDiscussionRequest) -> Any:
    try:
        personas = _resolve_personas(req.persona_ids)
        jm = get_job_manager()
        runner = _run_classic_discussion if req.mode == "classic" else _run_agent_discussion
        job_id = jm.submit(runner, personas, req.topic, req.categories)
        return JobResponse(job_id=job_id, status="pending")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"議論ジョブ投入エラー: {e}")
        raise HTTPException(status_code=500, detail="議論の開始に失敗しました")


# --- get_discussion ---

@router.get(
    "/discussions/{discussion_id}",
    response_model=DiscussionDetailResponse,
    summary="議論結果を取得する",
    description="指定されたIDの議論結果（メッセージ、インサイト含む）を取得します。",
)
async def get_discussion_detail(discussion_id: str) -> Any:
    try:
        dm = get_discussion_manager()
        discussion = dm.get_discussion(discussion_id)
        if not discussion:
            raise HTTPException(status_code=404, detail="議論が見つかりません")
        return _discussion_detail(discussion)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"議論取得エラー: {e}")
        raise HTTPException(status_code=500, detail="議論の取得に失敗しました")


# --- generate_insights ---

@router.post(
    "/discussions/{discussion_id}/insights",
    response_model=JobResponse,
    status_code=202,
    summary="議論結果からインサイトを生成する",
    description="保存済みの議論結果からインサイトを生成します。処理は非同期で実行され、ジョブIDが返されます。GET /api/jobs/{job_id} でステータスと結果を確認してください。",
)
async def generate_insights(
    discussion_id: str, req: GenerateInsightsRequest
) -> Any:
    try:
        dm = get_discussion_manager()
        discussion = dm.get_discussion(discussion_id)
        if not discussion:
            raise HTTPException(status_code=404, detail="議論が見つかりません")

        cats = (
            [InsightCategory.from_dict(c.model_dump()) for c in req.categories]
            if req.categories
            else None
        )

        def _run() -> list[dict]:
            insights = dm.generate_insights(discussion, categories=cats)
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
        job_id = jm.submit(_run)
        return JobResponse(job_id=job_id, status="pending")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"インサイト生成エラー: {e}")
        raise HTTPException(status_code=500, detail="インサイト生成に失敗しました")


# --- run_interview ---

@router.post(
    "/interviews",
    response_model=List[MessageResponse],
    summary="ペルソナにインタビューする",
    description="指定されたペルソナに質問を送り、回答を取得します。1回の呼び出しで1ターン（質問→回答）のみ実行され、会話コンテキストは保持されません。",
)
async def run_interview(req: RunInterviewRequest) -> Any:
    try:
        personas = _resolve_personas(req.persona_ids)
        im = get_interview_manager()
        session = im.start_interview_session(personas=personas)
        try:
            responses = im.send_user_message(
                session_id=session.id, message=req.question
            )
            return [
                MessageResponse(
                    persona_id=m.persona_id,
                    persona_name=m.persona_name,
                    content=m.content,
                    timestamp=m.timestamp.isoformat(),
                    message_type=m.message_type,
                )
                for m in responses
            ]
        finally:
            im.end_interview_session(session.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"インタビューエラー: {e}")
        raise HTTPException(status_code=500, detail="インタビューの実行に失敗しました")


# --- job status ---

@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="非同期ジョブのステータスを確認する",
    description="非同期ジョブ（ペルソナ生成、議論実行）のステータスと結果を取得します。statusがcompletedの場合、resultに結果が含まれます。",
)
async def get_job(job_id: str) -> Any:
    jm = get_job_manager()
    job = jm.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return JobResponse(
        job_id=job.id,
        status=job.status.value,
        result=job.result,
        error=job.error,
    )
