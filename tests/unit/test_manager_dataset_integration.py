"""PersonaAgentIntegration（データセット統合の gate・dedup・build）テスト。

dedup（重複 binding の丸ごと除外）と kill-switch/gate 判定は Component の責務。
以前は両 Manager に同一メソッドがコピーされていたが、単一ソース化したため
Component を直接テストする。両 Manager が Component を配線していることは末尾の
委譲スモークテストで確認する。
"""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.managers import agent_discussion_manager, interview_manager
from src.managers.components import persona_agent_integration as pai_module
from src.managers.components.persona_agent_integration import PersonaAgentIntegration
from src.managers.components.resource_lookup import build_resource_lookup
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.dataset import Dataset, DatasetColumn
from src.models.persona_binding import PersonaBinding

pytestmark = pytest.mark.unit


def _binding(persona_id, dataset_id, keys=None):
    return PersonaBinding.for_dataset(
        persona_id, dataset_id, binding_keys=keys or {}, now=datetime.now()
    )


def _dataset(dataset_id):
    now = datetime.now()
    return Dataset(
        id=dataset_id,
        name=f"ds-{dataset_id}",
        description="",
        s3_path=f"s3://bucket/{dataset_id}.csv",
        columns=[DatasetColumn(name="user_id", data_type="string")],
        row_count=10,
        created_at=now,
        updated_at=now,
    )


#: U6: 組織資産を使える人（U7 までは admin だけ）
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))


class _AnyAsset:
    def read(self, ids, deadline):
        return {i: AccessTarget(ResourceType.DATASET, i) for i in ids}


def _component(db, ds_service, agent_service=None):
    # U5: bindings come from PersonaBindingStore (a persona_id query)
    binding_store = Mock()
    binding_store.list_for_persona.side_effect = lambda pid: list(
        getattr(db, "bindings", [])
    )
    return PersonaAgentIntegration(
        database_service=db,
        agent_service=agent_service or Mock(),
        dataset_analysis_service=ds_service,
        binding_store=binding_store,
        resource_lookup=build_resource_lookup(
            [
                (ResourceType.DATASET, _AnyAsset()),
                (ResourceType.KNOWLEDGE_BASE, _AnyAsset()),
            ],
            sample_rate=0.0,
        ),
    )


class TestResolveDatasetBindings:
    def test_duplicate_dataset_excluded_wholesale(self):
        db = Mock()
        db.bindings = [
            _binding("p1", "ds-dup", {"user_id": "U1"}),
            _binding("p1", "ds-dup", {"user_id": "U2"}),  # 同 dataset に2 binding
            _binding("p1", "ds-ok", {"user_id": "U9"}),
        ]
        db.get_dataset.side_effect = _dataset
        comp = _component(db, Mock())

        accepted, datasets = comp._resolve_dataset_bindings("p1")

        accepted_ids = {b["dataset_id"] for b in accepted}
        assert "ds-dup" not in accepted_ids  # 丸ごと除外
        assert "ds-ok" in accepted_ids
        assert {d.id for d in datasets} == {"ds-ok"}

    def test_unique_empty_binding_keys_allowed(self):
        db = Mock()
        db.bindings = [_binding("p1", "ds-all", {})]
        db.get_dataset.side_effect = _dataset
        comp = _component(db, Mock())

        accepted, _ = comp._resolve_dataset_bindings("p1")
        assert accepted == [{"dataset_id": "ds-all", "binding_keys": {}}]

    def test_no_bindings_returns_empty(self):
        db = Mock()
        db.bindings = []
        comp = _component(db, Mock())
        assert comp._resolve_dataset_bindings("p1") == ([], [])


class TestKillSwitchAndGate:
    def _db_with_binding(self):
        db = Mock()
        db.bindings = [_binding("p1", "ds-ok", {"user_id": "U1"})]
        db.get_dataset.side_effect = _dataset
        return db

    def test_enabled_both_flags_builds_tools_and_prompt(self, monkeypatch):
        monkeypatch.setattr(pai_module.config, "ENABLE_DATASET_ANALYSIS", True)

        ds_service = Mock()
        ds_service.build_binding_tools.return_value = (
            [Mock()],
            [
                {
                    "alias": "dataset_1",
                    "name": "n",
                    "description": "d",
                    "row_count": 1,
                    "columns": ["user_id"],
                }
            ],
        )
        comp = _component(self._db_with_binding(), ds_service)

        bundle = comp.prepare(
            "p1", "BASE", enable_kb=False, enable_dataset=True, principal=ADMIN
        )
        ds_service.build_binding_tools.assert_called_once()
        assert "analyze_dataset" in bundle.enhanced_prompt
        assert bundle.additional_tools

    def test_global_flag_off_skips_everything(self, monkeypatch):
        monkeypatch.setattr(pai_module.config, "ENABLE_DATASET_ANALYSIS", False)

        ds_service = Mock()
        comp = _component(self._db_with_binding(), ds_service)

        bundle = comp.prepare(
            "p1", "BASE", enable_kb=False, enable_dataset=True, principal=ADMIN
        )
        ds_service.build_binding_tools.assert_not_called()
        assert bundle.enhanced_prompt == "BASE"
        assert bundle.additional_tools is None

    def test_session_flag_off_skips_everything(self, monkeypatch):
        monkeypatch.setattr(pai_module.config, "ENABLE_DATASET_ANALYSIS", True)

        ds_service = Mock()
        comp = _component(self._db_with_binding(), ds_service)

        bundle = comp.prepare(
            "p1", "BASE", enable_kb=False, enable_dataset=False, principal=ADMIN
        )
        ds_service.build_binding_tools.assert_not_called()
        assert bundle.enhanced_prompt == "BASE"

    def test_excluded_dataset_absent_from_tools_and_prompt(self, monkeypatch):
        # build_binding_tools が ([], []) を返せば tool もプロンプトも出ない
        # （同一 accepted_descriptors から両方生成する一貫性）。
        monkeypatch.setattr(pai_module.config, "ENABLE_DATASET_ANALYSIS", True)

        ds_service = Mock()
        ds_service.build_binding_tools.return_value = ([], [])
        comp = _component(self._db_with_binding(), ds_service)

        bundle = comp.prepare(
            "p1", "BASE", enable_kb=False, enable_dataset=True, principal=ADMIN
        )
        assert "analyze_dataset" not in bundle.enhanced_prompt
        assert bundle.additional_tools is None


# 両 Manager が Component を正しく配線していることの委譲スモークテスト。
MANAGER_CLASSES = [
    interview_manager.InterviewManager,
    agent_discussion_manager.AgentDiscussionManager,
]


@pytest.mark.parametrize("cls", MANAGER_CLASSES)
def test_constructor_accepts_dataset_analysis_service(cls):
    ds = Mock()
    mgr = cls(
        agent_service=Mock(), database_service=Mock(), dataset_analysis_service=ds
    )
    assert mgr.dataset_analysis_service is ds


@pytest.mark.parametrize("cls", MANAGER_CLASSES)
def test_manager_wires_agent_integration_component(cls):
    db, agent_svc, ds = Mock(), Mock(), Mock()
    mgr = cls(agent_service=agent_svc, database_service=db, dataset_analysis_service=ds)
    comp = mgr._agent_integration
    assert isinstance(comp, PersonaAgentIntegration)
    # Manager の各 Service が Component に注入されていること。
    assert comp._database_service is db
    assert comp._agent_service is agent_svc
    assert comp._dataset_analysis_service is ds
