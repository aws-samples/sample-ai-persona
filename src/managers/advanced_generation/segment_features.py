"""The features of a segment: only numbers that were counted (U6 addendum).

Only ``AdvancedPersonaGenerationManager`` uses this, so it is a module of that
Manager's package. It is a pure decision: no model, no Service.

The analysis agent used to write each segment's features as free text, which
nothing checked (a "baseline" could hold another segment's figure). Now a
feature is a number the library counted. A number merely seen in a tool
output cannot be tied to a column, a condition and a segment, so what the
agent writes is not used:

- ``run_code`` segment: the features the library counted from the axis table
  and the recounted assignment (``kind`` ``mean`` or ``share``), and the mean of
  each metric the code computed (``derived``: the library averages them, but
  the values are the code's own and are shown as such). The ``features`` the AI
  code passed are not used.
- ``cluster`` segment: the features ``cluster_segments`` counted.
- any other segment (a fixed tool such as ``analyze_dataset``): the layer of
  the ``profile_segments`` call the segment names (``profile_tool_call_id``,
  ``profile_key``), when that layer counted the same number of people as the
  segment. The size, which the verification matched to a count of the tool
  output, is what ties the counted layer to the segment; without a matching
  layer there are no features.

Features are put in order of how far they are from the whole, and only the
first :data:`MAX_FEATURES` stay. One that is hardly different from the whole
is no trait of the segment and is left out: under :data:`MIN_EFFECT` spreads
(standard deviations for a mean, Cohen's h for a share), or, when no spread is
known, under :data:`MIN_DIFFERENCE` of the whole.
"""

from __future__ import annotations

import math
from typing import Any, Collection, Iterator, Mapping, Optional, Sequence

from ...models.analysis import EmittedResult, Segment

#: Features that stay for one segment, the biggest differences first.
MAX_FEATURES = 6
#: Places kept for the metrics the code computed.
DERIVED_SLOTS = 3
#: A feature closer to the whole than this (relative to the whole) is no trait.
MIN_DIFFERENCE = 0.05
#: The smallest effect that is a trait: 0.2 spreads (Cohen's small effect).
MIN_EFFECT = 0.2
_DIGITS = 3
_KINDS = frozenset({"mean", "share", "derived"})


def _number(value: Any) -> Optional[float]:
    """``value`` as a number, or ``None`` (text, bool, NaN)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _numbers(value: Any) -> Iterator[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers(item)


def _feature(item: Any, kind: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One feature as ``{name, kind, value, baseline}`` with numbers, or ``None``."""
    if not isinstance(item, Mapping):
        return None
    name = str(item.get("name") or "").strip()
    value = _number(item.get("value"))
    baseline = _number(item.get("baseline"))
    chosen = kind or str(item.get("kind") or "")
    if not name or value is None or baseline is None or chosen not in _KINDS:
        return None
    feature: dict[str, Any] = {
        "name": name,
        "kind": chosen,
        "value": value,
        "baseline": baseline,
    }
    spread = _number(item.get("spread"))
    if spread is not None and spread > 0:
        feature["spread"] = spread
    return feature


def _proportion(value: float) -> Optional[float]:
    return value if 0.0 <= value <= 1.0 else None


def _effect(feature: Mapping[str, Any]) -> Optional[float]:
    """The size of the difference, free of the column's unit and offset.

    A mean is measured in standard deviations of all rows, a share as Cohen's
    ``h`` (the difference of the arcsine-root proportions). ``None`` when the
    figures do not allow it (no spread, a share outside 0..1).
    """
    value, baseline = float(feature["value"]), float(feature["baseline"])
    if feature["kind"] == "share":
        a, b = _proportion(value), _proportion(baseline)
        if a is None or b is None:
            return None
        return abs(2 * math.asin(math.sqrt(a)) - 2 * math.asin(math.sqrt(b)))
    spread = feature.get("spread")
    if spread:
        return abs(value - baseline) / float(spread)
    return None


def _distance(feature: Mapping[str, Any]) -> float:
    effect = _effect(feature)
    if effect is not None:
        return effect
    value, baseline = float(feature["value"]), float(feature["baseline"])
    return abs(value - baseline) / max(abs(baseline), 1e-9)


def _is_trait(feature: Mapping[str, Any]) -> bool:
    """Whether the feature differs enough from the whole to say who the people are."""
    effect = _effect(feature)
    if effect is not None:
        return effect >= MIN_EFFECT
    return _distance(feature) >= MIN_DIFFERENCE


def _ranked(features: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    features = [f for f in features if _is_trait(f)]
    return _ranked_by_distance(features)


def _ranked_by_distance(
    features: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """The biggest differences first; metrics the code computed keep some places.

    The metrics (``derived``) are what the purpose is often about (purchases),
    so up to :data:`DERIVED_SLOTS` of them stay even when the columns of the
    table differ more.
    """
    ordered = sorted(features, key=_distance, reverse=True)
    derived = [f for f in ordered if f["kind"] == "derived"][:DERIVED_SLOTS]
    rest = [f for f in ordered if f not in derived][: MAX_FEATURES - len(derived)]
    kept = sorted([*derived, *rest], key=_distance, reverse=True)
    return tuple(dict(f) for f in kept)


def _counted(items: Any, skip: Collection[str] = ()) -> tuple[dict[str, Any], ...]:
    if not isinstance(items, (list, tuple)):
        return ()
    made = (_feature(i) for i in items)
    return _ranked([f for f in made if f is not None and _column_of(f) not in skip])


def _cluster_features(segment: Segment, outputs: Mapping[str, Any]) -> Any:
    spec = segment.definition.get("spec")
    index = spec.get("cluster_index") if isinstance(spec, Mapping) else None
    output = outputs.get(segment.tool_call_id or "")
    clusters = output.get("clusters") if isinstance(output, Mapping) else None
    if not isinstance(clusters, list):
        return ()
    for cluster in clusters:
        if isinstance(cluster, Mapping) and str(cluster.get("index")) == str(index):
            return cluster.get("features")
    return ()


def _profiled(segment: Segment, outputs: Mapping[str, Any]) -> Any:
    """The features of the segment's ``profile_segments`` layer, if it is its."""
    output = outputs.get(segment.profile_tool_call_id or "")
    layers = output.get("layers") if isinstance(output, Mapping) else None
    if not isinstance(layers, list) or segment.profile_key is None:
        return ()
    for layer in layers:
        if (
            isinstance(layer, Mapping)
            and str(layer.get("key")) == segment.profile_key
            and layer.get("size") == segment.size
        ):
            return layer.get("features")
    return ()


def _column_of(feature: Mapping[str, Any]) -> str:
    """The column a feature is about (``プラン=無料`` is about ``プラン``)."""
    return str(feature.get("name", "")).split("=", 1)[0]


def settle_features(
    segments: Sequence[Segment],
    *,
    runs: Mapping[str, Optional[EmittedResult]],
    outputs: Mapping[str, Any],
    skip: Collection[str] = (),
) -> list[Segment]:
    """Give each segment the features that were counted (see the module doc).

    Args:
        segments: Segments from the analysis agent.
        runs: ``run_code`` call ID -> its recounted result (``None`` failed).
        outputs: Tool call ID -> structured tool output.
        skip: Columns confirmed as no trait (IDs, codes); no feature is made
            of them.
    """
    settled: list[Segment] = []
    for segment in segments:
        call = segment.tool_call_id
        if call is not None and call in runs:
            result = runs[call]
            key = segment.segment_key
            items: Any = ()
            if result is not None and key is not None:
                for entry in result.segments:
                    if str(entry.get("segment_key")) == key:
                        items = entry.get("features")
            features = _counted(items, skip)
        elif str(segment.definition.get("kind")) == "cluster":
            features = _counted(_cluster_features(segment, outputs), skip)
        else:
            features = _counted(_profiled(segment, outputs), skip)
        settled.append(segment.update(features=features))
    return settled
