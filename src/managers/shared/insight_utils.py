"""インサイト生成・カテゴリー保存の共通ユーティリティ。

DiscussionManager, AgentDiscussionManager から共通利用される
インサイト処理ロジックを集約する。
"""

import logging
from typing import Dict, List, Optional

from ...models.discussion import Discussion
from ...models.insight import Insight
from ...models.insight_category import InsightCategory


def normalize_and_deduplicate_insights(
    raw_insights: List[Dict],
    categories: Optional[List[InsightCategory]] = None,
) -> List[Dict]:
    """AIServiceから返された生インサイトデータを正規化・重複除去する。

    ビジネスルール:
    - カテゴリ名の部分一致による正規化
    - confidence scoreの0.0-1.0クランプ
    - description min-length (>10) フィルタ
    - description重複除去

    Args:
        raw_insights: AIServiceが返した生データ
        categories: 有効なカテゴリー（Noneなら正規化スキップ）

    Returns:
        正規化済みインサイトリスト
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
    """カテゴリ名を部分一致で正規化する。"""
    for valid_cat in valid_categories:
        if valid_cat in category or category in valid_cat:
            return valid_cat
    return valid_categories[-1] if valid_categories else "その他"


def _deduplicate_by_description(insights: List[Dict]) -> List[Dict]:
    """descriptionの重複を除去する。"""
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
    categories: Optional[List[InsightCategory]],
    ai_service: "AIService",  # type: ignore[name-defined]  # noqa: F821
    logger: logging.Logger,
) -> Discussion:
    """議論にインサイトを生成・付与し、カテゴリー設定を保存する。

    1. AIServiceでインサイト抽出（生データ）
    2. Manager層で正規化・重複除去
    3. 議論オブジェクトにインサイト追加
    4. カテゴリーをagent_configに保存
    5. 失敗時はインサイトなしの議論をそのまま返す（例外を握りつぶし警告ログ）

    Args:
        discussion: インサイトを付与する議論オブジェクト
        categories: カスタムカテゴリー（Noneならデフォルト使用）
        ai_service: AIServiceインスタンス
        logger: ロガー

    Returns:
        Discussion: インサイト付き議論オブジェクト
    """
    try:
        raw_insights = ai_service.extract_insights(
            discussion.messages, categories=categories, topic=discussion.topic
        )
        normalized = normalize_and_deduplicate_insights(raw_insights, categories)
        insights = _parse_insights(normalized)

        for insight in insights:
            discussion = discussion.add_insight(insight)

        if categories:
            discussion = save_categories_to_config(discussion, categories)

        logger.info(
            f"Generated {len(insights)} insights for discussion: {discussion.id}"
        )
    except Exception as e:
        logger.warning(f"インサイト生成に失敗しました: {e}")

    return discussion


def save_categories_to_config(
    discussion: Discussion,
    categories: List[InsightCategory],
) -> Discussion:
    """カテゴリーを議論のagent_configに保存する。

    純粋関数: Discussionの新インスタンスを返す。
    """
    if not categories:
        return discussion

    categories_data = [cat.to_dict() for cat in categories]

    agent_config = discussion.agent_config or {}
    agent_config["insight_categories"] = categories_data

    return Discussion(
        id=discussion.id,
        topic=discussion.topic,
        participants=discussion.participants,
        messages=discussion.messages,
        insights=discussion.insights,
        created_at=discussion.created_at,
        mode=discussion.mode,
        agent_config=agent_config,
        documents=discussion.documents,
    )


def get_default_insight_categories() -> List[InsightCategory]:
    """デフォルトのインサイトカテゴリーを取得する。"""
    return InsightCategory.get_default_categories()


def _parse_insights(insight_data_list: List[dict]) -> List[Insight]:
    """構造化データからInsightオブジェクトのリストを生成する。"""
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
