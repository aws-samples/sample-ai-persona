"""Prompt templates for insight extraction."""

from typing import List, Optional

from ..models.message import Message
from ..models.insight_category import InsightCategory


def build_insight_extraction_prompt(
    messages: List[Message],
    categories: Optional[List[InsightCategory]],
    topic: str = "",
    participant_profiles: Optional[List[str]] = None,
) -> str:
    """Build the prompt for insight extraction.

    Statements from every round are included: the first round carries the most
    concrete episodes, which are the raw material for new hypotheses. With
    ``participant_profiles`` the insights say what kind of person held a view,
    not only their name.
    """
    persona_statements = [
        f"{_round_label(msg)}**{msg.persona_name}**: {msg.content}"
        for msg in messages
        if msg.persona_id != "facilitator"
    ]
    facilitator_summaries = [
        f"ラウンド{msg.round_number}: {msg.content}"
        for msg in messages
        if msg.persona_id == "facilitator"
    ]

    persona_text = "\n".join(persona_statements)
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
    profile_section = (
        "\n# 参加者のプロフィール\n" + "\n".join(participant_profiles) + "\n"
        if participant_profiles
        else ""
    )
    profile_rules = (
        "- 観察では、発言した人を名前だけでなくプロフィール（年代・暮らし方・層など）で書いてください。"
        "例: 「夜に惣菜を買う20代の一人暮らし（岡田さん）は…」\n"
        "- 仮説では、どんな人（層）に当てはまるかを必ず書いてください。層の割合が分かる場合は添えてください\n"
        if participant_profiles
        else ""
    )

    prompt = f"""以下のペルソナ議論を分析し、議論テーマの目的に沿って、新しい仮説につながるインサイトを抽出してください。
{topic_section}{profile_section}
# ペルソナの発言（[R番号] はラウンド）
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

# 観察と仮説
- 発言にない事実を作らないでください
- 発言だけで言えること（はっきり語られた事実や、複数の人が述べた共通点など）は、description を「観察: …」だけで書いてください
- 発言から一歩踏み込んだ推論があるときだけ、description を「観察: …／仮説: …」の形にし、観察には発言に基づく事実、仮説にはそこからの推論を書いてください。観察を言い直しただけの仮説は付けないでください
{profile_rules}- 多くの参加者が同意した点だけでなく、対立点や1人だけが述べた意見、意外な発言から最低2個のインサイトを抽出してください

# 信頼度スコアの基準
信頼度スコア（0.0-1.0）は、言及した人数ではなく根拠の強さで付けてください。1人の具体的な体験に基づくものでも、根拠が明確なら0.7以上にしてかまいません。

- **0.9-1.0 (非常に高い)**: 具体的な体験や理由が明確に語られ、他の発言とも矛盾しない
- **0.7-0.8 (高い)**: 具体的な体験や理由に基づいている（1人の発言でもよい）
- **0.5-0.6 (中程度)**: 発言から推論できるが、根拠が間接的
- **0.3-0.4 (低い)**: 発言の文脈から読み取れるが、推論の要素が強い
- **0.1-0.2 (非常に低い)**: 一般的な推測に基づく

# 出力形式
以下のJSON形式で正確に出力してください。他の説明文は一切含めないでください：

[
    {{
        "category": "{category_names[0]}",
        "description": "観察: 発言に基づく事実",
        "confidence_score": 0.8,
        "supporting_messages": ["ペルソナ名: 根拠となる発言の抜粋"]
    }},
    {{
        "category": "{category_names[1] if len(category_names) > 1 else category_names[0]}",
        "description": "観察: 発言に基づく事実／仮説: そこから推論した仮説",
        "confidence_score": 0.65,
        "supporting_messages": ["ペルソナ名: 根拠となる発言の抜粋", "別のペルソナ名: 根拠となる発言の抜粋"]
    }}
]

# 注意事項
- 各カテゴリーから最低1つ、合計5-10個のインサイトを抽出
- categoryは必ず{category_names_str}のいずれかを使用
- supporting_messages には根拠となる発言を1-3件、「ペルソナ名: 発言の抜粋」の形で原文から抜き出す（言い換えない）
- 抽象的な記述ではなく、議論の内容に基づいた具体的な記述にする
- confidence_scoreは根拠の強さに基づいて正直に評価する
"""
    return prompt


def _round_label(msg: Message) -> str:
    """Return the round label for a statement (empty when the round is unknown)."""
    return f"[R{msg.round_number}] " if msg.round_number else ""
