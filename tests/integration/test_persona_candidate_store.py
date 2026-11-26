"""Integration tests for PersonaCandidateStore (reading and writing generated candidates), using moto.

Covers writing candidates in bulk, the claim condition, item size, cleanup only when
the marker matches, strongly consistent reads, and only one of two claims winning.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.dataset import DatasetColumn  # noqa: E402
from src.models.persona import Persona  # noqa: E402
from src.models.persona_candidate import (  # noqa: E402
    MAX_CANDIDATE_ITEM_BYTES,
    BehaviorDatasetCandidate,
    PersonaCandidate,
)
from src.services.stores.persona_candidate_store import (  # noqa: E402
    CandidateUnavailable,
    PersonaCandidateStore,
    PersonaCandidateStoreError,
)

REGION = "ap-northeast-1"
TABLE = "PersonaCandidatesTest"
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "candidate_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "candidate_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> PersonaCandidateStore:
    return PersonaCandidateStore(region=REGION, table_name=TABLE, client=client)


def _candidate(
    owner: str = "sub-a", ttl: int = 3600, **kwargs: Any
) -> PersonaCandidate:
    persona = Persona.create_new(
        name="佐藤 花子",
        age=34,
        occupation="会社員",
        background="東京で働く",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
    )
    return PersonaCandidate.from_persona(
        persona,
        owner_sub=owner,
        generation_id="gen-1",
        ttl_seconds=ttl,
        now=NOW,
        generation_log=[{"type": "thinking", "content": "考え中"}],
        generation_context={"data_type": "interview", "persona_count": 1},
        **kwargs,
    )


class TestWrite:
    def test_put_all_writes_every_candidate_with_the_ttl_attribute(self, store, client):
        candidates = [_candidate() for _ in range(30)]  # more than one batch
        store.put_all(candidates)

        for c in candidates:
            item = client.get_item(
                TableName=TABLE, Key={"candidate_id": {"S": c.candidate_id}}
            )["Item"]
            assert item["expires_at"] == {"N": str(c.expires_at)}
            assert item["owner_sub"] == {"S": "sub-a"}

    def test_get_is_a_strong_read_and_round_trips(self, store, client):
        dataset = BehaviorDatasetCandidate(
            temp_id="t1",
            name="佐藤_購買",
            data_type_label="購買",
            temp_s3_key="candidates/sub-a/c/t1.csv",
            columns=(DatasetColumn(name="user_id", data_type="string"),),
            row_count=3,
        )
        candidate = _candidate(behavior_datasets=(dataset,))
        store.put_all([candidate])

        seen: list[dict[str, Any]] = []
        original = client.get_item

        def spy(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return original(**kwargs)

        client.get_item = spy
        assert store.get(candidate.candidate_id) == candidate
        assert seen[0]["ConsistentRead"] is True
        assert store.get("missing") is None

    def test_item_size_counts_the_serialized_item(self):
        small = _candidate()
        big = PersonaCandidate.from_persona(
            Persona.create_new(
                name="n",
                age=1,
                occupation="o",
                background="b",
                values=["v"],
                pain_points=["p"],
                goals=["g"],
            ),
            owner_sub="s",
            generation_id="g",
            ttl_seconds=1,
            now=NOW,
            generation_log=[{"type": "thinking", "content": "あ" * 130_000}],
        )
        assert 0 < PersonaCandidateStore.item_size(small) < 2_000
        # 3 bytes per character (UTF-8), so it exceeds 390,000 bytes
        assert PersonaCandidateStore.item_size(big) > MAX_CANDIDATE_ITEM_BYTES


def _sized(candidate: PersonaCandidate, size: int) -> PersonaCandidate:
    """The candidate with its generation log padded to exactly ``size`` bytes."""
    from dataclasses import replace

    def with_log(text: str) -> PersonaCandidate:
        return replace(
            candidate, generation_log=[{"type": "thinking", "content": text}]
        )

    base = PersonaCandidateStore.item_size(with_log(""))
    padded = with_log("x" * (size - base))  # ASCII is 1 byte per character
    assert PersonaCandidateStore.item_size(padded) == size
    return padded


def _count(client: Any) -> int:
    return client.scan(TableName=TABLE, Select="COUNT")["Count"]


class TestSizeBoundary:
    """A serialized size of exactly 358,400 bytes is written; one byte more is not."""

    def test_a_candidate_of_exactly_the_limit_is_written(self, store, client):
        candidate = _sized(_candidate(), MAX_CANDIDATE_ITEM_BYTES)
        assert MAX_CANDIDATE_ITEM_BYTES == 358_400

        store.put_all([candidate])

        assert store.get(candidate.candidate_id) == candidate
        assert _count(client) == 1

    @pytest.mark.parametrize(("extra", "written"), [(0, 1), (1, 0)])
    def test_generation_writes_at_the_limit_and_nothing_one_byte_over(
        self, store, client, monkeypatch, extra, written
    ):
        from unittest.mock import Mock

        from src.managers import persona_generation_manager as mod
        from src.managers.persona_generation_manager import (
            PersonaGenerationManager,
            PersonaGenerationManagerError,
        )
        from src.models.errors import ErrorCode
        from tests.conftest import MEMBER_A
        from tests.error_helpers import raises_code

        # Make a generated candidate exactly the limit + extra bytes (production code is unchanged)
        original = PersonaCandidate.from_persona
        monkeypatch.setattr(
            PersonaCandidate,
            "from_persona",
            lambda *a, **k: _sized(original(*a, **k), MAX_CANDIDATE_ITEM_BYTES + extra),
        )
        agent = Mock()
        agent.run_persona_generation.return_value = (
            mod._PersonaListOutput(
                personas=[
                    mod._PersonaOutput(
                        name="田中",
                        age=30,
                        occupation="会社員",
                        background="背景",
                        values=["v"],
                        pain_points=["p"],
                        goals=["g"],
                    )
                ]
            ),
            [{"type": "thinking", "content": "考えた"}],
        )
        manager = PersonaGenerationManager(
            agent_service=agent,
            dataset_analysis_service=Mock(),
            candidate_store=store,
            s3_service=Mock(),
            clock=lambda: NOW,
        )

        def generate() -> Any:
            return manager.generate(
                MEMBER_A.principal(),
                file_contents=[
                    ("十分な長さのインタビュー本文です。".encode(), "a.txt")
                ],
                data_type="interview",
                persona_count=1,
            )

        if written:
            result = generate()
            stored = store.get(result.personas[0].id)
            assert stored is not None
            assert PersonaCandidateStore.item_size(stored) == MAX_CANDIDATE_ITEM_BYTES
        else:
            with raises_code(
                PersonaGenerationManagerError, ErrorCode.GENERATION_RESULT_TOO_LARGE
            ):
                generate()
        assert _count(client) == written


class TestClaim:
    def test_owner_claims_before_expiry(self, store):
        candidate = _candidate()
        store.put_all([candidate])

        claimed = store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        assert claimed.claim_token == "tok"
        assert claimed.claimed_at == NOW
        assert store.get(candidate.candidate_id).claim_token == "tok"

    @pytest.mark.parametrize(
        ("owner", "at"),
        [
            ("sub-b", NOW),  # someone else's
            ("sub-a", NOW + timedelta(hours=1)),  # exactly at expiry
            ("sub-a", NOW + timedelta(hours=2)),  # expired
        ],
    )
    def test_other_users_and_expired_candidates_are_unavailable(self, store, owner, at):
        candidate = _candidate()
        store.put_all([candidate])

        with pytest.raises(CandidateUnavailable):
            store.claim(candidate.candidate_id, owner, "tok", at)
        assert store.get(candidate.candidate_id).claim_token is None

    def test_missing_candidate_is_unavailable(self, store):
        with pytest.raises(CandidateUnavailable):
            store.claim("missing", "sub-a", "tok", NOW)

    def test_a_second_claim_waits_for_five_minutes(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "first", NOW)

        with pytest.raises(CandidateUnavailable):
            store.claim(
                candidate.candidate_id, "sub-a", "second", NOW + timedelta(minutes=4)
            )
        stale = store.claim(
            candidate.candidate_id, "sub-a", "second", NOW + timedelta(minutes=6)
        )
        assert stale.claim_token == "second"

    def test_saved_candidate_cannot_be_claimed_again(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)
        assert store.mark_saved(candidate.candidate_id, "tok", NOW)

        with pytest.raises(CandidateUnavailable):
            store.claim(
                candidate.candidate_id, "sub-a", "tok2", NOW + timedelta(minutes=10)
            )


class TestCleanup:
    def test_release_needs_the_same_token(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        assert not store.release(candidate.candidate_id, "other")
        assert store.get(candidate.candidate_id).claim_token == "tok"
        assert store.release(candidate.candidate_id, "tok")
        assert store.get(candidate.candidate_id).claim_token is None
        # released: the owner can claim again at once
        store.claim(candidate.candidate_id, "sub-a", "again", NOW)

    def test_delete_needs_the_same_token(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        assert not store.delete(candidate.candidate_id, "other")
        assert store.get(candidate.candidate_id) is not None
        assert store.delete(candidate.candidate_id, "tok")
        assert store.get(candidate.candidate_id) is None

    def test_mark_saved_needs_the_same_token(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        assert not store.mark_saved(candidate.candidate_id, "other", NOW)
        assert store.get(candidate.candidate_id).saved_at is None


class TestConfirmBeforeSave:
    """Each candidate's hold is checked right before it is saved."""

    def test_the_holder_confirms_and_renews_the_hold(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        later = NOW + timedelta(minutes=4)
        assert store.confirm_claim(candidate.candidate_id, "tok", later)
        # renewed: another request still cannot take it 4 minutes after that
        assert store.get(candidate.candidate_id).claimed_at == later
        with pytest.raises(CandidateUnavailable):
            store.claim(
                candidate.candidate_id, "sub-a", "second", later + timedelta(minutes=4)
            )

    def test_a_taken_over_hold_is_not_confirmed(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "first", NOW)
        store.claim(
            candidate.candidate_id, "sub-a", "second", NOW + timedelta(minutes=6)
        )

        assert not store.confirm_claim(
            candidate.candidate_id, "first", NOW + timedelta(minutes=7)
        )
        assert store.get(candidate.candidate_id).claim_token == "second"

    def test_a_saved_candidate_is_not_confirmed(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)
        assert store.mark_saved(candidate.candidate_id, "tok", NOW)

        assert not store.confirm_claim(candidate.candidate_id, "tok", NOW)

    def test_a_missing_candidate_is_not_confirmed(self, store):
        assert not store.confirm_claim("missing", "tok", NOW)

    def test_mark_saved_needs_the_hold_and_no_earlier_save(self, store):
        candidate = _candidate()
        store.put_all([candidate])
        store.claim(candidate.candidate_id, "sub-a", "tok", NOW)

        assert store.mark_saved(candidate.candidate_id, "tok", NOW)
        later = NOW + timedelta(minutes=1)
        assert not store.mark_saved(candidate.candidate_id, "tok", later)
        assert store.get(candidate.candidate_id).saved_at == NOW


class TestConcurrentClaims:
    """When two hosts claim the same candidate at once, exactly one wins."""

    def test_only_one_of_two_simultaneous_claims_wins(self, store, client, monkeypatch):
        import threading

        from moto.dynamodb.models import DynamoDBBackend

        # Real DynamoDB applies conditional writes to one item one at a time, but with moto's
        # update_item another thread can slip in between the condition check and the write.
        # Serialize per item like the real thing so only the condition decides the winner (no waiting on time)
        serial = threading.Lock()
        original_update = DynamoDBBackend.update_item

        def atomic_update(self: Any, *args: Any, **kwargs: Any) -> Any:
            with serial:
                return original_update(self, *args, **kwargs)

        monkeypatch.setattr(DynamoDBBackend, "update_item", atomic_update)

        candidate = _candidate()
        store.put_all([candidate])
        # Each host has its own Store and client (same moto table); no in-memory state is shared
        hosts = [
            PersonaCandidateStore(
                region=REGION,
                table_name=TABLE,
                client=boto3.client("dynamodb", region_name=REGION),
            )
            for _ in range(2)
        ]
        start = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def claim(host: PersonaCandidateStore, token: str) -> None:
            start.wait()
            try:
                host.claim(candidate.candidate_id, "sub-a", token, NOW)
                outcomes[token] = "claimed"
            except CandidateUnavailable:
                outcomes[token] = "unavailable"
            except Exception as e:  # noqa: BLE001 - unexpected failures are recorded as outcomes too
                outcomes[token] = e

        threads = [
            threading.Thread(target=claim, args=(host, f"tok-{i}"))
            for i, host in enumerate(hosts)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(map(str, outcomes.values())) == ["claimed", "unavailable"]
        (winner,) = [tok for tok, result in outcomes.items() if result == "claimed"]
        assert store.get(candidate.candidate_id).claim_token == winner


class TestFailures:
    def test_other_dynamodb_failures_are_store_errors(self, client):
        store = PersonaCandidateStore(
            region=REGION, table_name="NoSuchTable", client=client
        )
        with pytest.raises(PersonaCandidateStoreError) as exc_info:
            store.get("x")
        assert not isinstance(exc_info.value, CandidateUnavailable)
        with pytest.raises(PersonaCandidateStoreError):
            store.put_all([_candidate()])
