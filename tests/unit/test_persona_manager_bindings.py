"""PersonaManager の紐付け・プレビュー・候補からの保存・1 体の読み出し・アバター（U5）。

BR1.4、BR1.5、BR1.9、BR2.1〜BR2.9、BR4.3〜BR4.5、BR6.1、BR6.4、NFR1.24〜NFR1.26、
NFR2.10、AC2.3.1、AC2.3.3。判定は本物の AccessPolicy と ResourceLookup。
"""

from dataclasses import replace

import pytest

from src.managers.persona_manager import PersonaManagerError
from src.models.errors import AccessDeniedError, CodedError, ErrorCode
from src.models.persona_binding import KB_TARGET_KEY, PersonaBinding, dataset_target_key
from src.models.persona_candidate import (
    BehaviorDatasetCandidate,
    CandidateClaim,
    PersonaCandidate,
)
from src.models.dataset import DatasetColumn
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
def shared(world) -> object:
    """member A's persona shared with ``sales``."""
    return world.add_persona(make_persona(A.sub, ["sales"]))


@pytest.fixture
def private(world) -> object:
    """member A's persona, not shared."""
    return world.add_persona(make_persona(A.sub))


# =============================================================================
# 一覧（BR2.1、BR2.7、BR2.8、NFR2.10）
# =============================================================================


class TestListBindings:
    def test_readers_see_bindings_with_names_and_usability(self, world, shared):
        org = world.add_dataset(scope=["sales"])
        locked = world.add_dataset(name="使えない")
        dedicated = world.add_dataset(owner_persona_id=shared.id, name="専用")
        for d in (org, locked, dedicated):
            world.bindings.put(
                PersonaBinding.for_dataset(
                    shared.id, d.id, dedicated=d is dedicated, now=NOW
                )
            )

        view = world.manager().get_dataset_bindings(SALES_PEER, shared.id)

        rows = {r.target_id: r for r in view.bindings}
        assert rows[org.id].name == "購買データ" and rows[org.id].usable
        assert not rows[locked.id].usable  # shown with 「利用権限なし」
        assert rows[dedicated.id].usable and rows[dedicated.id].dedicated
        assert not view.manageable
        assert view.choices == []  # no operations for a shared reader

    def test_bindings_to_vanished_targets_are_left_out(self, world, shared):
        gone = world.add_dataset()
        world.bindings.put(PersonaBinding.for_dataset(shared.id, gone.id, now=NOW))
        del world.datasets[gone.id]

        view = world.manager().get_dataset_bindings(A, shared.id)

        assert view.bindings == []
        # nothing was written by reading (BR2.8)
        assert (shared.id, dataset_target_key(gone.id)) in world.bindings.items

    def test_unreadable_persona_is_not_found(self, world, private):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().get_dataset_bindings(B, private.id)
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().get_kb_binding(B, private.id)

    def test_choices_are_usable_org_assets_for_managers_only(self, world, private):
        usable = world.add_dataset(scope=["sales"])
        world.add_dataset(name="範囲外")
        world.add_dataset(owner_persona_id="other", name="他の専用")

        member_view = world.manager().get_dataset_bindings(A, private.id)
        admin_view = world.manager().get_dataset_bindings(ADMIN_P, private.id)

        assert [d.id for d in member_view.choices] == [usable.id]
        assert {d.name for d in admin_view.choices} == {"購買データ", "範囲外"}

    def test_kb_binding_view(self, world, shared):
        kb = world.add_kb()
        world.bindings.put(
            PersonaBinding.for_kb(
                shared.id, kb.id, metadata_filters={"a": "b"}, now=NOW
            )
        )

        view = world.manager().get_kb_binding(SALES_PEER, shared.id)

        assert view.binding is not None
        assert view.binding.name == "社内KB"
        assert view.binding.metadata_filters == {"a": "b"}
        assert not view.binding.usable  # no scope until U7 -> admin only
        assert view.choices == []

    def test_kb_names_come_from_one_lookup(self, world, shared, monkeypatch):
        kb = world.add_kb(scope=["sales"])
        world.bindings.put(PersonaBinding.for_kb(shared.id, kb.id, now=NOW))
        calls: list[object] = []
        original = world.lookup.get_many

        def spy(kind, ids):
            calls.append(kind)
            return original(kind, ids)

        monkeypatch.setattr(world.lookup, "get_many", spy)
        world.manager().get_kb_binding(A, shared.id)
        from src.models.authz import ResourceType

        assert calls.count(ResourceType.KNOWLEDGE_BASE) <= 2  # bound + choices

    def test_ten_bindings_read_each_asset_kind_once(self, world, shared, monkeypatch):
        """NFR2.10: 名前と利用範囲は種類ごとに 1 回でまとめて読む（紐付けごとに読まない）。

        KB の紐付けはペルソナに 1 つ（固定キー ``KB``、BR2.9）なので、10 件はデータセット 9 と KB 1。
        共有された人で見る（管理できないので選択肢の読みはない）。
        """
        from src.models.authz import ResourceType
        from tests.unit.persona_u5_world import _Reader

        datasets = [world.add_dataset(scope=["sales"], name=f"d{i}") for i in range(9)]
        for d in datasets:
            world.bindings.put(PersonaBinding.for_dataset(shared.id, d.id, now=NOW))
        kb = world.add_kb(scope=["sales"])
        world.bindings.put(PersonaBinding.for_kb(shared.id, kb.id, now=NOW))
        assert len(world.bindings.list_for_persona(shared.id)) == 10

        reads: list[tuple[ResourceType, tuple[str, ...]]] = []
        original_read = _Reader.read

        def counting_read(self, ids, deadline):
            reads.append((self.kind, tuple(ids)))
            return original_read(self, ids, deadline)

        singles: list[tuple[object, str]] = []
        original_get = world.lookup.get

        def counting_get(kind, resource_id):
            singles.append((kind, resource_id))
            return original_get(kind, resource_id)

        monkeypatch.setattr(_Reader, "read", counting_read)
        monkeypatch.setattr(world.lookup, "get", counting_get)
        manager = world.manager()

        dataset_view = manager.get_dataset_bindings(SALES_PEER, shared.id)
        kb_view = manager.get_kb_binding(SALES_PEER, shared.id)

        assert len(dataset_view.bindings) == 9 and kb_view.binding is not None
        dataset_reads = [ids for kind, ids in reads if kind is ResourceType.DATASET]
        kb_reads = [ids for kind, ids in reads if kind is ResourceType.KNOWLEDGE_BASE]
        assert len(dataset_reads) == 1  # 9 件を 1 回の読みで
        assert set(dataset_reads[0]) == {d.id for d in datasets}
        assert kb_reads == [(kb.id,)]
        assert singles == []  # 1 件ずつの get を呼ばない


# =============================================================================
# 作成・削除（BR2.2〜BR2.5、BR2.9、BR6.4）
# =============================================================================


class TestCreateBinding:
    def test_owner_binds_a_usable_org_dataset(self, world, private):
        dataset = world.add_dataset(scope=["sales"])

        world.manager().create_dataset_binding(
            A, private.id, dataset.id, key_name="user_id", key_value="U1"
        )

        binding = world.bindings.items[(private.id, dataset_target_key(dataset.id))]
        assert binding.binding_keys == {"user_id": "U1"}

    def test_shared_reader_cannot_bind(self, world, shared):
        dataset = world.add_dataset(scope=["sales"])
        with pytest.raises(AccessDeniedError):
            world.manager().create_dataset_binding(SALES_PEER, shared.id, dataset.id)
        assert world.bindings.items == {}

    def test_unusable_org_dataset_is_not_found(self, world, private):
        dataset = world.add_dataset()  # no scope: admin only until U7
        with raises_code(PersonaManagerError, ErrorCode.DATASET_NOT_FOUND):
            world.manager().create_dataset_binding(A, private.id, dataset.id)

    def test_admin_binds_an_unscoped_org_dataset(self, world, private):
        dataset = world.add_dataset()
        world.manager().create_dataset_binding(ADMIN_P, private.id, dataset.id)
        assert (private.id, dataset_target_key(dataset.id)) in world.bindings.items

    def test_dedicated_dataset_binds_only_to_its_persona(self, world, private):
        other = world.add_persona(make_persona(A.sub))
        dedicated = world.add_dataset(owner_persona_id=private.id)

        world.manager().create_dataset_binding(A, private.id, dedicated.id)
        with raises_code(PersonaManagerError, ErrorCode.DATASET_NOT_FOUND):
            world.manager().create_dataset_binding(A, other.id, dedicated.id)

    def test_unknown_key_column(self, world, private):
        dataset = world.add_dataset(scope=["sales"])
        with raises_code(
            PersonaManagerError, ErrorCode.DATASET_COLUMN_NOT_FOUND, column="nope"
        ):
            world.manager().create_dataset_binding(
                A, private.id, dataset.id, key_name="nope", key_value="x"
            )

    def test_kb_binding_is_replaced_in_one_write(self, world, private):
        first, second = world.add_kb(), world.add_kb()
        manager = world.manager()
        manager.create_kb_binding(ADMIN_P, private.id, first.id)
        manager.create_kb_binding(ADMIN_P, private.id, second.id, {"k": "v"})

        kbs = [k for k in world.bindings.items if k[1] == KB_TARGET_KEY]
        assert kbs == [(private.id, KB_TARGET_KEY)]
        assert world.bindings.items[kbs[0]].target_id == second.id

    def test_member_cannot_bind_an_unscoped_kb(self, world, private):
        kb = world.add_kb()
        with raises_code(PersonaManagerError, ErrorCode.KB_BINDING_NOT_FOUND):
            world.manager().create_kb_binding(A, private.id, kb.id)

    def test_legacy_persona_without_owner_is_managed_by_admin_only(self, world):
        legacy = world.add_persona(make_persona(None))
        dataset = world.add_dataset(scope=["sales"])
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().create_dataset_binding(A, legacy.id, dataset.id)
        world.manager().create_dataset_binding(ADMIN_P, legacy.id, dataset.id)


class TestDeleteBinding:
    def test_delete_by_persona_and_dataset(self, world, private):
        other = world.add_persona(make_persona(A.sub))
        dataset = world.add_dataset(scope=["sales"])
        for pid in (private.id, other.id):
            world.bindings.put(PersonaBinding.for_dataset(pid, dataset.id, now=NOW))

        world.manager().delete_dataset_binding(A, private.id, dataset.id)

        assert (private.id, dataset_target_key(dataset.id)) not in world.bindings.items
        assert (other.id, dataset_target_key(dataset.id)) in world.bindings.items

    def test_missing_binding_is_not_found(self, world, private):
        with raises_code(PersonaManagerError, ErrorCode.DATASET_BINDING_NOT_FOUND):
            world.manager().delete_dataset_binding(A, private.id, "nothing")
        with raises_code(PersonaManagerError, ErrorCode.KB_BINDING_NOT_FOUND):
            world.manager().delete_kb_binding(A, private.id)

    def test_shared_reader_cannot_delete(self, world, shared):
        kb = world.add_kb()
        world.bindings.put(PersonaBinding.for_kb(shared.id, kb.id, now=NOW))
        with pytest.raises(AccessDeniedError):
            world.manager().delete_kb_binding(SALES_PEER, shared.id)
        assert (shared.id, KB_TARGET_KEY) in world.bindings.items

    def test_unbinding_a_dedicated_dataset_deletes_it(self, world, private):
        dedicated = world.add_dataset(
            owner_persona_id=private.id,
            s3_path=f"s3://bucket/persona_datasets/{private.id}/d.csv",
        )
        world.bindings.put(
            PersonaBinding.for_dataset(
                private.id, dedicated.id, dedicated=True, now=NOW
            )
        )

        world.manager().delete_dataset_binding(A, private.id, dedicated.id)

        assert dedicated.id not in world.datasets
        world.s3.delete_file.assert_called_once_with(dedicated.s3_path)

    def test_failed_dataset_cleanup_keeps_the_unbinding(self, world, private, logs):
        dedicated = world.add_dataset(owner_persona_id=private.id)
        world.bindings.put(
            PersonaBinding.for_dataset(
                private.id, dedicated.id, dedicated=True, now=NOW
            )
        )
        world.dataset_store.delete.side_effect = RuntimeError("down")

        world.manager().delete_dataset_binding(A, private.id, dedicated.id)

        assert world.bindings.items == {}
        (event,) = logs.events("persona.cleanup_failed")
        assert event["step"] == "persona_datasets"


# =============================================================================
# プレビュー（BR2.6）
# =============================================================================


class TestPreview:
    def test_reader_previews_rows_narrowed_by_binding_keys(self, world, shared):
        dataset = world.add_dataset(scope=["sales"])
        world.bindings.put(
            PersonaBinding.for_dataset(
                shared.id, dataset.id, binding_keys={"user_id": "U1"}, now=NOW
            )
        )

        data = world.manager().preview_binding_data(SALES_PEER, shared.id, dataset.id)

        assert data == {"columns": ["user_id"], "rows": [["U1"]], "total_count": 1}
        kwargs = world.preview.execute_dataset_preview.call_args.kwargs
        assert kwargs["params"] == ["U1"]
        assert '"user_id" = $1' in kwargs["data_sql"]

    def test_unusable_org_asset_is_binding_not_found(self, world, shared):
        dataset = world.add_dataset()  # not usable by sales
        world.bindings.put(PersonaBinding.for_dataset(shared.id, dataset.id, now=NOW))
        with raises_code(PersonaManagerError, ErrorCode.DATASET_BINDING_NOT_FOUND):
            world.manager().preview_binding_data(SALES_PEER, shared.id, dataset.id)
        world.preview.execute_dataset_preview.assert_not_called()

    def test_dedicated_dataset_is_previewed_by_readers(self, world, shared):
        dedicated = world.add_dataset(owner_persona_id=shared.id)
        world.bindings.put(
            PersonaBinding.for_dataset(shared.id, dedicated.id, dedicated=True, now=NOW)
        )
        world.manager().preview_binding_data(SALES_PEER, shared.id, dedicated.id)
        world.preview.execute_dataset_preview.assert_called_once()

    def test_unreadable_persona_and_unknown_binding(self, world, private, shared):
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            world.manager().preview_binding_data(B, private.id, "d")
        with raises_code(PersonaManagerError, ErrorCode.DATASET_BINDING_NOT_FOUND):
            world.manager().preview_binding_data(A, shared.id, "d")


# =============================================================================
# 候補からの保存（BR1.4、BR1.5、BR1.9、BR6.1）
# =============================================================================


def _candidate(
    world: World, *, datasets: int = 0, name: str = "佐藤 花子"
) -> PersonaCandidate:
    base = make_persona(None, name=name)
    behavior = tuple(
        BehaviorDatasetCandidate(
            temp_id=f"t{i}",
            name=f"{name}_購買{i}",
            data_type_label="購買",
            temp_s3_key=f"candidates/{A.sub}/{base.id}/t{i}.csv",
            columns=(DatasetColumn(name="user_id", data_type="string"),),
            row_count=2,
            binding_key_column="user_id",
            binding_key_value="U1",
        )
        for i in range(datasets)
    )
    return PersonaCandidate.from_persona(
        base,
        owner_sub=A.sub,
        generation_id="g",
        ttl_seconds=60,
        now=NOW,
        generation_log=[{"type": "thinking", "content": "考えた"}],
        generation_context={"data_type": "dwh"},
        behavior_datasets=behavior,
    )


def _claim(*candidates: PersonaCandidate) -> CandidateClaim:
    return CandidateClaim(token="tok", owner_sub=A.sub, candidates=tuple(candidates))


class TestSaveGeneratedPersonas:
    def test_bulk_save_uses_new_ids_and_the_saver_as_owner(self, world):
        first, second = _candidate(world), _candidate(world, name="鈴木")

        result = world.manager().save_generated_personas(A, _claim(first, second))

        assert result.saved_ids == (first.candidate_id, second.candidate_id)
        saved = [world.personas[pid] for pid in result.saved_persona_ids]
        assert {p.owner_sub for p in saved} == {A.sub}
        assert {p.id for p in saved}.isdisjoint(
            {first.candidate_id, second.candidate_id}
        )
        assert all(p.shared_group_ids == frozenset() for p in saved)
        assert saved[0].generation_log == [{"type": "thinking", "content": "考えた"}]

    def test_one_failure_does_not_stop_the_others(self, world):
        good, bad = _candidate(world), _candidate(world, name="壊れた")
        original = world.persona_store.put_new.side_effect

        def flaky(persona):
            if persona.name == "壊れた":
                raise RuntimeError("down")
            return original(persona)

        world.persona_store.put_new.side_effect = flaky
        result = world.manager().save_generated_personas(A, _claim(good, bad))

        assert result.saved_ids == (good.candidate_id,)
        assert result.failed_ids == (bad.candidate_id,)
        assert result.failed_names == ("壊れた",)

    def test_single_save_takes_the_edited_fields_and_the_generation_record(self, world):
        candidate = _candidate(world)
        edited = replace(candidate.to_persona(), name="直した名前")

        result = world.manager().save_generated_personas(
            A, _claim(candidate), edited=edited
        )

        (persona_id,) = result.saved_persona_ids
        saved = world.personas[persona_id]
        assert saved.name == "直した名前"
        assert saved.generation_context == {"data_type": "dwh"}
        assert saved.id != candidate.candidate_id

    def test_single_save_validation_error_is_raised(self, world):
        candidate = _candidate(world)
        edited = replace(candidate.to_persona(), name="")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED):
            world.manager().save_generated_personas(A, _claim(candidate), edited=edited)

    def test_selected_behavior_data_becomes_dedicated_datasets(self, world):
        candidate = _candidate(world, datasets=2)

        result = world.manager().save_generated_personas(
            A,
            _claim(candidate),
            edited=candidate.to_persona(),
            selected_temp_ids={"t1", "unknown-temp-id"},
        )

        (persona_id,) = result.saved_persona_ids
        (dataset,) = world.datasets.values()
        assert dataset.owner_persona_id == persona_id
        assert (
            dataset.s3_path
            == f"s3://bucket/persona_datasets/{persona_id}/{dataset.id}.csv"
        )
        world.s3.copy_object.assert_called_once_with(
            candidate.behavior_datasets[1].temp_s3_key,
            f"persona_datasets/{persona_id}/{dataset.id}.csv",
        )
        binding = world.bindings.items[(persona_id, dataset_target_key(dataset.id))]
        assert binding.dedicated
        assert binding.binding_keys == {"user_id": "U1"}

    def test_binding_failure_removes_the_copy_and_the_metadata(self, world):
        candidate = _candidate(world, datasets=1)
        world.bindings.fail_put = True

        result = world.manager().save_generated_personas(
            A,
            _claim(candidate),
            edited=candidate.to_persona(),
            selected_temp_ids={"t0"},
        )

        assert result.saved_ids == (candidate.candidate_id,)
        assert result.failed_datasets == ("佐藤 花子_購買0",)
        assert world.datasets == {}
        world.s3.delete_file.assert_called_once()

    def test_copy_failure_names_the_dataset(self, world):
        candidate = _candidate(world, datasets=1)
        world.s3.copy_object.side_effect = RuntimeError("s3 down")

        result = world.manager().save_generated_personas(
            A,
            _claim(candidate),
            edited=candidate.to_persona(),
            selected_temp_ids={"t0"},
        )

        assert result.failed_datasets == ("佐藤 花子_購買0",)
        assert world.datasets == {}


# =============================================================================
# 1 体の読み出しとアバター（BR4.3〜BR4.5）
# =============================================================================


class TestGetPersonaAndAvatar:
    def test_get_persona_needs_read(self, world, shared, private):
        manager = world.manager()
        assert manager.get_persona(SALES_PEER, shared.id).id == shared.id
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_persona(SALES_PEER, private.id)

    def test_avatar_url_needs_read(self, world, shared, private):
        world.personas[shared.id] = shared.with_avatar(
            "s3://bucket/persona_avatars/x.webp"
        )
        world.personas[private.id] = private.with_avatar("s3://bucket/a.webp")
        world.s3.generate_presigned_url.return_value = "https://signed"
        manager = world.manager()

        result = manager.deliver_avatar(SALES_PEER, shared.id)
        assert result.presigned_url == "https://signed"
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.deliver_avatar(SALES_PEER, private.id)

    def test_avatar_changes_need_manage(self, world, shared):
        manager = world.manager()
        with pytest.raises(AccessDeniedError):
            manager.delete_avatar(SALES_PEER, shared.id)
        with pytest.raises(AccessDeniedError):
            manager.set_avatar(SALES_PEER, shared.id, b"x")
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.delete_avatar(B, shared.id)

    def test_owner_removes_the_avatar(self, world, private):
        world.personas[private.id] = private.with_avatar("s3://bucket/a.webp")
        updated = world.manager().delete_avatar(A, private.id)
        assert updated.avatar_path is None
        world.s3.delete_file.assert_called_once_with("s3://bucket/a.webp")
