"""Shared utilities for insight generation and category saving.

Collects the insight handling used by DiscussionManager and
AgentDiscussionManager.
"""

import logging
from dataclasses import replace
from typing import Dict, List, Optional

from ...models.discussion import MAX_INSIGHTS, Discussion
from ...models.insight import Insight
from ...models.insight_category import InsightCategory
from ...models.message import Message


def normalize_and_deduplicate_insights(
    raw_insights: List[Dict],
    categories: Optional[List[InsightCategory]] = None,
) -> List[Dict]:
    """Normalize and de-duplicate the raw insight data returned by AIService.

    Business rules:
    - Normalize category names by partial match
    - Clamp the confidence score to 0.0-1.0
    - Filter by description min-length (>10)
    - De-duplicate descriptions

    Args:
        raw_insights: Raw data returned by AIService.
        categories: Valid categories (None skips normalization).

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
            }
        )

    return _deduplicate_by_description(normalized)


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

    Returns:
        Discussion: The discussion with insights.
    """
    try:
        insights = generate_insights(messages, discussion.topic, categories, ai_service)
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
) -> List[Insight]:
    """Extract insights from messages and return them normalized and de-duplicated (no truncation)."""
    raw_insights = ai_service.extract_insights(
        messages, categories=categories, topic=topic
    )
    return _parse_insights(normalize_and_deduplicate_insights(raw_insights, categories))


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
