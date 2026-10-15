"""ResourceLookup（判定に要る項目の読み取り、約束 C3、ADR-011）の単体テスト。

BR2.10（登録制・失敗を許可に倒さない）、NFR1.3、NFR8.8、NFR8.9、NFR9.11、NFR9.12、
NFR9.18（呼び出しごとに 3 秒の締め切り）、NFR Design R-01（締め切りを読み方に渡す）、
Q2（500 件超は想定外）。時刻は注入した時計で進め、time.sleep は使わない。
"""

import json
from typing import Sequence

import pytest

from src.managers.components.resource_lookup import (
    LOOKUP_DEADLINE_SECONDS,
    ResourceLookup,
    build_resource_lookup,
)
from src.models.authz import AccessTarget, ResourceType
from src.models.errors import CodedError, ErrorCode, LookupUnavailableError
from tests.error_helpers import raises_code

PERSONA = ResourceType.PERSONA


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class StubReader:
    """A LookupReader that knows some IDs and records each call."""

    def __init__(
        self,
        known: set[str] | None = None,
        *,
        clock: FakeClock | None = None,
        seconds_per_read: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.known = known or set()
        self.calls: list[tuple[list[str], float]] = []
        self._clock = clock
        self._seconds = seconds_per_read
        self._error = error

    def read(self, ids: Sequence[str], deadline: float) -> dict[str, AccessTarget]:
        self.calls.append((list(ids), deadline))
        if self._clock is not None:
            self._clock.now += self._seconds
        if self._error is not None:
            raise self._error
        return {
            i: AccessTarget(PERSONA, i, owner_sub="owner-1")
            for i in ids
            if i in self.known
        }


class ReadFailed(CodedError):
    """What a Service-layer reader raises when the table is out of reach."""

    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _lookup(reader: StubReader, clock: FakeClock, **kwargs: object) -> ResourceLookup:
    kwargs.setdefault("sample_rate", 0.0)
    return build_resource_lookup([(PERSONA, reader)], clock=clock, **kwargs)  # type: ignore[arg-type]


def _events(capsys: pytest.CaptureFixture[str], name: str) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return [line for line in lines if line.get("event") == name]


class TestRegisteredReaders:
    def test_get_reads_one_id_with_a_three_second_deadline(self):
        clock = FakeClock()
        reader = StubReader({"p-1"})

        target = _lookup(reader, clock).get(PERSONA, "p-1")

        assert target.owner_sub == "owner-1"
        assert reader.calls == [(["p-1"], clock.now + LOOKUP_DEADLINE_SECONDS)]
        assert LOOKUP_DEADLINE_SECONDS == 3.0

    def test_missing_id_does_not_exist(self):
        target = _lookup(StubReader(), FakeClock()).get(PERSONA, "gone")
        assert target == AccessTarget.missing(PERSONA, "gone")

    def test_get_many_returns_every_requested_id(self):
        reader = StubReader({"p-1"})
        result = _lookup(reader, FakeClock()).get_many(PERSONA, ["p-1", "p-2", "p-1"])
        assert set(result) == {"p-1", "p-2"}
        assert result["p-1"].exists and not result["p-2"].exists
        assert reader.calls[0][0] == ["p-1", "p-2"]

    def test_no_ids_reads_nothing(self):
        reader = StubReader()
        assert _lookup(reader, FakeClock()).get_many(PERSONA, []) == {}
        assert reader.calls == []

    def test_ids_are_read_a_hundred_at_a_time(self):
        reader = StubReader()
        ids = [f"p-{i}" for i in range(250)]

        result = _lookup(reader, FakeClock()).get_many(PERSONA, ids)

        assert [len(call[0]) for call in reader.calls] == [100, 100, 50]
        assert len(result) == 250


class TestRegistration:
    def test_unregistered_type_fails_closed(self, capsys):
        lookup = _lookup(StubReader(), FakeClock())
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            lookup.get(ResourceType.SURVEY, "s-1")
        (line,) = _events(capsys, "authz.lookup_failed")
        assert line == {
            "event": "authz.lookup_failed",
            "ts": line["ts"],
            "resource_type": "survey",
            "reason": "unregistered",
        }

    def test_empty_lookup_fails_closed(self):
        with pytest.raises(LookupUnavailableError):
            ResourceLookup({}).get(PERSONA, "p-1")

    def test_a_type_cannot_be_registered_twice(self):
        with pytest.raises(ValueError):
            build_resource_lookup([(PERSONA, StubReader()), (PERSONA, StubReader())])


class TestLimits:
    def test_more_than_five_hundred_ids_is_a_programming_error(self):
        reader = StubReader()
        with pytest.raises(ValueError):
            _lookup(reader, FakeClock()).get_many(
                PERSONA, [f"p-{i}" for i in range(501)]
            )
        assert reader.calls == []

    def test_slow_reads_stop_at_the_deadline(self, capsys):
        """1 回の読み取りが 1 秒かかるとき、3 秒 + 0.2 秒以内に 503（NFR9.18）。"""
        clock = FakeClock()
        started = clock.now
        reader = StubReader(clock=clock, seconds_per_read=1.0)

        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            _lookup(reader, clock).get_many(PERSONA, [f"p-{i}" for i in range(500)])

        assert clock.now - started <= LOOKUP_DEADLINE_SECONDS + 0.2
        assert len(reader.calls) == 3
        assert {call[1] for call in reader.calls} == {started + LOOKUP_DEADLINE_SECONDS}
        (line,) = _events(capsys, "authz.lookup_failed")
        assert line["reason"] == "deadline"

    @pytest.mark.parametrize("reason", ["unprocessed", "deadline", "unreachable"])
    def test_reader_failures_become_503_with_their_reason(self, capsys, reason):
        reader = StubReader(error=ReadFailed("table down", reason=reason))
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            _lookup(reader, FakeClock()).get(PERSONA, "p-1")
        (line,) = _events(capsys, "authz.lookup_failed")
        assert line["reason"] == reason

    def test_reader_failure_without_reason_is_unreachable(self, capsys):
        reader = StubReader(
            error=CodedError("boom", code=ErrorCode.DATABASE_OPERATION_FAILED)
        )
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            _lookup(reader, FakeClock()).get(PERSONA, "p-1")
        (line,) = _events(capsys, "authz.lookup_failed")
        assert line["reason"] == "unreachable"


class TestTimingLog:
    def test_every_call_is_logged_at_rate_one(self, capsys):
        clock = FakeClock()
        reader = StubReader({"p-1"}, clock=clock, seconds_per_read=0.012)
        lookup = _lookup(reader, clock, sample_rate=1.0)

        lookup.get_many(PERSONA, ["p-1", "p-secret"])
        lookup.get(PERSONA, "p-1")

        lines = _events(capsys, "authz.lookup")
        assert len(lines) == 2
        assert lines[0]["resource_type"] == "persona"
        assert lines[0]["count"] == 2
        assert lines[0]["duration_ms"] == pytest.approx(12.0)
        assert "p-secret" not in json.dumps(lines)

    def test_default_rate_logs_about_one_in_ten(self, capsys):
        draws = iter([0.05] + [0.5] * 9)
        lookup = _lookup(
            StubReader(),
            FakeClock(),
            sample_rate=0.1,
            random_source=lambda: next(draws),
        )
        for _ in range(10):
            lookup.get(PERSONA, "p-1")
        assert len(_events(capsys, "authz.lookup")) == 1

    def test_rate_zero_logs_nothing(self, capsys):
        _lookup(StubReader(), FakeClock(), sample_rate=0.0).get(PERSONA, "p-1")
        assert _events(capsys, "authz.lookup") == []


class TestDiscussionRegistration:
    """U6: 議論を ResourceLookup に登録する（表示名は topic、約束 C3・C8）。"""

    def test_discussions_are_registered_with_their_topic(self):
        from src.managers.components.sharing_runtime import lookup_tables

        tables = {t.resource_type: t for t in lookup_tables()}
        discussion = tables[ResourceType.DISCUSSION]
        assert discussion.display_name_attribute == "topic"
        assert discussion.table_name.endswith("Discussions")

    def test_old_discussion_without_owner_reads_as_ownerless(self, monkeypatch):
        moto = pytest.importorskip("moto")
        import boto3

        from src.managers.components.sharing_runtime import lookup_tables
        from src.services.authz_lookup_service import AuthzLookupService

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        table = next(
            t for t in lookup_tables() if t.resource_type is ResourceType.DISCUSSION
        )
        with moto.mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            client.create_table(
                TableName=table.table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            client.put_item(
                TableName=table.table_name,
                Item={"id": {"S": "old"}, "topic": {"S": "古い議論"}},
            )
            client.put_item(
                TableName=table.table_name,
                Item={
                    "id": {"S": "new"},
                    "topic": {"S": "新しい議論"},
                    "owner_sub": {"S": "a"},
                    "shared_group_ids": {"L": [{"S": "sales"}]},
                },
            )
            service = AuthzLookupService(
                region="us-east-1", jobs_table_name="Jobs", client=client
            )
            lookup = build_resource_lookup(
                [(ResourceType.DISCUSSION, service.reader(table))], sample_rate=0.0
            )
            found = lookup.get_many(ResourceType.DISCUSSION, ["old", "new", "gone"])
        assert found["old"].owner_sub is None
        assert found["old"].display_name == "古い議論"
        assert found["new"].shared_group_ids == frozenset({"sales"})
        assert found["gone"].exists is False
