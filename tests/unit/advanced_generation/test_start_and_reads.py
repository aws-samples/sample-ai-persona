"""AdvancedPersonaGenerationManager: the start, the free slot, the reads and the files' reuse."""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import Mock

import pytest

from src.managers.advanced_generation.errors import AdvancedGenerationError
from src.managers.advanced_generation.manager import AdvancedPersonaGenerationManager
from src.models.advanced_generation import GenerationStatus
from src.models.errors import ErrorCode
from src.services.stores.advanced_generation_store import AdvancedGenerationStore
from tests.error_helpers import raises_code
from tests.unit.advanced_generation.helpers import (
    ADMIN,
    DAYS,
    MEMBER,
    OTHER,
    START,
    _boom,
    _Env,
    _start,
)

pytestmark = pytest.mark.unit


class TestStart:
    def test_writes_lock_generation_and_mark(self, env: _Env) -> None:
        file = env.ready_file()

        generation_id = _start(env.manager(), file)

        generation = env.generations.get(generation_id)
        assert generation is not None
        assert generation.status is GenerationStatus.PREPARING
        lock = env.generations.get_lock(ADMIN.sub)
        assert lock is not None and lock.generation_id == generation_id
        marked = env.files.get(file.source_file_id)
        assert marked is not None and marked.in_use_by == generation_id

    def test_unavailable_user_is_refused(self, env: _Env) -> None:
        file = env.ready_file(owner=MEMBER.sub)

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_UNAVAILABLE
        ):
            _start(env.manager(), file, principal=MEMBER)

    @pytest.mark.parametrize("count", [0, 11])
    def test_persona_count_outside_1_to_10(self, env: _Env, count: int) -> None:
        file = env.ready_file()

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_PERSONA_COUNT_INVALID
        ):
            _start(env.manager(), file, count=count)

    def test_four_tables_are_refused(self, env: _Env) -> None:
        # three tables at most
        files = [env.ready_file() for _ in range(4)]

        with raises_code(
            AdvancedGenerationError,
            ErrorCode.UPLOAD_SOURCE_INVALID,
            reason="tables",
            max=3,
        ):
            env.manager().start_generation(
                ADMIN,
                source_file_ids=[f.source_file_id for f in files],
                purpose="",
                persona_count=2,
            )

    def test_purpose_over_the_limit_is_refused(self, env: _Env) -> None:
        file = env.ready_file()

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_PURPOSE_TOO_LONG
        ):
            env.manager().start_generation(
                ADMIN,
                source_file_ids=[file.source_file_id],
                purpose="あ" * 2001,
                persona_count=2,
            )

    def test_someone_elses_file_is_not_found(self, env: _Env) -> None:
        file = env.ready_file(owner=OTHER.sub)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_SOURCE_NOT_FOUND
        ):
            _start(env.manager(), file)

    def test_no_free_slot_is_busy_and_writes_nothing(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=1)
        _start(manager, env.ready_file())
        other = env.ready_file(owner=OTHER.sub)

        with raises_code(AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_BUSY):
            _start(manager, other, principal=OTHER)
        assert env.generations.get_lock(OTHER.sub) is None

    def test_held_lock_is_already_running_and_returns_the_slot(self, env: _Env) -> None:
        manager = env.manager(max_concurrent=2)
        _start(manager, env.ready_file())

        with raises_code(
            AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING
        ):
            _start(manager, env.ready_file())
        # The refused start gave its slot back: another user can still start.
        _start(manager, env.ready_file(owner=OTHER.sub), principal=OTHER)


class TestSlotIsAlwaysReturned:
    """The free slot goes back last and once, whatever an end step raises (§2)."""

    def _slot_is_free_exactly_once(self, manager, env: _Env, caplog) -> None:
        _start(manager, env.ready_file(owner=OTHER.sub), principal=OTHER)
        with raises_code(AdvancedGenerationError, ErrorCode.ADVANCED_GENERATION_BUSY):
            _start(manager, env.ready_file())
        assert "released twice" not in caplog.text

    def test_run_returns_the_slot_when_the_end_log_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(manager._finishing, "log_finished", _boom)

        manager.run(generation_id)  # never raises (a pool thread)

        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_run_returns_the_slot_when_the_store_end_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(env.generations, "finish", _boom)
        monkeypatch.setattr(manager._finishing, "log_finished", _boom)

        manager.run(generation_id)

        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_abort_start_returns_the_slot_when_the_transition_raises(
        self, env: _Env, caplog, monkeypatch
    ) -> None:
        manager = env.manager(max_concurrent=1)
        generation_id = _start(manager, env.ready_file())
        monkeypatch.setattr(env.generations, "transition", _boom)

        manager.abort_start(generation_id)

        # The lock and the mark still come off (each end step stands alone).
        assert env.generations.get_lock(ADMIN.sub) is None
        self._slot_is_free_exactly_once(manager, env, caplog)

    def test_abort_start_returns_the_slot_when_the_read_raises(
        self, env: _Env, caplog
    ) -> None:
        manager = env.manager(max_concurrent=1)
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.generations.get = _boom  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                manager.abort_start(
                    generation_id,
                    owner_sub=ADMIN.sub,
                    source_file_ids=[file.source_file_id],
                )
        finally:
            del env.generations.get  # back to the store's own method

        # The holds come off from what the caller knows, not 75 minutes later
        assert env.generations.get_lock(ADMIN.sub) is None
        released = env.files.get(file.source_file_id)
        assert released is not None and released.in_use_by is None
        self._slot_is_free_exactly_once(manager, env, caplog)


class TestReads:
    def _done(self, env: _Env) -> tuple[AdvancedPersonaGenerationManager, str]:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        manager.run(generation_id)
        return manager, generation_id

    def test_events_after_a_sequence(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        progress = manager.stream_events(ADMIN, generation_id, after_sequence=2)

        assert progress.is_finished
        assert [e.sequence for e in progress.events][0] == 3

    def test_elapsed_of_a_running_generation_counts_from_its_start(
        self, env: _Env
    ) -> None:
        # elapsed time counts from the generation's created_at
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.clock.advance(minutes=2, seconds=14)

        clock = manager.elapsed_clock(ADMIN, generation_id)

        assert clock.seconds == 134
        assert clock.running is True

    def test_elapsed_of_a_finished_generation_stops_at_its_end(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())
        env.sandbox.on_profile = lambda: env.clock.advance(minutes=3)
        manager.run(generation_id)
        env.clock.advance(minutes=10)

        clock = manager.elapsed_clock(ADMIN, generation_id)

        assert clock.seconds == 180
        assert clock.running is False

    def test_elapsed_of_someone_elses_generation_is_not_found(self, env: _Env) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.elapsed_clock(OTHER, generation_id)

    def test_someone_elses_generation_is_not_found(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.stream_events(OTHER, generation_id)
        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_NOT_FOUND):
            manager.get_result(OTHER, generation_id)

    def test_past_24_hours_is_expired(self, env: _Env) -> None:
        manager, generation_id = self._done(env)
        env.clock.advance(hours=25)

        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_EXPIRED):
            manager.stream_events(ADMIN, generation_id)
        with raises_code(AdvancedGenerationError, ErrorCode.GENERATION_EXPIRED):
            manager.get_record(ADMIN, generation_id)

    def test_result_and_record(self, env: _Env) -> None:
        manager, generation_id = self._done(env)

        result = manager.get_result(ADMIN, generation_id)
        record = manager.get_record(ADMIN, generation_id)

        assert len(result.candidates) == 2
        assert sum(s.size for s in result.segments) == 300
        assert result.prepare_reports[0].rows_read == 300
        assert record.generation_id == generation_id
        assert record.seed == 42
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", b["sha256"]) for b in record.code_blocks
        )

    def test_running_generation_is_found_through_the_owner_lock(
        self, env: _Env
    ) -> None:
        manager = env.manager()
        generation_id = _start(manager, env.ready_file())

        assert manager.running_generation_id(ADMIN) == generation_id
        assert manager.running_generation_id(OTHER) is None

    def test_no_running_generation_after_the_end(self, env: _Env) -> None:
        manager, _ = self._done(env)

        assert manager.running_generation_id(ADMIN) is None

    def test_unreadable_lock_means_no_running_generation(self, env: _Env) -> None:
        failing = Mock(spec=AdvancedGenerationStore)
        failing.get_lock.side_effect = AdvancedGenerationError(
            "down", code=ErrorCode.GENERATION_OPERATION_FAILED
        )
        manager = env.manager(generation_store=failing)

        assert manager.running_generation_id(ADMIN) is None

    def test_is_available_uses_the_policy_only(self, env: _Env) -> None:
        manager = AdvancedPersonaGenerationManager(settings_store=env.settings)

        assert manager.is_available(ADMIN) is True
        assert manager.is_available(MEMBER) is False


class TestReuseAndExpiry:
    def test_the_start_stamps_the_last_use_and_moves_the_expiry(
        self, env: _Env
    ) -> None:
        file = env.ready_file()
        env.clock.advance(days=2)

        _start(env.manager(), file)

        found = env.files.get(file.source_file_id)
        assert found is not None
        assert found.last_used_at == env.clock.now
        assert found.expires_at == env.clock.now + timedelta(days=DAYS)

    def test_an_expired_file_cannot_start(self, env: _Env) -> None:
        file = env.ready_file()  # a legacy record: no expires_at
        env.clock.advance(days=DAYS)

        with raises_code(
            AdvancedGenerationError, ErrorCode.GENERATION_SOURCE_NOT_FOUND
        ):
            _start(env.manager(), file)
        found = env.files.get(file.source_file_id)
        assert found is not None and found.in_use_by is None

    def test_the_end_does_not_stamp_the_last_use(self, env: _Env) -> None:
        manager = env.manager()
        file = env.ready_file()
        generation_id = _start(manager, file)
        env.clock.advance(minutes=5)

        manager.run(generation_id)

        found = env.files.get(file.source_file_id)
        assert found is not None and found.last_used_at == START
