"""A check of the structure of what the synthesis wrote.

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It is a pure decision: no model, no Service.

It asks for what a candidate needs to be usable, not for what is true (the
numbers are checked by ``AnalysisVerifier``):

- every persona has a name, an occupation, a background and at least one
  value, pain point and goal (blank texts do not count);
- with a table, every persona points at least at one evidence;
- with a table, the personas stand for as many different layers as there can
  be: with as many layers as personas, each layer once.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .prompts import AdvancedPersonaOutput

#: Fixed words for the log.
INCOMPLETE = "incomplete"
NO_EVIDENCE = "no_evidence"
COVERAGE = "coverage"


def _filled(items: Sequence[str]) -> bool:
    return any(str(i).strip() for i in items)


def check_personas(
    personas: Sequence[AdvancedPersonaOutput],
    segment_ids: Sequence[str],
    *,
    has_table: bool,
) -> Optional[str]:
    """Return the first problem as a fixed word, or ``None`` when all is well."""
    for persona in personas:
        texts = (persona.name, persona.occupation, persona.background)
        lists = (persona.values, persona.pain_points, persona.goals)
        if not all(str(t).strip() for t in texts) or not all(
            _filled(items) for items in lists
        ):
            return INCOMPLETE
        if has_table and not persona.evidence_ids:
            return NO_EVIDENCE
    if has_table and segment_ids:
        stood_for = {p.segment_id for p in personas if p.segment_id in set(segment_ids)}
        if len(stood_for) < min(len(personas), len(set(segment_ids))):
            return COVERAGE
    return None
