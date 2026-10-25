"""PersonaGenerationManager の生成候補（U5、ADR-008）の単体テスト。

BR1.1〜BR1.3、BR1.6〜BR1.9、NFR1.22、NFR1.36、NFR2.12、NFR2.13、NFR8.16、NFR8.17、
NFR9.27、NFR9.28、AC2.3.1、AC2.3.2。Store と S3 はモック（候補の Store は辞書の偽物）。
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from src.managers import persona_generation_manager as mod
from src.managers.persona_generation_manager import (
    PersonaGenerationManager,
    PersonaGenerationManagerError,
)
from src.models.errors import AccessDeniedError, ErrorCode
from src.models.persona_candidate import (
    CandidateClaim,
    CandidateSaveResult,
    PersonaCandidate,
)
from src.services.stores.persona_candidate_store import (
    CandidateUnavailable,
    PersonaCandidateStore,
    PersonaCandidateStoreError,
)
from tests.conftest import MEMBER_A, MEMBER_B, NO_ROLE
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture

NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
A = MEMBER_A.principal()
B = MEMBER_B.principal()


class FakeCandidateStore:
    """Dictionary-backed PersonaCandidateStore with the same conditions."""

    def __init__(self) -> None:
        self.items: dict[str, PersonaCandidate] = {}
        self.fail_claim_on: set[str] = set()
        self.fail_delete = False
        self.fail_release = False
        self.calls: list[str] = []

    def put_all(self, candidates: list[PersonaCandidate]) -> None:
        self.calls.append("put_all")
        for c in candidates:
            self.items[c.candidate_id] = c

    def claim(
        self, candidate_id: str, owner_sub: str, token: str, now: datetime
    ) -> PersonaCandidate:
        if candidate_id in self.fail_claim_on:
            raise PersonaCandidateStoreError("down")
        c = self.items.get(candidate_id)
        if (
            c is None
            or c.owner_sub != owner_sub
            or c.is_expired(now)
            or c.saved_at is not None
            or c.claim_is_active(now)
        ):
            raise CandidateUnavailable("unavailable")
        claimed = c.claimed(token=token, at=now)
        self.items[candidate_id] = claimed
        return claimed

    def _holds(self, candidate_id: str, token: str) -> bool:
        c = self.items.get(candidate_id)
        return c is not None and c.claim_token == token

    def release(self, candidate_id: str, token: str) -> bool:
        if self.fail_release:
            raise PersonaCandidateStoreError("down")
        if not self._holds(candidate_id, token):
            return False
        c = self.items[candidate_id]
        from dataclasses import replace

        self.items[candidate_id] = replace(c, claimed_at=None, claim_token=None)
        return True

    def delete(self, candidate_id: str, token: str) -> bool:
        if self.fail_delete:
            raise PersonaCandidateStoreError("down")
        if not self._holds(candidate_id, token):
            return False
        del self.items[candidate_id]
        return True

    def mark_saved(self, candidate_id: str, token: str, now: datetime) -> bool:
        if not self._holds(candidate_id, token):
            return False
        if self.items[candidate_id].saved_at is not None:
            return False
        self.items[candidate_id] = self.items[candidate_id].mark_saved(now)
        return True

    def confirm_claim(self, candidate_id: str, token: str, now: datetime) -> bool:
        self.calls.append("confirm_claim")
        if not self._holds(candidate_id, token):
            return False
        c = self.items[candidate_id]
        if c.saved_at is not None:
            return False
        self.items[candidate_id] = c.claimed(token=token, at=now)
        return True


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _output(count: int = 1) -> Any:
    return mod._PersonaListOutput(
        personas=[
            mod._PersonaOutput(
                name=f"田中{i}",
                age=30 + i,
                occupation="会社員",
                background="背景",
                values=["v"],
                pain_points=["p"],
                goals=["g"],
            )
            for i in range(count)
        ]
    )


@pytest.fixture
def store() -> FakeCandidateStore:
    return FakeCandidateStore()


@pytest.fixture
def s3() -> Mock:
    s3 = Mock()
    s3.upload_file.side_effect = lambda content, key, **kw: f"s3://bucket/{key}"
    s3.path_of.side_effect = lambda key: f"s3://bucket/{key}"
    return s3


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _manager(store, s3, clock, *, count: int = 1, log=None) -> PersonaGenerationManager:
    agent = Mock()
    agent.create_generation_agent.return_value = Mock()
    agent.run_persona_generation.return_value = (
        _output(count),
        log if log is not None else [{"type": "thinking", "content": "考えた"}],
    )
    return PersonaGenerationManager(
        agent_service=agent,
        dataset_analysis_service=Mock(),
        candidate_store=store,
        s3_service=s3,
        clock=clock,
    )


def _generate(manager: PersonaGenerationManager, principal=A, count: int = 1):
    return manager.generate(
        principal,
        file_contents=[("十分な長さのインタビュー本文です。".encode(), "a.txt")],
        data_type="interview",
        persona_count=count,
    )


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


# =============================================================================
# 生成（BR1.1、BR1.2、BR1.6）
# =============================================================================


class TestGenerate:
    def test_candidates_are_written_with_owner_generation_and_8h_expiry(
        self, store, s3, clock
    ):
        result = _generate(_manager(store, s3, clock, count=3), count=3)

        assert len(result.personas) == 3
        written = [store.items[p.id] for p in result.personas]
        assert {c.owner_sub for c in written} == {A.sub}
        assert len({c.generation_id for c in written}) == 1
        assert {c.expires_at for c in written} == {int(NOW.timestamp()) + 28800}
        assert written[0].generation_log == [{"type": "thinking", "content": "考えた"}]
        assert written[0].generation_context["persona_count"] == 3
        assert store.calls == ["put_all"]

    def test_generation_needs_ai_execute(self, store, s3, clock):
        with pytest.raises(AccessDeniedError):
            _generate(_manager(store, s3, clock), principal=NO_ROLE.principal())
        assert store.items == {}

    def test_a_too_large_candidate_writes_nothing(self, store, s3, clock):
        huge_log = [{"type": "thinking", "content": "あ" * 130_000}]
        manager = _manager(store, s3, clock, count=2, log=huge_log)

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_RESULT_TOO_LARGE
        ):
            _generate(manager, count=2)
        assert store.items == {}

    def test_a_failed_write_fails_the_generation(self, store, s3, clock):
        store.put_all = Mock(side_effect=PersonaCandidateStoreError("down"))
        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_OPERATION_FAILED
        ):
            _generate(_manager(store, s3, clock))

    def test_behavior_csvs_are_staged_under_candidates(
        self, store, s3, clock, monkeypatch
    ):
        csv = b"user_id,amount\nU1,10\nU1,20\n"
        monkeypatch.setattr(
            "src.services.data_agent_service.DataAgentService.download_csv",
            staticmethod(lambda url: csv),
        )
        manager = _manager(store, s3, clock)
        monkeypatch.setattr(manager, "_determine_tools", lambda *a, **kw: [])

        def run(**kwargs: Any) -> Any:
            # the data agent reports a tool call and then a CSV URL
            queue = manager._recording_queue
            queue.put({"type": "tool_call", "detail": "顧客の購買履歴をCSVで出力"})
            queue.put({"type": "csv_url", "url": "https://example.com/a.csv"})
            return _output(1), []

        manager.agent_service.run_persona_generation.side_effect = run
        result = manager.generate(
            A,
            file_contents=[],
            data_type="dwh",
            persona_count=1,
            data_description="購買",
            auto_link_behavior=True,
        )

        candidate = store.items[result.personas[0].id]
        (dataset,) = candidate.behavior_datasets
        assert dataset.temp_s3_key == (
            f"candidates/{A.sub}/{candidate.candidate_id}/{dataset.temp_id}.csv"
        )
        assert dataset.row_count == 2
        assert dataset.binding_keys == {"user_id": "U1"}
        s3.upload_file.assert_called_once()
        assert s3.upload_file.call_args.args[1] == dataset.temp_s3_key
        assert result.behavior_datasets == (dataset,)

    def test_unstaged_csv_is_not_a_candidate(self, store, s3, clock, monkeypatch):
        monkeypatch.setattr(
            "src.services.data_agent_service.DataAgentService.download_csv",
            staticmethod(lambda url: b"user_id\nU1\n"),
        )
        s3.upload_file.side_effect = RuntimeError("s3 down")
        manager = _manager(store, s3, clock)
        monkeypatch.setattr(manager, "_determine_tools", lambda *a, **kw: [])

        def run(**kwargs: Any) -> Any:
            manager._recording_queue.put({"type": "csv_url", "url": "https://x/a.csv"})
            return _output(1), []

        manager.agent_service.run_persona_generation.side_effect = run
        result = manager.generate(
            A,
            file_contents=[],
            data_type="dwh",
            persona_count=1,
            data_description="購買",
            auto_link_behavior=True,
        )
        assert result.behavior_datasets == ()
        assert store.items[result.personas[0].id].behavior_datasets == ()


# =============================================================================
# 押さえ（BR1.3）
# =============================================================================


class TestClaim:
    def test_owner_claims_on_another_instance(self, store, s3, clock):
        generated = _generate(_manager(store, s3, clock, count=2), count=2)
        other_instance = _manager(store, s3, clock)  # another task

        claim = other_instance.claim_candidates(A, [p.id for p in generated.personas])

        assert isinstance(claim, CandidateClaim)
        assert claim.ids == tuple(p.id for p in generated.personas)
        assert all(store.items[i].claim_token == claim.token for i in claim.ids)

    @pytest.mark.parametrize(
        "case", ["other_user", "expired", "in_progress", "missing"]
    )
    def test_unavailable_candidates_are_all_cache_expired(
        self, store, s3, clock, case, logs
    ):
        manager = _manager(store, s3, clock)
        cid = _generate(manager).personas[0].id
        principal = A
        if case == "other_user":
            principal = B
        elif case == "expired":
            clock.now = NOW + timedelta(hours=8)
        elif case == "in_progress":
            manager.claim_candidates(A, [cid])
        else:
            cid = "missing"

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        ):
            manager.claim_candidates(principal, [cid])
        (event,) = logs.events("persona.candidate_unavailable")
        assert event["count"] == 1
        assert logs.events("authz.denied") == []

    def test_double_submit_saves_once(self, store, s3, clock):
        manager = _manager(store, s3, clock)
        cid = _generate(manager).personas[0].id
        manager.claim_candidates(A, [cid])
        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        ):
            manager.claim_candidates(A, [cid])

    def test_one_unavailable_releases_the_others(self, store, s3, clock):
        manager = _manager(store, s3, clock, count=2)
        ids = [p.id for p in _generate(manager, count=2).personas]

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        ):
            manager.claim_candidates(A, ids + ["missing"])
        assert all(store.items[i].claim_token is None for i in ids)

    def test_a_store_failure_releases_and_is_transient(self, store, s3, clock):
        manager = _manager(store, s3, clock, count=2)
        ids = [p.id for p in _generate(manager, count=2).personas]
        store.fail_claim_on = {ids[1]}

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_OPERATION_FAILED
        ):
            manager.claim_candidates(A, ids)
        assert store.items[ids[0]].claim_token is None

    def test_nothing_selected(self, store, s3, clock):
        with raises_code(
            PersonaGenerationManagerError, ErrorCode.PERSONA_SELECTION_REQUIRED
        ):
            _manager(store, s3, clock).claim_candidates(A, [])

    def test_a_stale_claim_can_be_taken_over(self, store, s3, clock):
        manager = _manager(store, s3, clock)
        cid = _generate(manager).personas[0].id
        manager.claim_candidates(A, [cid])
        clock.now = NOW + timedelta(minutes=6)
        assert manager.claim_candidates(A, [cid]).ids == (cid,)


# =============================================================================
# 片付け（BR1.7、BR1.9、NFR8.16）
# =============================================================================


def _claimed_with_dataset(
    store, s3, clock
) -> tuple[PersonaGenerationManager, CandidateClaim]:
    manager = _manager(store, s3, clock, count=2)
    ids = [p.id for p in _generate(manager, count=2).personas]
    from dataclasses import replace

    from src.models.dataset import DatasetColumn
    from src.models.persona_candidate import BehaviorDatasetCandidate

    dataset = BehaviorDatasetCandidate(
        temp_id="t1",
        name="n",
        data_type_label="l",
        temp_s3_key=f"candidates/{A.sub}/{ids[0]}/t1.csv",
        columns=(DatasetColumn(name="c", data_type="string"),),
        row_count=1,
    )
    store.items[ids[0]] = replace(store.items[ids[0]], behavior_datasets=(dataset,))
    return manager, manager.claim_candidates(A, ids)


class TestFinish:
    def test_saved_candidates_and_their_csvs_are_deleted(self, store, s3, clock):
        manager, claim = _claimed_with_dataset(store, s3, clock)
        saved, failed = claim.ids

        manager.finish_candidates(
            claim, CandidateSaveResult(saved_ids=(saved,), failed_ids=(failed,))
        )

        assert saved not in store.items
        s3.delete_file.assert_called_once_with(
            f"s3://bucket/candidates/{A.sub}/{saved}/t1.csv"
        )
        # the failed one is released and can be saved again
        assert store.items[failed].claim_token is None

    def test_no_result_releases_everything(self, store, s3, clock):
        manager, claim = _claimed_with_dataset(store, s3, clock)
        manager.finish_candidates(claim, None)
        assert all(store.items[i].claim_token is None for i in claim.ids)
        s3.delete_file.assert_not_called()

    def test_undeletable_saved_candidate_is_marked_saved(self, store, s3, clock, logs):
        manager, claim = _claimed_with_dataset(store, s3, clock)
        saved = claim.ids[0]
        store.fail_delete = True

        manager.finish_candidates(claim, CandidateSaveResult(saved_ids=(saved,)))

        assert store.items[saved].saved_at is not None
        events = logs.events("persona.candidate_cleanup_failed")
        assert [e["step"] for e in events] == ["delete"]
        assert events[0]["candidate_id"] == saved
        assert events[0]["error_type"] == "PersonaCandidateStoreError"

    def test_cleanup_failures_are_warnings_only(self, store, s3, clock, logs):
        manager, claim = _claimed_with_dataset(store, s3, clock)
        s3.delete_file.side_effect = RuntimeError("s3 down")
        store.fail_release = True

        manager.finish_candidates(
            claim,
            CandidateSaveResult(saved_ids=(claim.ids[0],), failed_ids=(claim.ids[1],)),
        )

        steps = sorted(
            e["step"] for e in logs.events("persona.candidate_cleanup_failed")
        )
        assert steps == ["release", "temp_csv"]
        assert all(
            e["level"] == "WARNING"
            for e in logs.events("persona.candidate_cleanup_failed")
        )


# =============================================================================
# R-04: 押さえを奪われた候補は、元の要求が保存しない（BR1.3、BR1.7、NFR2.13）
# =============================================================================


class TestHoldIsConfirmedBeforeEachSave:
    """A request whose hold went stale and was taken over saves nothing twice."""

    def _persona_manager(self, store, world, clock):
        return world.manager(candidate_store=store, clock=clock)

    def test_a_taken_over_candidate_is_not_saved_by_the_first_request(
        self, store, s3, clock
    ):
        from tests.unit.persona_u5_world import World

        world = World()
        generation = _manager(store, s3, clock)
        cid = _generate(generation).personas[0].id
        first = generation.claim_candidates(A, [cid])
        # the first save stalls past five minutes; a second request takes over
        clock.now = NOW + timedelta(minutes=6)
        second = generation.claim_candidates(A, [cid])

        stale = self._persona_manager(store, world, clock).save_generated_personas(
            A, first
        )
        generation.finish_candidates(first, stale)
        fresh = self._persona_manager(store, world, clock).save_generated_personas(
            A, second
        )
        generation.finish_candidates(second, fresh)

        assert stale.saved_ids == () and stale.failed_ids == (cid,)
        assert stale.failed_names == ("田中0",)
        assert fresh.saved_ids == (cid,)
        assert len(world.personas) == 1  # no second persona
        assert cid not in store.items  # cleaned up by the request that saved

    def test_the_hold_is_checked_right_before_each_candidate(self, store, s3, clock):
        from tests.unit.persona_u5_world import World

        world = World()
        generation = _manager(store, s3, clock, count=2)
        ids = [p.id for p in _generate(generation, count=2).personas]
        claim = generation.claim_candidates(A, ids)
        # the second candidate is taken over while the first one is saved
        original = world.persona_store.put_new.side_effect

        def stall_then_take_over(persona):
            saved = original(persona)
            clock.now = NOW + timedelta(minutes=6)
            store.claim(ids[1], A.sub, "another-request", clock.now)
            return saved

        world.persona_store.put_new.side_effect = stall_then_take_over
        result = self._persona_manager(store, world, clock).save_generated_personas(
            A, claim
        )

        assert result.saved_ids == (ids[0],)
        assert result.failed_ids == (ids[1],)
        assert len(world.personas) == 1
        # saved_at is written under the same hold before the cleanup
        assert store.items[ids[0]].saved_at is not None
        assert store.items[ids[1]].claim_token == "another-request"

    def test_a_taken_over_single_save_is_cache_expired(self, store, s3, clock):
        from src.managers.persona_manager import PersonaManagerError
        from src.models.persona import Persona
        from tests.unit.persona_u5_world import World

        world = World()
        generation = _manager(store, s3, clock)
        cid = _generate(generation).personas[0].id
        first = generation.claim_candidates(A, [cid])
        clock.now = NOW + timedelta(minutes=6)
        generation.claim_candidates(A, [cid])
        edited = Persona.create_new(
            name="編集後",
            age=40,
            occupation="会社員",
            background="背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )

        with raises_code(
            PersonaManagerError, ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
        ):
            self._persona_manager(store, world, clock).save_generated_personas(
                A, first, edited=edited
            )
        assert world.personas == {}


class TestNoInProcessCache:
    def test_the_manager_keeps_no_ttl_cache(self, store, s3, clock):
        manager = _manager(store, s3, clock)
        assert not hasattr(manager, "_personas_cache")
        assert not hasattr(manager, "_behavior_datasets_cache")
        for name in (
            "generate_and_cache",
            "get_cached_persona",
            "pop_cached_persona",
            "get_cached_behavior_datasets",
            "pop_cached_behavior_datasets",
            "build_and_cache_behavior_datasets",
        ):
            assert not hasattr(PersonaGenerationManager, name)

    def test_candidate_store_type(self):
        assert PersonaCandidateStore.item_size  # the size measure lives in the Service
