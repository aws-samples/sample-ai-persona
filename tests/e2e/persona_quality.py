"""Measuring the quality of generated personas (U6 addendum).

Two kinds of measure, so an improvement can be shown with numbers:

- deterministic: :func:`invented_numbers` (numbers a persona states that the
  analysis never showed) and :func:`distinctness` (how different the personas
  are from each other);
- a model as the judge: :func:`judge_prompt` / :func:`judge` /
  :func:`parse_scores` score five criteria from 1 to 5.

``tests/e2e/test_persona_quality_eval.py`` uses them with the real app and
real models; the pure parts are tested in ``tests/unit``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

#: The criteria the judge scores (1 = poor, 5 = very good).
CRITERIA = ("concrete", "distinct", "consistent", "usable", "restraint")

_EXPLAIN = {
    "concrete": "具体性: 行動・暮らし・意思決定の癖が、場面が浮かぶほど具体的か",
    "distinct": "層の違い: ペルソナどうしが、行動や考え方の点で明確に違うか",
    "consistent": "データとの整合: 層の特徴・根拠と矛盾せず、それに沿っているか",
    "usable": "実用性: 指示（目的）に沿った施策や判断に、そのまま使えるか",
    "restraint": "想像の節度: データにないことを断定しすぎず、想像が妥当な範囲か",
}

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class JudgeScoreError(ValueError):
    """The judge's reply is not a full set of scores."""


@dataclass(frozen=True)
class JudgeScores:
    values: dict[str, int]
    comment: str = ""

    @property
    def mean(self) -> float:
        return sum(self.values.values()) / len(self.values)


def _loose(reply: str) -> dict[str, object]:
    """Scores read key by key, for a reply whose JSON is broken by its comment."""
    data: dict[str, object] = {}
    for criterion in CRITERIA:
        found = re.search(rf'"{criterion}"\s*:\s*(-?\d+)\s*[,}}]', reply)
        if found:
            data[criterion] = int(found.group(1))
    comment = re.search(r'"comment"\s*:\s*"(.*)', reply, re.DOTALL)
    if comment:
        data["comment"] = comment.group(1).rstrip('"} \n`')
    return data


def parse_scores(reply: str) -> JudgeScores:
    """Read the scores from the judge's reply (a JSON object inside any text).

    A comment with a raw line break or quote breaks the JSON; the scores are
    then read key by key.
    """
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0:
        raise JudgeScoreError("no JSON object")
    try:
        data = json.loads(reply[start : end + 1]) if end > start else None
    except json.JSONDecodeError:
        data = None
    if data is None:
        # a reply cut off inside its comment, or one broken by the comment
        data = _loose(reply)
    if not isinstance(data, dict):
        raise JudgeScoreError("not an object")
    values: dict[str, int] = {}
    for criterion in CRITERIA:
        value = data.get(criterion)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise JudgeScoreError(f"bad score for {criterion}")
        values[criterion] = value
    return JudgeScores(values=values, comment=str(data.get("comment") or ""))


def judge_prompt(*, purpose: str, segments_text: str, personas_text: str) -> str:
    """The request to the judge: the rubric, the analysis shown and the personas."""
    rubric = "\n".join(f"- {key}: {_EXPLAIN[key]}" for key in CRITERIA)
    return (
        "あなたは、データ分析から作ったペルソナの質を評価する担当です。"
        "次の評価の観点ごとに 1〜5 の整数で採点してください（5 が最も良い）。\n\n"
        f"# 観点\n{rubric}\n\n"
        f"# 利用者の指示（目的）\n{purpose or '（指示なし）'}\n\n"
        f"# 分析の結果（層と特徴。これがペルソナの根拠）\n{segments_text}\n\n"
        f"# ペルソナ\n{personas_text}\n\n"
        "# 出力\n次の形の JSON だけを返してください。\n"
        '{"concrete": 数, "distinct": 数, "consistent": 数, "usable": 数, '
        '"restraint": 数, "comment": "200 字以内の講評"}'
    )


def _tokens(text: str) -> list[str]:
    return [m.group(0).rstrip(",") for m in _NUMBER.finditer(text)]


def invented_numbers(
    persona_text: str, material: str, allowed: Sequence[str] = ()
) -> list[str]:
    """Numbers in ``persona_text`` that ``material`` never showed.

    Args:
        persona_text: What a persona says.
        material: What the analysis showed (layers, features, evidence).
        allowed: Numbers that belong to the persona itself (its age).
    """
    known = set(_tokens(material)) | {a.rstrip(",") for a in allowed}
    return [t for t in _tokens(persona_text) if t not in known]


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def distinctness(texts: Sequence[str]) -> float:
    """1 minus the mean overlap of the texts' character pairs (1 = all different)."""
    if len(texts) < 2:
        return 1.0
    grams = [_bigrams(t) for t in texts]
    overlaps = []
    for a, b in combinations(grams, 2):
        union = a | b
        overlaps.append(len(a & b) / len(union) if union else 1.0)
    return 1.0 - sum(overlaps) / len(overlaps)


def judge(prompt: str) -> JudgeScores:
    """Ask the model (the app's Bedrock model) to score; needs real AWS."""
    import boto3

    from src.config import config

    client = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    response = client.converse(
        modelId=config.BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 2000},
    )
    text = "".join(
        block.get("text", "") for block in response["output"]["message"]["content"]
    )
    return parse_scores(text)
