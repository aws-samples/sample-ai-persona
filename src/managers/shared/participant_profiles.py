"""Short profiles of discussion participants for insight extraction.

Insights name who said what. A name alone sends the reader to another screen to
find out who that person is, so each participant gets a short profile: age,
gender and occupation, plus the customer segment and its share when the persona
came from an advanced generation. The profile goes into the extraction prompt
and is added to each quoted supporting statement.
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from ...models.demographics import gender_label
from ...models.persona import Persona

#: Characters of the occupation kept in a label (some are long descriptions).
MAX_OCCUPATION_LENGTH = 20
#: Values and pain points given to the extraction prompt per participant.
MAX_PROMPT_VALUES = 3
MAX_PROMPT_PAIN_POINTS = 2

_SPEAKER_SEPARATOR = re.compile(r"[:：]")
_ANNOTATION = re.compile(r"[（(].*$")


@dataclass(frozen=True)
class ParticipantProfile:
    """One participant's profile.

    Attributes:
        name: The persona name as spoken in the discussion.
        label: Short profile for quotes (e.g. ``38歳・女性・パート勤務／…層（全体の48%）``).
        prompt_line: The line given to the extraction prompt.
    """

    name: str
    label: str
    prompt_line: str


def build_participant_profiles(
    personas: Optional[Iterable[Persona]],
) -> List[ParticipantProfile]:
    """Build a profile for each persona (an empty list when there are none)."""
    profiles = []
    for persona in personas or []:
        label = _label(persona)
        line = f"- {persona.name}: {label}"
        if persona.values:
            line += "／価値観: " + "、".join(persona.values[:MAX_PROMPT_VALUES])
        if persona.pain_points:
            line += "／課題: " + "、".join(persona.pain_points[:MAX_PROMPT_PAIN_POINTS])
        profiles.append(ParticipantProfile(persona.name, label, line))
    return profiles


def attach_profile_to_quote(quote: str, profiles: Sequence[ParticipantProfile]) -> str:
    """Turn ``名前: 発言`` into ``名前（プロフィール）: 発言``.

    The speaker is matched by full name (spaces and a trailing さん ignored) or
    by a family name only one participant has. Anything else is returned as it
    is, so a wrong profile is never attached.
    """
    parts = _SPEAKER_SEPARATOR.split(quote, maxsplit=1)
    if len(parts) != 2 or not profiles:
        return quote
    speaker = _normalize(_ANNOTATION.sub("", parts[0]))
    if not speaker:
        return quote
    profile = _find(speaker, profiles)
    if profile is None:
        return quote
    return f"{profile.name}（{profile.label}）: {parts[1].strip()}"


def _label(persona: Persona) -> str:
    items = [f"{persona.age}歳"]
    gender = gender_label(persona.gender)
    if gender:
        items.append(gender)
    occupation = _short_occupation(persona.occupation)
    if occupation:
        items.append(occupation)
    label = "・".join(items)
    segment = _segment_text(persona.generation_context)
    return f"{label}／{segment}" if segment else label


def _short_occupation(occupation: str) -> str:
    # The first part before a note or a list is the occupation itself
    head = re.split(r"[（(・、,]", occupation or "", maxsplit=1)[0].strip()
    return head[:MAX_OCCUPATION_LENGTH]


def _segment_text(context: Optional[Mapping[str, Any]]) -> str:
    """The persona's segment and share; the share of an unverified segment is left out."""
    if not context:
        return ""
    segment_id = context.get("segment_id")
    segment = next(
        (
            s
            for s in context.get("segments") or []
            if isinstance(s, Mapping) and s.get("id") == segment_id
        ),
        None,
    )
    if segment is None or not segment.get("label"):
        return ""
    text = str(segment["label"])
    unverified = context.get("unverified_segment_ids") or []
    share = segment.get("share")
    if segment_id not in unverified and isinstance(share, (int, float)):
        text += f"（全体の{share * 100:.0f}%）"
    return text


def _normalize(name: str) -> str:
    return re.sub(r"\s", "", name).removesuffix("さん")


def _find(
    speaker: str, profiles: Sequence[ParticipantProfile]
) -> Optional[ParticipantProfile]:
    for profile in profiles:
        if _normalize(profile.name) == speaker:
            return profile
    family = [p for p in profiles if p.name.split()[:1] == [speaker]]
    return family[0] if len(family) == 1 else None
