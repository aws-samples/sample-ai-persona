"""discussion / interview Manager のデータセット統合（gate・dedup・build）テスト。

dedup（重複 binding の丸ごと除外）は Manager の責務であることを検証する。
"""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.models.dataset import Dataset, DatasetColumn, PersonaDatasetBinding
from src.managers import agent_discussion_manager, interview_manager

pytestmark = pytest.mark.unit

# 両 Manager で同一ロジックのため parametrize で網羅する。
# モジュール経由で参照し、import 形式を from ... import に統一する
# （同一モジュールの import / import from 混在を避ける）。
MANAGER_CLASSES = [
    interview_manager.InterviewManager,
    agent_discussion_manager.AgentDiscussionManager,
]


def _binding(persona_id, dataset_id, keys=None):
    return PersonaDatasetBinding(
        id=f"b-{dataset_id}-{hash(str(keys)) & 0xFFFF}",
        persona_id=persona_id,
        dataset_id=dataset_id,
        binding_keys=keys or {},
        created_at=datetime.now(),
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


def _make(cls, db, ds_service):
    return cls(
        agent_service=Mock(),
        database_service=db,
        dataset_analysis_service=ds_service,
    )


@pytest.mark.parametrize("cls", MANAGER_CLASSES)
class TestResolveDatasetBindings:
    def test_duplicate_dataset_excluded_wholesale(self, cls):
        db = Mock()
        db.get_bindings_by_persona.return_value = [
            _binding("p1", "ds-dup", {"user_id": "U1"}),
            _binding("p1", "ds-dup", {"user_id": "U2"}),  # 同 dataset に2 binding
            _binding("p1", "ds-ok", {"user_id": "U9"}),
        ]
        db.get_dataset.side_effect = _dataset
        mgr = _make(cls, db, Mock())

        accepted, datasets = mgr._resolve_dataset_bindings("p1")

        accepted_ids = {b["dataset_id"] for b in accepted}
        assert "ds-dup" not in accepted_ids  # 丸ごと除外
        assert "ds-ok" in accepted_ids
        assert {d.id for d in datasets} == {"ds-ok"}

    def test_unique_empty_binding_keys_allowed(self, cls):
        db = Mock()
        db.get_bindings_by_persona.return_value = [_binding("p1", "ds-all", {})]
        db.get_dataset.side_effect = _dataset
        mgr = _make(cls, db, Mock())

        accepted, datasets = mgr._resolve_dataset_bindings("p1")
        assert accepted == [{"dataset_id": "ds-all", "binding_keys": {}}]

    def test_no_bindings_returns_empty(self, cls):
        db = Mock()
        db.get_bindings_by_persona.return_value = []
        mgr = _make(cls, db, Mock())
        assert mgr._resolve_dataset_bindings("p1") == ([], [])


@pytest.mark.parametrize("cls", MANAGER_CLASSES)
class TestKillSwitchAndGate:
    def _db_with_binding(self):
        db = Mock()
        db.get_bindings_by_persona.return_value = [
            _binding("p1", "ds-ok", {"user_id": "U1"})
        ]
        db.get_dataset.side_effect = _dataset
        return db

    def test_enabled_both_flags_builds_tools_and_prompt(self, cls, monkeypatch):
        monkeypatch.setattr(interview_manager.config, "ENABLE_DATASET_ANALYSIS", True)
        monkeypatch.setattr(agent_discussion_manager.config, "ENABLE_DATASET_ANALYSIS", True)

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
        mgr = _make(cls, self._db_with_binding(), ds_service)

        sections, tool_groups = mgr._build_integration_sections(
            "p1", enable_kb=False, enable_dataset=True
        )
        ds_service.build_binding_tools.assert_called_once()
        assert any("analyze_dataset" in s for s in sections)
        assert any(g for g in tool_groups)

    def test_global_flag_off_skips_everything(self, cls, monkeypatch):
        monkeypatch.setattr(interview_manager.config, "ENABLE_DATASET_ANALYSIS", False)
        monkeypatch.setattr(agent_discussion_manager.config, "ENABLE_DATASET_ANALYSIS", False)

        ds_service = Mock()
        mgr = _make(cls, self._db_with_binding(), ds_service)

        sections, tool_groups = mgr._build_integration_sections(
            "p1", enable_kb=False, enable_dataset=True
        )
        ds_service.build_binding_tools.assert_not_called()
        assert sections == [] and all(not g for g in tool_groups)

    def test_session_flag_off_skips_everything(self, cls, monkeypatch):
        monkeypatch.setattr(interview_manager.config, "ENABLE_DATASET_ANALYSIS", True)
        monkeypatch.setattr(agent_discussion_manager.config, "ENABLE_DATASET_ANALYSIS", True)

        ds_service = Mock()
        mgr = _make(cls, self._db_with_binding(), ds_service)

        sections, tool_groups = mgr._build_integration_sections(
            "p1", enable_kb=False, enable_dataset=False
        )
        ds_service.build_binding_tools.assert_not_called()
        assert sections == []

    def test_excluded_dataset_absent_from_tools_and_prompt(self, cls, monkeypatch):
        # build_binding_tools が ([], []) を返せば tool もプロンプトも出ない
        # （同一 accepted_descriptors から両方生成する一貫性）。
        monkeypatch.setattr(interview_manager.config, "ENABLE_DATASET_ANALYSIS", True)
        monkeypatch.setattr(agent_discussion_manager.config, "ENABLE_DATASET_ANALYSIS", True)

        ds_service = Mock()
        ds_service.build_binding_tools.return_value = ([], [])
        mgr = _make(cls, self._db_with_binding(), ds_service)

        sections, tool_groups = mgr._build_integration_sections(
            "p1", enable_kb=False, enable_dataset=True
        )
        assert not any("analyze_dataset" in s for s in sections)
        assert all(not g for g in tool_groups)


@pytest.mark.parametrize("cls", MANAGER_CLASSES)
def test_constructor_accepts_dataset_analysis_service(cls):
    ds = Mock()
    mgr = cls(
        agent_service=Mock(), database_service=Mock(), dataset_analysis_service=ds
    )
    assert mgr.dataset_analysis_service is ds
