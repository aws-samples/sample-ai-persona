"""PersonaManager.delete_persona の後始末（U5、reliability-design.md §1）。

BR3.1〜BR3.4、NFR1.35、NFR1.42、NFR8.14、AC5.4.1〜AC5.4.3。
"""

import pytest

from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.memory import DeleteAllResult
from src.models.persona_binding import PersonaBinding
from tests.error_helpers import raises_code
from tests.log_helpers import LogCapture
from tests.unit.persona_u5_world import (
    ADMIN_P,
    A,
    B,
    NOW,
    SALES_PEER,
    World,
    make_persona,
)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


@pytest.fixture
def persona(world):
    """member A's persona with an org dataset, a dedicated dataset, a KB and an avatar."""
    p = world.add_persona(
        make_persona(A.sub, ["sales"]).with_avatar("s3://bucket/persona_avatars/p.webp")
    )
    org = world.add_dataset(scope=["sales"])
    dedicated = world.add_dataset(
        owner_persona_id=p.id, s3_path=f"s3://bucket/persona_datasets/{p.id}/d.csv"
    )
    kb = world.add_kb()
    world.bindings.put(PersonaBinding.for_dataset(p.id, org.id, now=NOW))
    world.bindings.put(
        PersonaBinding.for_dataset(p.id, dedicated.id, dedicated=True, now=NOW)
    )
    world.bindings.put(PersonaBinding.for_kb(p.id, kb.id, now=NOW))
    world.org_id = org.id  # type: ignore[attr-defined]
    world.dedicated_id = dedicated.id  # type: ignore[attr-defined]
    return p


def _cleanup_events(logs: LogCapture) -> list[dict]:
    return logs.events("persona.cleanup_failed")


class TestDeleteCleansUp:
    def test_everything_attached_to_the_persona_goes(self, world, persona, logs):
        assert world.manager().delete_persona(A, persona.id)

        assert persona.id not in world.personas
        assert world.bindings.items == {}
        world.memory.delete_all.assert_called_once()
        assert world.memory.delete_all.call_args.args[0] == persona.id
        # only the dedicated dataset is deleted (registration and CSV)
        assert world.dedicated_id not in world.datasets
        assert world.org_id in world.datasets
        deleted = {c.args[0] for c in world.s3.delete_file.call_args_list}
        assert deleted == {
            f"s3://bucket/persona_datasets/{persona.id}/d.csv",
            "s3://bucket/persona_avatars/p.webp",
        }
        assert _cleanup_events(logs) == []

    def test_memory_gets_a_30_second_deadline(self, world, persona, monkeypatch):
        from src.managers import persona_manager as mod

        monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
        world.manager().delete_persona(A, persona.id)
        assert world.memory.delete_all.call_args.kwargs["deadline"] == 130.0

    def test_without_long_term_memory_nothing_is_called(
        self, world, persona, logs, monkeypatch
    ):
        from src.managers import persona_manager as mod

        monkeypatch.setattr(mod.config, "ENABLE_LONG_TERM_MEMORY", False)
        world.manager(memory_service=None).delete_persona(A, persona.id)
        world.memory.delete_all.assert_not_called()
        assert _cleanup_events(logs) == []

    def test_memory_on_without_a_service_is_logged(
        self, world, persona, logs, monkeypatch
    ):
        """R-03: memory is on but its service could not be built (BR3.3)."""
        from src.managers import persona_manager as mod

        monkeypatch.setattr(mod.config, "ENABLE_LONG_TERM_MEMORY", True)
        assert world.manager(memory_service=None).delete_persona(A, persona.id)

        (event,) = _cleanup_events(logs)
        assert (event["step"], event["reason"]) == ("memory", "error")
        assert event["persona_id"] == persona.id
        assert event["level"] == "ERROR"
        # the other steps still ran
        assert world.bindings.items == {}
        assert world.dedicated_id not in world.datasets


class TestCleanupFailuresAreLoggedNotRaised:
    def test_memory_timeout(self, world, persona, logs):
        world.memory.delete_all.return_value = DeleteAllResult(
            deleted=1, remaining=None, timed_out=True
        )
        assert world.manager().delete_persona(A, persona.id)

        (event,) = _cleanup_events(logs)
        assert (event["step"], event["reason"]) == ("memory", "timeout")
        assert event["persona_id"] == persona.id
        assert event["level"] == "ERROR"
        assert "remaining" not in event  # unknown (None is not written)

    def test_memory_error(self, world, persona, logs):
        world.memory.delete_all.return_value = DeleteAllResult(
            deleted=0, remaining=3, timed_out=False, error_type="ClientError"
        )
        world.manager().delete_persona(A, persona.id)
        (event,) = _cleanup_events(logs)
        assert (event["step"], event["reason"], event["remaining"]) == (
            "memory",
            "error",
            3,
        )
        assert event["error_type"] == "ClientError"

    def test_binding_delete_failure_still_removes_the_dedicated_dataset(
        self, world, persona, logs
    ):
        world.bindings.fail_delete_all = True
        assert world.manager().delete_persona(A, persona.id)

        assert [e["step"] for e in _cleanup_events(logs)] == ["bindings"]
        assert world.dedicated_id not in world.datasets
        world.memory.delete_all.assert_called_once()

    def test_unreadable_bindings_skip_dedicated_datasets(self, world, persona, logs):
        world.bindings.fail_list = True
        assert world.manager().delete_persona(A, persona.id)
        assert [e["step"] for e in _cleanup_events(logs)] == ["bindings"]
        assert world.dedicated_id in world.datasets  # cannot be found: logged

    def test_dataset_and_avatar_failures(self, world, persona, logs):
        world.db.delete_dataset.side_effect = RuntimeError("db down")
        world.s3.delete_file.side_effect = RuntimeError("s3 down")
        assert world.manager().delete_persona(A, persona.id)

        steps = [e["step"] for e in _cleanup_events(logs)]
        assert steps == ["persona_datasets", "avatar"]
        assert all("down" not in str(e) for e in _cleanup_events(logs))


class TestDeleteDecision:
    def test_shared_reader_gets_403_and_nothing_is_removed(self, world, persona):
        with pytest.raises(AccessDeniedError):
            world.manager().delete_persona(SALES_PEER, persona.id)
        assert len(world.bindings.items) == 3
        world.memory.delete_all.assert_not_called()

    def test_unrelated_user_gets_404(self, world, persona):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().delete_persona(B, persona.id)

    def test_admin_deletes_a_legacy_persona(self, world):
        legacy = world.add_persona(make_persona(None))
        assert world.manager().delete_persona(ADMIN_P, legacy.id)
