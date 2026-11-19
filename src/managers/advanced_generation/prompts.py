"""Instructions, prompts and the persona schema of the advanced generation (U1).

The prompts depend on the method and the phase (business logic), so they live
with the Manager (``architecture.md``: the Service only builds the agents and
wraps the data). Data is never written into these texts: it goes to the
Service as ``data`` blocks, which it wraps as untrusted (BR5.6).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ...models.analysis import AnalysisMethod, AnalysisPlan

PLANNING_INSTRUCTIONS = (
    "あなたは表データの分析の計画を立てる担当です。利用者の指示（purpose）と、"
    "表の列ごとの要約（profile）を読み、分析の計画を 1 つ作ってください。\n"
    "手法の目安: 対象を指定する指示なら filter、比較を求める指示なら compare、"
    "指示が空欄で 1 人 1 行の表なら cluster。目安と違う手法を選ぶときは、"
    "deviation_reason に理由を必ず書いてください。\n"
    "filter で複数のペルソナを作るときは、層を分ける列を split_axis に、"
    "profile にある列名のまま書いてください。"
)

ANALYSIS_INSTRUCTIONS = (
    "あなたは表データを分析する担当です。渡されたツールだけを使ってください。"
    "SQL は書けません。列名は profile_dataset の結果にある名前だけを使ってください。\n"
    "分析の結果は、ペルソナが代表する層（segments）と、その根拠（evidence）だけです。"
    "各層の size と share は、ツールの出力に出てきた値をそのまま使い、"
    "definition_tool_call_id にその値を出したツールの tool_call_id を書いてください。"
    "根拠の tool_call_id も、その集計を出したツールのものにしてください。"
)

SYNTHESIS_INSTRUCTIONS = (
    "あなたは分析の結果からペルソナを書く担当です。渡された層（segments）と"
    "根拠（evidence）と前準備の要約だけを使い、表の行の値を作らないでください。"
    "各ペルソナには、代表する層の id を segment_id に、使った根拠の id を "
    "evidence_ids に書いてください。"
)

_METHOD_GUIDE = {
    AnalysisMethod.FILTER: (
        "手法は filter です。analyze_dataset の filters で対象を絞り、"
        "group_by と count で層の行数を数えてください。"
    ),
    AnalysisMethod.COMPARE: (
        "手法は compare です。analyze_dataset で比べる群ごとに group_by して、"
        "count と比べる指標を集計してください。"
    ),
    AnalysisMethod.CLUSTER: (
        "手法は cluster です。cluster_segments に、特徴にする数値・カテゴリの列を"
        "渡してください。群がそのまま層になります。列が足りない・群が空になる"
        "というエラーが返ったら、別の列を選んでもう 1 回だけ試してください。"
    ),
}


def planning_prompt(persona_count: int, attempt: int) -> str:
    """The planning request (asked again once when the reason is missing)."""
    prompt = f"作るペルソナの数は {persona_count} 人です。分析の計画を作ってください。"
    if attempt > 1:
        prompt += (
            "\n前回の計画は目安と違う手法なのに deviation_reason がありませんでした。"
            "理由を書くか、目安の手法にしてください。"
        )
    return prompt


def analysis_prompt(plan: AnalysisPlan, persona_count: int) -> str:
    """The analysis request for the planned method (BR5.2〜BR5.4)."""
    lines = [
        _METHOD_GUIDE[plan.method],
        f"層の数はペルソナの数と同じ {persona_count} にしてください。",
    ]
    if plan.method is AnalysisMethod.FILTER and persona_count >= 2:
        axis = plan.split_axis or "絞った層をよく分ける列"
        lines.append(
            f"絞った層を {axis} で group_by し、行数の多い順に {persona_count} 個の"
            "層にしてください。分けられないときは、絞った層を 1 つにしてください。"
        )
    return "\n".join(lines)


def synthesis_prompt(persona_count: int) -> str:
    """The synthesis request (exactly ``persona_count`` personas, BR5.5)."""
    return (
        f"ペルソナをちょうど {persona_count} 人書いてください。"
        "どのペルソナも、segments のどれか 1 つの層を代表させてください。"
    )


class AdvancedPersonaOutput(BaseModel):
    """One persona the synthesis agent writes (Persona fields + its segment)."""

    name: str = Field(description="名前（国・地域に即した自然な名前。日本語表記）")
    age: int = Field(description="年齢")
    gender: Optional[str] = Field(default=None, description="male / female / other")
    country: Optional[str] = Field(default=None, description="ISO 3166-1 alpha-2")
    city: Optional[str] = Field(default=None, description="居住都市名")
    occupation: str = Field(description="職業")
    background: str = Field(description="背景・経歴")
    values: list[str] = Field(description="価値観")
    pain_points: list[str] = Field(description="課題・悩み")
    goals: list[str] = Field(description="目標・願望")
    segment_id: str = Field(description="代表する層の id")
    evidence_ids: list[str] = Field(default_factory=list, description="使った根拠")


class AdvancedPersonaListOutput(BaseModel):
    """What the synthesis agent returns."""

    personas: list[AdvancedPersonaOutput]
