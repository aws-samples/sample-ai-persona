"""Features of a segment come from what the library counted (U6 addendum)."""

from __future__ import annotations

from typing import Any

import pytest

from src.managers.advanced_generation.segment_features import (
    MAX_FEATURES,
    MIN_DIFFERENCE,
    MIN_EFFECT,
    settle_features,
)
from src.models.analysis import EmittedResult, Segment


def _segment(
    *,
    call: str = "call-1",
    kind: str = "filter",
    features: tuple[dict[str, Any], ...] = (),
    **spec: Any,
) -> Segment:
    return Segment(
        id="seg-1",
        label="優良",
        definition={"kind": kind, "spec": {"tool_call_id": call, **spec}},
        size=10,
        share=0.5,
        features=features,
        evidence_ids=("ev-1",),
    )


def _mean(name: str, value: float, baseline: float) -> dict[str, Any]:
    return {"name": name, "kind": "mean", "value": value, "baseline": baseline}


def _emitted(features: list[dict[str, Any]]) -> EmittedResult:
    return EmittedResult(
        population=20,
        population_source="source_1",
        segments=({"segment_key": "a", "size": 10, "features": features},),
    )


class TestRunCodeSegments:
    def test_the_library_features_replace_what_the_ai_wrote(self) -> None:
        segment = _segment(
            segment_key="a",
            features=({"name": "年齢", "value": "43.7歳", "baseline": "他の層は41歳"},),
        )
        runs = {"call-1": _emitted([_mean("年齢", 47.0, 41.9)])}

        [settled] = settle_features([segment], runs=runs, outputs={})

        assert [f["name"] for f in settled.features] == ["年齢"]
        assert settled.features[0]["value"] == 47.0
        assert settled.features[0]["baseline"] == 41.9

    def test_a_segment_without_library_features_has_none(self) -> None:
        segment = _segment(
            segment_key="a",
            features=({"name": "年齢", "value": "1", "baseline": "2"},),
        )

        [settled] = settle_features(
            [segment], runs={"call-1": _emitted([])}, outputs={}
        )

        assert settled.features == ()

    def test_a_metric_the_code_computed_is_kept_with_its_kind(self) -> None:
        derived = {
            "name": "recency",
            "kind": "derived",
            "value": 12.0,
            "baseline": 45.0,
        }
        runs = {"call-1": _emitted([derived])}

        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})

        assert settled.features[0]["kind"] == "derived"

    def test_features_that_are_not_numbers_are_dropped(self) -> None:
        bad = {"name": "x", "kind": "mean", "value": "高い", "baseline": 1.0}
        runs = {"call-1": _emitted([bad, _mean("年齢", 40.0, 35.0)])}

        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})

        assert [f["name"] for f in settled.features] == ["年齢"]

    def test_metrics_keep_their_places_among_many_columns(self) -> None:
        columns = [_mean(f"c{i}", 200.0 + i, 100.0) for i in range(10)]
        metrics = [
            {"name": f"m{i}", "kind": "derived", "value": 130.0, "baseline": 100.0}
            for i in range(3)
        ]
        runs = {"call-1": _emitted([*columns, *metrics])}

        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})

        kinds = [f["kind"] for f in settled.features]
        assert len(kinds) == MAX_FEATURES and kinds.count("derived") == 3

    def test_a_feature_close_to_the_whole_is_left_out(self) -> None:
        close = _mean("近い", 100.0 * (1 + MIN_DIFFERENCE / 2), 100.0)
        far = _mean("遠い", 120.0, 100.0)
        same_zero = _mean("ゼロ", 0.0, 0.0)
        runs = {"call-1": _emitted([close, far, same_zero])}

        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})

        assert [f["name"] for f in settled.features] == ["遠い"]

    def test_the_biggest_differences_come_first_and_only_a_few_stay(self) -> None:
        many = [_mean(f"c{i}", 100.0 + i, 100.0) for i in range(10)]
        many.append(_mean("big", 300.0, 100.0))
        runs = {"call-1": _emitted(many)}

        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})

        assert len(settled.features) == MAX_FEATURES
        assert settled.features[0]["name"] == "big"


class TestClusterSegments:
    def test_the_cluster_features_are_used(self) -> None:
        output = {
            "clusters": [
                {"index": 0, "features": [_mean("age", 30.0, 40.0)]},
                {"index": 1, "features": [_mean("age", 50.0, 40.0)]},
            ]
        }
        segment = _segment(kind="cluster", cluster_index=1)

        [settled] = settle_features([segment], runs={}, outputs={"call-1": output})

        assert settled.features[0]["value"] == 50.0


class TestFixedToolSegments:
    @staticmethod
    def _profiled(size: int = 10, key: str = "gold") -> dict[str, Any]:
        return {
            "population": 60,
            "layers": [
                {
                    "key": key,
                    "size": size,
                    "share": size / 60,
                    "features": [_mean_s("年齢", 47.0, 40.0, 8.0)],
                },
                {
                    "key": "other",
                    "size": 50,
                    "share": 50 / 60,
                    "features": [_mean_s("年齢", 30.0, 40.0, 8.0)],
                },
            ],
        }

    def _segment_of(self, **spec: Any) -> Segment:
        return _segment(call="call-1", **spec)

    def test_what_the_agent_wrote_is_not_used(self) -> None:
        # both numbers are in the output, yet nothing ties them to a column,
        # a condition or this segment
        output = {
            "columns": ["plan", "avg_age"],
            "rows": [["gold", 43.7], ["all", 41.9]],
        }
        segment = _segment(
            features=({"name": "年齢", "value": 43.7, "baseline": 41.9},)
        )

        [settled] = settle_features([segment], runs={}, outputs={"call-1": output})

        assert settled.features == ()

    def test_the_counted_layer_of_the_same_size_gives_the_features(self) -> None:
        segment = self._segment_of(profile_tool_call_id="call-2", profile_key="gold")

        [settled] = settle_features(
            [segment], runs={}, outputs={"call-2": self._profiled(size=10)}
        )

        assert [f["name"] for f in settled.features] == ["年齢"]
        assert settled.features[0]["value"] == 47.0

    def test_a_layer_of_another_size_is_not_the_same_rows(self) -> None:
        segment = self._segment_of(profile_tool_call_id="call-2", profile_key="gold")

        [settled] = settle_features(
            [segment], runs={}, outputs={"call-2": self._profiled(size=9)}
        )

        assert settled.features == ()

    @pytest.mark.parametrize(
        "spec",
        [
            {"profile_tool_call_id": "call-9", "profile_key": "gold"},  # no such call
            {"profile_tool_call_id": "call-2", "profile_key": "nope"},  # no such layer
            {"profile_tool_call_id": "call-2"},  # no key
            {"profile_key": "gold"},  # no call
        ],
    )
    def test_a_layer_that_cannot_be_found_gives_nothing(
        self, spec: dict[str, Any]
    ) -> None:
        segment = self._segment_of(**spec)

        [settled] = settle_features(
            [segment], runs={}, outputs={"call-2": self._profiled()}
        )

        assert settled.features == ()

    def test_the_output_of_a_failed_or_odd_call_gives_nothing(self) -> None:
        segment = self._segment_of(profile_tool_call_id="call-2", profile_key="gold")

        for output in (None, "text", {"layers": "x"}, {"layers": [None, 3]}):
            [settled] = settle_features([segment], runs={}, outputs={"call-2": output})
            assert settled.features == ()

    def test_small_differences_are_still_left_out(self) -> None:
        output = self._profiled()
        output["layers"][0]["features"] = [_mean_s("年齢", 40.1, 40.0, 8.0)]
        segment = self._segment_of(profile_tool_call_id="call-2", profile_key="gold")

        [settled] = settle_features([segment], runs={}, outputs={"call-2": output})

        assert settled.features == ()


def _mean_s(name: str, value: float, baseline: float, spread: float) -> dict[str, Any]:
    return {**_mean(name, value, baseline), "spread": spread}


def _share(name: str, value: float, baseline: float) -> dict[str, Any]:
    return {"name": name, "kind": "share", "value": value, "baseline": baseline}


class TestDifferenceIsMeasuredInSpreads:
    def _settled(self, features: list[dict[str, Any]]) -> list[str]:
        runs = {"call-1": _emitted(features)}
        [settled] = settle_features([_segment(segment_key="a")], runs=runs, outputs={})
        return [f["name"] for f in settled.features]

    def test_a_mean_is_a_trait_by_its_distance_in_standard_deviations(self) -> None:
        small = _mean_s("小さい", 100.5, 100.0, 10.0)  # 0.05 sd
        large = _mean_s("大きい", 106.0, 100.0, 10.0)  # 0.6 sd
        assert MIN_EFFECT > 0.05

        assert self._settled([small, large]) == ["大きい"]

    def test_a_whole_near_zero_does_not_inflate_a_difference(self) -> None:
        # relative to a whole of 0 this would be infinite; in spreads it is 0.05
        near_zero = _mean_s("損益", 0.5, 0.0, 10.0)

        assert self._settled([near_zero]) == []

    def test_a_big_unit_does_not_hide_a_difference(self) -> None:
        # 5% of 100,000 is nothing in relative terms, but it is 1 sd here
        money = _mean_s("金額", 105_000.0, 100_000.0, 5_000.0)

        assert self._settled([money]) == ["金額"]

    def test_a_share_is_measured_in_proportions_not_in_ratios(self) -> None:
        tiny_ratio = _share("まれ", 0.02, 0.01)  # twice as big, one point apart
        real = _share("多い", 0.48, 0.33)

        assert self._settled([tiny_ratio, real]) == ["多い"]

    def test_the_biggest_effect_comes_first_across_kinds(self) -> None:
        names = self._settled(
            [_mean_s("平均", 112.0, 100.0, 10.0), _share("割合", 0.9, 0.2)]
        )

        assert names == ["割合", "平均"]


class TestSkippedColumns:
    def test_a_column_confirmed_as_no_trait_makes_no_feature(self) -> None:
        segment = _segment(segment_key="a")
        runs = {
            "call-1": _emitted(
                [
                    _mean("年齢", 47.0, 41.9),
                    {
                        "name": "郵便番号=100",
                        "kind": "share",
                        "value": 0.9,
                        "baseline": 0.1,
                    },
                    _mean("顧客番号", 900.0, 100.0),
                ]
            )
        }

        [settled] = settle_features(
            [segment], runs=runs, outputs={}, skip={"郵便番号", "顧客番号"}
        )

        assert [f["name"] for f in settled.features] == ["年齢"]


class TestMalformedSpec:
    def test_a_cluster_segment_without_a_spec_has_no_features_and_no_crash(
        self,
    ) -> None:
        segment = Segment(
            id="seg-1",
            label="a",
            definition={"kind": "cluster", "spec": None},
            size=10,
            share=0.5,
            features=(),
            evidence_ids=("ev-1",),
        )

        [settled] = settle_features([segment], runs={}, outputs={})

        assert settled.features == ()
