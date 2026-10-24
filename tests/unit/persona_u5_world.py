"""U5 の PersonaManager のテストで共有する偽物（unit-test-instructions.md「テストのデータ」）。

- 部署は sales と plan。member A は sales、``SALES_PEER``（共有された人）も sales、
  member C は plan、admin は所属なし
- ペルソナは member A の 2 件（共有なし、sales に共有）、member C の 1 件、
  ``owner_sub`` のない旧データ 1 件
- 紐付けの Store は辞書の偽物、ResourceLookup は本物（読み手だけ偽物）、
  AccessPolicy は本物
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Sequence
from unittest.mock import Mock

from src.services.dataset_store import DatasetStore
from src.services.knowledge_base_store import KnowledgeBaseStore
from src.managers.components.resource_lookup import ResourceLookup
from src.managers.persona_manager import PersonaManager
from src.models.authz import AccessTarget, Principal, ResourceType, Role
from src.models.dataset import Dataset, DatasetColumn
from src.models.knowledge_base import KnowledgeBase
from src.models.memory import DeleteAllResult
from src.models.persona import Persona
from src.models.persona_binding import PersonaBinding
from tests.conftest import ADMIN, MEMBER_A, MEMBER_B

NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)

A = MEMBER_A.principal()
#: Someone in ``sales``: the persona shared with ``sales`` is readable to them.
SALES_PEER = Principal(
    "local:peer@example.com", frozenset({Role.MEMBER}), frozenset({"sales"})
)
#: Unrelated member (``dev``).
B = MEMBER_B.principal()
MEMBER_C = Principal(
    "local:c@example.com", frozenset({Role.MEMBER}), frozenset({"plan"})
)
ADMIN_P = ADMIN.principal()


def make_persona(
    owner: str | None, shared: Sequence[str] = (), **kwargs: Any
) -> Persona:
    base = Persona.create_new(
        name=kwargs.pop("name", "佐藤 花子"),
        age=34,
        occupation="会社員",
        background="東京で働く",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
        tags=["都市"],
    )
    return replace(base, owner_sub=owner, shared_group_ids=frozenset(shared), **kwargs)


class FakeBindingStore:
    """Dictionary-backed PersonaBindingStore."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], PersonaBinding] = {}
        self.fail_list = False
        self.fail_delete_all = False
        self.fail_put = False

    def list_for_persona(self, persona_id: str) -> list[PersonaBinding]:
        if self.fail_list:
            raise RuntimeError("bindings down")
        return [b for (pid, _), b in sorted(self.items.items()) if pid == persona_id]

    def put(self, binding: PersonaBinding) -> None:
        if self.fail_put:
            raise RuntimeError("bindings down")
        self.items[(binding.persona_id, binding.target_key)] = binding

    def delete(self, persona_id: str, target_key: str) -> bool:
        return self.items.pop((persona_id, target_key), None) is not None

    def delete_all(
        self, persona_id: str, bindings: Sequence[PersonaBinding] | None = None
    ) -> int:
        if self.fail_delete_all:
            raise RuntimeError("bindings down")
        keys = [k for k in self.items if k[0] == persona_id]
        for k in keys:
            del self.items[k]
        return len(keys)


class _Reader:
    def __init__(self, world: "World", kind: ResourceType) -> None:
        self.world = world
        self.kind = kind

    def read(self, ids: Sequence[str], deadline: float) -> dict[str, AccessTarget]:
        found: dict[str, AccessTarget] = {}
        for i in ids:
            if self.kind is ResourceType.PERSONA and (p := self.world.personas.get(i)):
                found[i] = AccessTarget(
                    self.kind, i, p.owner_sub, p.shared_group_ids, p.name
                )
            elif self.kind is ResourceType.DATASET and (
                d := self.world.datasets.get(i)
            ):
                found[i] = AccessTarget(
                    self.kind, i, None, self.world.scopes.get(i, frozenset()), d.name
                )
            elif self.kind is ResourceType.KNOWLEDGE_BASE and (
                k := self.world.kbs.get(i)
            ):
                found[i] = AccessTarget(
                    self.kind, i, None, self.world.scopes.get(i, frozenset()), k.name
                )
        return found


class World:
    """Personas, datasets, knowledge bases and the parts PersonaManager needs."""

    def __init__(self) -> None:
        self.personas: dict[str, Persona] = {}
        self.datasets: dict[str, Dataset] = {}
        self.kbs: dict[str, KnowledgeBase] = {}
        #: Usage scope of organization assets (U7 adds the column).
        self.scopes: dict[str, frozenset[str]] = {}
        self.bindings = FakeBindingStore()
        self.db = Mock()
        self.db.get_persona.side_effect = (
            lambda pid, consistent=False: self.personas.get(pid)
        )
        self.db.save_persona.side_effect = self._save_persona
        self.dataset_store = Mock(spec=DatasetStore)
        self.dataset_store.get.side_effect = (
            lambda did, consistent=False: self.datasets.get(did)
        )
        self.dataset_store.put_new.side_effect = self._save_dataset
        self.dataset_store.delete.side_effect = lambda did: self.datasets.pop(did, None)
        self.dataset_store.list_org_assets.side_effect = lambda: [
            d for d in self.datasets.values() if not d.owner_persona_id
        ]
        self.kb_store = Mock(spec=KnowledgeBaseStore)
        self.kb_store.list_all.side_effect = lambda: list(self.kbs.values())
        self.kb_store.get.side_effect = lambda kid, consistent=False: self.kbs.get(kid)
        self.db.update_persona_avatar.return_value = True
        self.s3 = Mock()
        self.s3.path_of.side_effect = lambda key: f"s3://bucket/{key}"
        self.s3.key_of.side_effect = lambda path: path.removeprefix("s3://bucket/")
        self.s3.copy_object.side_effect = lambda src, dst: f"s3://bucket/{dst}"
        self.s3.upload_file.side_effect = (
            lambda content, key, **kw: f"s3://bucket/{key}"
        )
        self.sharing = Mock()
        self.sharing.delete_with_shares.side_effect = self._delete_with_shares
        self.memory = Mock()
        self.memory.delete_all.return_value = DeleteAllResult(
            deleted=2, remaining=0, timed_out=False
        )
        self.preview = Mock()
        self.preview.execute_dataset_preview.return_value = (1, ["user_id"], [["U1"]])
        #: Every hold is still the request's (R-04); tests that take one over
        #: pass a real-condition fake instead.
        self.candidates = Mock()
        self.candidates.confirm_claim.return_value = True
        self.candidates.mark_saved.return_value = True
        self.lookup = ResourceLookup(
            {kind: _Reader(self, kind) for kind in ResourceType}, sample_rate=0
        )

    # --- data ----------------------------------------------------------------

    def add_persona(self, persona: Persona) -> Persona:
        self.personas[persona.id] = persona
        return persona

    def add_dataset(
        self, owner_persona_id: str | None = None, scope: Sequence[str] = (), **kw: Any
    ) -> Dataset:
        dataset = Dataset.create_new(
            name=kw.pop("name", "購買データ"),
            description="",
            s3_path=kw.pop("s3_path", "s3://bucket/datasets/x.csv"),
            columns=[DatasetColumn(name="user_id", data_type="string")],
            row_count=3,
            owner_persona_id=owner_persona_id,
        )
        self.datasets[dataset.id] = dataset
        self.scopes[dataset.id] = frozenset(scope)
        return dataset

    def add_kb(self, scope: Sequence[str] = ()) -> KnowledgeBase:
        kb = KnowledgeBase.create_new("KB123", "社内KB", "説明")
        self.kbs[kb.id] = kb
        self.scopes[kb.id] = frozenset(scope)
        return kb

    def _save_persona(self, persona: Persona) -> str:
        self.personas[persona.id] = persona
        return persona.id

    def _save_dataset(self, dataset: Dataset) -> None:
        self.datasets[dataset.id] = dataset

    def _delete_with_shares(
        self, principal: Principal, rtype: ResourceType, pid: str
    ) -> dict[str, Any] | None:
        from src.managers.components.access_policy import AccessPolicy
        from src.models.errors import ErrorCode

        persona = self.personas.get(pid)
        target = (
            AccessTarget(rtype, pid, persona.owner_sub, persona.shared_group_ids)
            if persona
            else AccessTarget.missing(rtype, pid)
        )
        AccessPolicy.ensure_manageable(principal, target, ErrorCode.PERSONA_NOT_FOUND)
        assert persona is not None
        del self.personas[pid]
        body: dict[str, Any] = {"id": pid}
        if persona.avatar_path:
            body["avatar_path"] = persona.avatar_path
        return body

    # --- the manager -----------------------------------------------------------

    def manager(self, **overrides: Any) -> PersonaManager:
        options: dict[str, Any] = {
            "database_service": self.db,
            "knowledge_base_store": self.kb_store,
            "dataset_store": self.dataset_store,
            "s3_service": self.s3,
            "resource_sharing": self.sharing,
            "owners": Mock(),
            "binding_store": self.bindings,
            "resource_lookup": self.lookup,
            "memory_service": self.memory,
            "preview_service": self.preview,
            "candidate_store": self.candidates,
        }
        options.update(overrides)
        return PersonaManager(**options)
