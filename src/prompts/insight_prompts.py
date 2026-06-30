"""インサイト抽出プロンプトテンプレート。"""

from typing import List, Optional

from ..models.message import Message
from ..models.insight_category import InsightCategory


def build_insight_extraction_prompt(
    messages: List[Message],
    categories: Optional[List[InsightCategory]],
    topic: str = "",
) -> str:
    """インサイト抽出用プロンプトを構築する。"""
    max_round = max((msg.round_number or 0) for msg in messages)
    persona_final_statements = [
        f"**{msg.persona_name}**: {msg.content}"
        for msg in messages
        if msg.persona_id != "facilitator" and (msg.round_number or 0) > max_round - 3
    ]
    facilitator_summaries = [
        f"ラウンド{msg.round_number}: {msg.content}"
        for msg in messages
        if msg.persona_id == "facilitator"
    ]

    persona_text = "\n".join(persona_final_statements)
    facilitator_text = "\n".join(facilitator_summaries) if facilitator_summaries else ""

    if categories is None:
        categories = InsightCategory.get_default_categories()

    categories_section = ""
    for i, category in enumerate(categories, 1):
        categories_section += f"\n## {i}. {category.name}\n"
        categories_section += f"- {category.description}\n"

    category_names = [cat.name for cat in categories]
    category_names_str = "、".join([f'"{name}"' for name in category_names])

    topic_section = f"\n# 議論テーマと目的\n{topic}\n" if topic else ""

    prompt = f"""以下のペルソナ議論を分析し、議論テーマの目的に沿った実践的なインサイトを抽出してください。
{topic_section}
# ペルソナの直近の発言
{persona_text}
"""
    if facilitator_text:
        prompt += f"""
# 各ラウンドのファシリテータ要約（議論の流れ）
{facilitator_text}
"""

    prompt += f"""
# インサイト抽出の観点
以下の{len(categories)}つのカテゴリーから、議論内容に基づいた具体的で実践的なインサイトを抽出してください：
{categories_section}

# 信頼度スコアの基準
各インサイトには以下の基準で信頼度スコア（0.0-1.0）を付与してください：

- **0.9-1.0 (非常に高い)**: 複数のペルソナが明確に言及し、具体的な根拠がある
- **0.7-0.8 (高い)**: 議論の中で明確に表現され、十分な根拠がある
- **0.5-0.6 (中程度)**: 議論から推測できるが、間接的な根拠
- **0.3-0.4 (低い)**: 議論の文脈から読み取れるが、推測の要素が強い
- **0.1-0.2 (非常に低い)**: 一般的な推測に基づく

# 出力形式
以下のJSON形式で正確に出力してください。他の説明文は一切含めないでください：

[
    {{
        "category": "{category_names[0]}",
        "description": "具体的で実践的なインサイトの内容",
        "confidence_score": 0.85
    }},
    {{
        "category": "{category_names[1] if len(category_names) > 1 else category_names[0]}",
        "description": "別のインサイトの内容",
        "confidence_score": 0.72
    }}
]

# 注意事項
- 各カテゴリーから最低1つ、合計5-10個のインサイトを抽出
- categoryは必ず{category_names_str}のいずれかを使用
- 抽象的な記述ではなく、議論の内容に基づいた具体的な記述にする
- 議論で言及されていない内容を推測で追加しない
- confidence_scoreは議論内容に基づいて正直に評価する
"""
    return prompt
