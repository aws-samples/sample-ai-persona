"""
REST API ルーター（JSON応答用）
"""

import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.managers.persona_manager import PersonaManager
from src.managers.discussion_manager import DiscussionManager

logger = logging.getLogger(__name__)

router = APIRouter()

# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_discussion_manager = None


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


@router.get("/personas", response_model=List[PersonaResponse])
async def list_personas(search: Optional[str] = None):
    """ペルソナ一覧取得API"""
    try:
        persona_manager = get_persona_manager()
        personas = persona_manager.get_all_personas()

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
async def get_persona(persona_id: str):
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
async def list_discussions():
    """議論一覧取得API"""
    try:
        discussion_manager = get_discussion_manager()
        discussions = discussion_manager.get_all_discussions()

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
async def health():
    """ヘルスチェックAPI"""
    return {"status": "ok"}
