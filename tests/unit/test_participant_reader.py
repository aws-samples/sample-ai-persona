"""ParticipantReader（参加者のペルソナの判定付きの読み）の単体テスト。

BR2.8（開始時に全員の can_read）、BR7.1〜BR7.3（実行中の確かめ直し、参加者の状態）、
NFR1.46、NFR1.47、NFR1.63（読み取りに届かなければ 503）、NFR2.16、NFR2.17
（参加者のまとめ読みは 1 回）。判定は本物の AccessPolicy を使う。
"""

from datetime import datetime
from typing import Sequence

import pytest

from src.managers.components.participant_reader import ParticipantReader
from src.managers.components.resource_lookup import build_resource_lookup
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.errors import CodedError, ErrorCode, LookupUnavailableError
from src.models.persona import Persona
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit

PERSONA = ResourceType.PERSONA
MEMBER_A = Principal(
    sub="a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
MEMBER_C = Principal(
    sub="c", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"plan"})
)
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))


class StubReader:
    """Decision columns of the personas (owner, shares)."""

    def __init__(
        self, targets: dict[str, AccessTarget], error: Exception | None = None
    ):
        self.targets = targets
        self.calls: list[list[str]] = []
        self.error = error

    def read(self, ids: Sequence[str], deadline: float) -> dict[str, AccessTarget]:
        self.calls.append(list(ids))
        if self.error is not None:
            raise self.error
        return {i: self.targets[i] for i in ids if i in self.targets}


class StubBodies:
    def __init__(self, personas: dict[str, Persona], error: Exception | None = None):
        self.personas = personas
        self.calls: list[list[str]] = []
        self.error = error

    def get_many(self, ids: list[str]) -> dict[str, Persona]:
        self.calls.append(list(ids))
        if self.error is not None:
            raise self.error
        return {i: self.personas[i] for i in ids if i in self.personas}


def _persona(pid: str, owner: str) -> Persona:
    now = datetime(2026, 10, 15)
    return Persona(
        id=pid,
        name=f"名前{pid}",
        age=30,
        occupation="職業",
        background="背景",
        values=[],
        pain_points=[],
        goals=[],
        created_at=now,
        updated_at=now,
        owner_sub=owner,
    )


class LookupFailed(CodedError):
    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("table unreachable")
        self.reason = "unreachable"


def _world(
    reader_error: Exception | None = None, bodies_error: Exception | None = None
):
    targets = {
        "own": AccessTarget(PERSONA, "own", owner_sub="a"),
        "shared": AccessTarget(
            PERSONA, "shared", owner_sub="b", shared_group_ids=frozenset({"sales"})
        ),
        "other": AccessTarget(PERSONA, "other", owner_sub="c"),
    }
    reader = StubReader(targets, reader_error)
    bodies = StubBodies(
        {pid: _persona(pid, t.owner_sub or "") for pid, t in targets.items()},
        bodies_error,
    )
    lookup = build_resource_lookup([(PERSONA, reader)], sample_rate=0.0)
    return ParticipantReader(lookup, bodies), reader, bodies


class TestReadForUse:
    def test_reads_the_bodies_after_every_participant_passed(self):
        participants, reader, bodies = _world()
        personas = participants.read_for_use(MEMBER_A, ["own", "shared"])
        assert [p.id for p in personas] == ["own", "shared"]
        assert len(reader.calls) == 1
        assert bodies.calls == [["own", "shared"]]

    def test_one_unreadable_participant_stops_everything(self):
        participants, _, bodies = _world()
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            participants.read_for_use(MEMBER_A, ["own", "other"])
        assert bodies.calls == []

    def test_deleted_and_unreadable_get_the_same_answer(self):
        participants, _, _ = _world()
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            participants.read_for_use(MEMBER_A, ["own", "gone"])

    def test_admin_reads_everyone(self):
        participants, _, _ = _world()
        assert len(participants.read_for_use(ADMIN, ["own", "other"])) == 2

    def test_lookup_failure_is_503_and_reads_no_body(self):
        participants, _, bodies = _world(reader_error=LookupFailed())
        with pytest.raises(LookupUnavailableError):
            participants.read_for_use(MEMBER_A, ["own"])
        assert bodies.calls == []

    def test_body_read_failure_is_503(self):
        participants, _, _ = _world(bodies_error=RuntimeError("boom"))
        with pytest.raises(LookupUnavailableError):
            participants.read_for_use(MEMBER_A, ["own"])

    def test_a_body_that_vanished_after_the_check_is_not_found(self):
        participants, _, bodies = _world()
        del bodies.personas["shared"]
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            participants.read_for_use(MEMBER_A, ["own", "shared"])


class TestRecheck:
    def test_ensure_all_readable_returns_the_targets(self):
        participants, reader, _ = _world()
        targets = participants.ensure_all_readable(MEMBER_A, ["own", "shared"])
        assert targets["shared"].owner_sub == "b"
        assert len(reader.calls) == 1

    def test_ensure_all_readable_raises_not_found(self):
        participants, _, _ = _world()
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            participants.ensure_all_readable(MEMBER_C, ["own"])

    def test_can_read_all_is_false_when_one_is_not_readable(self):
        participants, _, _ = _world()
        assert participants.can_read_all(MEMBER_A, ["own", "shared"])
        assert not participants.can_read_all(MEMBER_A, ["own", "other"])
        assert not participants.can_read_all(MEMBER_A, ["own", "gone"])


class TestStatuses:
    def test_readable_no_access_and_deleted(self):
        participants, reader, _ = _world()
        statuses = participants.statuses(MEMBER_A, ["own", "other", "gone"])
        assert statuses == {"own": "readable", "other": "no_access", "gone": "deleted"}
        assert len(reader.calls) == 1

    def test_no_participants_reads_nothing(self):
        participants, reader, _ = _world()
        assert participants.statuses(MEMBER_A, []) == {}
        assert reader.calls == []

    def test_lookup_failure_is_503(self):
        participants, _, _ = _world(reader_error=LookupFailed())
        with pytest.raises(LookupUnavailableError):
            participants.statuses(MEMBER_A, ["own"])


class TestStatusesWithBodies:
    """議論の詳細の参加者のカード: 読めるペルソナだけ本体を読む（BR7.3）。"""

    def test_reads_bodies_of_readable_participants_only(self):
        participants, reader, bodies = _world()
        statuses, personas = participants.statuses_with_bodies(
            MEMBER_A, ["own", "shared", "other", "gone"]
        )
        assert statuses == {
            "own": "readable",
            "shared": "readable",
            "other": "no_access",
            "gone": "deleted",
        }
        assert set(personas) == {"own", "shared"}
        assert len(reader.calls) == 1
        assert bodies.calls == [["own", "shared"]]

    def test_no_readable_participant_reads_no_body(self):
        participants, _, bodies = _world()
        statuses, personas = participants.statuses_with_bodies(
            MEMBER_C, ["own", "shared"]
        )
        assert statuses == {"own": "no_access", "shared": "no_access"}
        assert personas == {}
        assert bodies.calls == []

    def test_body_read_failure_keeps_the_statuses(self):
        participants, _, _ = _world(bodies_error=RuntimeError("boom"))
        statuses, personas = participants.statuses_with_bodies(MEMBER_A, ["own"])
        assert statuses == {"own": "readable"}
        assert personas == {}

    def test_lookup_failure_is_503(self):
        participants, _, bodies = _world(reader_error=LookupFailed())
        with pytest.raises(LookupUnavailableError):
            participants.statuses_with_bodies(MEMBER_A, ["own"])
        assert bodies.calls == []
