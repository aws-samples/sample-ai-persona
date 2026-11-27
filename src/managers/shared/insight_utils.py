"""Shared utilities for insight generation and category saving.

Collects the insight handling used by DiscussionManager and
AgentDiscussionManager.
"""

import logging
from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from ...models.discussion import MAX_INSIGHTS, Discussion
from ...models.insight import Insight
from ...models.insight_category import InsightCategory
from ...models.message import Message
from ...models.persona import Persona
from .participant_profiles import (
    ParticipantProfile,
    attach_profile_to_quote,
    build_participant_profiles,
)

#: Quotes shown under "根拠となる発言を見る" per insight; more adds noise, not evidence.
MAX_SUPPORTING_MESSAGES = 3
#: Characters kept per quote, so a model that pastes a whole statement stays readable.
MAX_SUPPORTING_MESSAGE_LENGTH = 200


def normalize_and_deduplicate_insights(
    raw_insights: List[Dict],
    categories: Optional[List[InsightCategory]] = None,
    profiles: Sequence[ParticipantProfile] = (),
) -> List[Dict]:
    """Normalize and de-duplicate the raw insight data returned by AIService.

    Business rules:
    - Normalize category names by partial match
    - Clamp the confidence score to 0.0-1.0
    - Filter by description min-length (>10)
    - De-duplicate descriptions
    - Cap the quoted statements and add each speaker's profile to them

    Args:
        raw_insights: Raw data returned by AIService.
        categories: Valid categories (None skips normalization).
        profiles: Participants' profiles, added to the quotes they said.

    Returns:
        Normalized insight list.
    """
    valid_category_names = [cat.name for cat in categories] if categories else []

    normalized = []
    for insight in raw_insights:
        if not isinstance(insight, dict):
            continue

        description = str(insight.get("description", "")).strip()
        if len(description) < 10:
            continue

        category = str(insight.get("category", "その他")).strip()
        if valid_category_names:
            category = _normalize_category(category, valid_category_names)

        try:
            confidence = float(insight.get("confidence_score", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5

        normalized.append(
            {
                "category": category,
                "description": description,
                "confidence_score": confidence,
                "supporting_messages": [
                    attach_profile_to_quote(quote, profiles)
                    for quote in _cap_supporting_messages(
                        insight.get("supporting_messages")
                    )
                ],
            }
        )

    return _deduplicate_by_description(normalized)


def _cap_supporting_messages(raw: object) -> List[str]:
    """Keep at most MAX_SUPPORTING_MESSAGES quotes, each trimmed to the length limit."""
    if not isinstance(raw, list):
        return []
    quotes = [q.strip() for q in raw if isinstance(q, str) and q.strip()]
    return [q[:MAX_SUPPORTING_MESSAGE_LENGTH] for q in quotes[:MAX_SUPPORTING_MESSAGES]]


def _normalize_category(category: str, valid_categories: List[str]) -> str:
    """Normalize a category name by partial match."""
    for valid_cat in valid_categories:
        if valid_cat in category or category in valid_cat:
            return valid_cat
    return valid_categories[-1] if valid_categories else "その他"


def _deduplicate_by_description(insights: List[Dict]) -> List[Dict]:
    """Remove duplicate descriptions."""
    unique = []
    seen: set = set()
    for insight in insights:
        key = insight["description"].lower().strip()
        if key not in seen:
            unique.append(insight)
            seen.add(key)
    return unique


def attach_insights_to_discussion(
    discussion: Discussion,
    messages: List[Message],
    categories: Optional[List[InsightCategory]],
    ai_service: "AIService",  # type: ignore[name-defined]  # noqa: F821
    logger: logging.Logger,
    personas: Optional[Sequence[Persona]] = None,
) -> Discussion:
    """Generate insights for a discussion, attach them and save the category settings.

    1. Extract insights (raw data) with AIService
    2. Normalize and de-duplicate in the Manager layer
    3. Add insights to the discussion (first 100 only)
    4. Save the categories to agent_config
    5. On failure return the discussion without insights (exception swallowed, warning logged)

    Args:
        discussion: Discussion to attach insights to.
        messages: The discussion's messages (kept outside the discussion body).
        categories: Custom categories (defaults used if None).
        ai_service: AIService instance.
        logger: Logger.
        personas: The participants, whose profiles go into the extraction.

    Returns:
        Discussion: The discussion with insights.
    """
    try:
        insights = generate_insights(
            messages, discussion.topic, categories, ai_service, personas=personas
        )
        if len(insights) > MAX_INSIGHTS:
            logger.info(
                f"Insights cut from {len(insights)} to {MAX_INSIGHTS}: {discussion.id}"
            )
        discussion = discussion.with_insights(
            discussion.insights + insights[: MAX_INSIGHTS - len(discussion.insights)]
        )

        if categories:
            discussion = save_categories_to_config(discussion, categories)

        logger.info(
            f"Generated {len(insights)} insights for discussion: {discussion.id}"
        )
    except Exception as e:
        logger.warning(f"インサイト生成に失敗しました: {type(e).__name__}")

    return discussion


def generate_insights(
    messages: List[Message],
    topic: str,
    categories: Optional[List[InsightCategory]],
    ai_service: "AIService",  # type: ignore[name-defined]  # noqa: F821
    personas: Optional[Sequence[Persona]] = None,
) -> List[Insight]:
    """Extract insights from messages and return them normalized and de-duplicated (no truncation).

    With ``personas``, the extraction knows who each participant is and the
    quoted statements carry the speaker's profile. Without them (for example a
    participant that can no longer be read) insights use the names only.
    """
    profiles = build_participant_profiles(personas)
    options = (
        {"participant_profiles": [p.prompt_line for p in profiles]} if profiles else {}
    )
    raw_insights = ai_service.extract_insights(
        messages, categories=categories, topic=topic, **options
    )
    return _parse_insights(
        normalize_and_deduplicate_insights(raw_insights, categories, profiles)
    )


def save_categories_to_config(
    discussion: Discussion,
    categories: List[InsightCategory],
) -> Discussion:
    """Save the categories to the discussion's agent_config.

    Pure function: returns a new Discussion instance.
    """
    if not categories:
        return discussion

    categories_data = [cat.to_dict() for cat in categories]

    agent_config = dict(discussion.agent_config or {})
    agent_config["insight_categories"] = categories_data

    return replace(discussion, agent_config=agent_config)


def get_default_insight_categories() -> List[InsightCategory]:
    """Get the default insight categories."""
    return InsightCategory.get_default_categories()


def _parse_insights(insight_data_list: List[dict]) -> List[Insight]:
    """Build a list of Insight objects from structured data."""
    insights: List[Insight] = []
    for data in insight_data_list:
        if not isinstance(data, dict):
            continue
        try:
            insight = Insight.create_new(
                category=data.get("category", "その他"),
                description=data.get("description", ""),
                supporting_messages=data.get("supporting_messages", []),
                confidence_score=float(data.get("confidence_score", 0.5)),
            )
            insights.append(insight)
        except (ValueError, TypeError):
            continue
    return insights
