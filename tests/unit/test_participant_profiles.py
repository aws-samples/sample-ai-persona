"""Tests for the participant profiles attached to insights."""

from src.managers.shared.participant_profiles import (
    attach_profile_to_quote,
    build_participant_profiles,
)
from src.models.persona import Persona


def _persona(name="田村 美咲", **kwargs) -> Persona:
    fields = dict(
        name=name,
        age=38,
        occupation="パート勤務（事務）・2児の母",
        background="郊外の住宅地に住む",
        values=["家計を守ること", "品質が同じならPB", "割引は賢く使う", "安心感"],
        pain_points=["育児用品の出費", "PBの品質への不安", "割引のない日"],
        goals=["出費を抑える"],
        gender="female",
    )
    fields.update(kwargs)
    return Persona.create_new(**fields)


def _advanced(segment_id="s1", unverified=()):
    persona = _persona()
    context = {
        "mode": "advanced",
        "segments": [
            {"id": "s1", "label": "PB・値引き反応の高いまとめ買い層", "share": 0.48},
            {"id": "s2", "label": "夕夜・QR決済の層", "share": 0.19},
        ],
        "segment_id": segment_id,
        "unverified_segment_ids": list(unverified),
    }
    from dataclasses import replace

    return replace(persona, generation_context=context)


class TestBuildParticipantProfiles:
    def test_label_has_age_gender_and_occupation(self):
        (profile,) = build_participant_profiles([_persona()])
        assert profile.name == "田村 美咲"
        assert profile.label == "38歳・女性・パート勤務"

    def test_advanced_persona_carries_its_segment_and_share(self):
        (profile,) = build_participant_profiles([_advanced()])
        assert profile.label == (
            "38歳・女性・パート勤務／PB・値引き反応の高いまとめ買い層（全体の48%）"
        )

    def test_unverified_segment_has_no_share(self):
        (profile,) = build_participant_profiles([_advanced(unverified=["s1"])])
        assert profile.label.endswith("／PB・値引き反応の高いまとめ買い層")

    def test_prompt_line_adds_values_and_pain_points(self):
        (profile,) = build_participant_profiles([_persona()])
        assert profile.prompt_line == (
            "- 田村 美咲: 38歳・女性・パート勤務"
            "／価値観: 家計を守ること、品質が同じならPB、割引は賢く使う"
            "／課題: 育児用品の出費、PBの品質への不安"
        )

    def test_long_occupation_is_shortened(self):
        (profile,) = build_participant_profiles(
            [
                _persona(
                    occupation="地元商店の経営者とても長い説明がずっと続いていく職業の書き方"
                )
            ]
        )
        assert len(profile.label.split("・")[2]) <= 20


class TestAttachProfileToQuote:
    def _profiles(self):
        return build_participant_profiles(
            [
                _persona(),
                _persona(name="岡田 拓海", age=29, gender="male", occupation="営業職"),
            ]
        )

    def test_full_name(self):
        assert attach_profile_to_quote("田村 美咲: 高いです", self._profiles()) == (
            "田村 美咲（38歳・女性・パート勤務）: 高いです"
        )

    def test_name_variants(self):
        profiles = self._profiles()
        for quote in (
            "田村美咲：高いです",
            "田村さん: 高いです",
            "田村 美咲（主婦）: 高いです",
        ):
            assert attach_profile_to_quote(quote, profiles) == (
                "田村 美咲（38歳・女性・パート勤務）: 高いです"
            )

    def test_unknown_speaker_is_left_as_is(self):
        assert (
            attach_profile_to_quote("佐藤: 高いです", self._profiles())
            == "佐藤: 高いです"
        )
        assert attach_profile_to_quote("高いです", self._profiles()) == "高いです"

    def test_shared_family_name_is_not_guessed(self):
        profiles = build_participant_profiles(
            [_persona(), _persona(name="田村 孝志", age=58, gender="male")]
        )
        assert (
            attach_profile_to_quote("田村さん: 高いです", profiles)
            == "田村さん: 高いです"
        )
