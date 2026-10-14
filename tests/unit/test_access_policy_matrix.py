"""判定表テスト（functional-spec.md WF7、AC4.4.5、約束 C2 の提供側のテスト）。

行は「ロール（admin / member / ロールなし）× 対象（自分のもの / 共有された / 無関係 /
所有者なし / 削除済み / 組織資産で利用範囲内 / 範囲外）」、列は「読む / 管理する /
資産を使う / 操作の権限」。NFR1.7 の「判定の入力が欠けている・おかしい」行も持つ。
外部のモックを使わない（NFR9.13）。
"""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.managers.components.access_policy import AccessPolicy
from src.managers.components.resource_sharing import ResourceSharing
from src.models.sharing import ShareChange
from src.models.authz import (
    AccessTarget,
    Department,
    Permission,
    Principal,
    ResourceType,
    Role,
    permissions_of,
)
from src.models.errors import AccessDeniedError, CodedError, ErrorCode

Outcome = Literal["ok", "404", "403", "-"]

ACTORS: dict[str, Principal] = {
    "member": Principal(
        sub="member-1", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
    ),
    "admin": Principal(sub="admin-1", roles=frozenset({Role.ADMIN})),
    "no_role": Principal(sub="norole-1", group_ids=frozenset({"sales"})),
}


def _target(actor: Principal, kind: str) -> AccessTarget:
    """The seven targets of WF7, seen from ``actor``."""
    persona = ResourceType.PERSONA
    asset = ResourceType.KNOWLEDGE_BASE
    if kind == "own":
        return AccessTarget(persona, "r", owner_sub=actor.sub)
    if kind == "shared":
        return AccessTarget(
            persona, "r", owner_sub="someone", shared_group_ids=frozenset({"sales"})
        )
    if kind == "unrelated":
        return AccessTarget(
            persona, "r", owner_sub="someone", shared_group_ids=frozenset({"hr"})
        )
    if kind == "ownerless":
        return AccessTarget(
            persona, "r", owner_sub=None, shared_group_ids=frozenset({"hr"})
        )
    if kind == "deleted":
        return AccessTarget(persona, "r", owner_sub=actor.sub, exists=False)
    if kind == "asset_in":
        return AccessTarget(asset, "r", shared_group_ids=frozenset({"sales"}))
    if kind == "asset_out":
        return AccessTarget(asset, "r", shared_group_ids=frozenset({"hr"}))
    raise AssertionError(kind)


# (actor, target, read, manage, use_asset)。"-" は表の「—」（その列を見ない）。
# admin の「共有された」は admin が部署を持たないため read_all で読める。
MATRIX: list[tuple[str, str, Outcome, Outcome, Outcome]] = [
    ("member", "own", "ok", "ok", "-"),
    ("member", "shared", "ok", "403", "-"),
    ("member", "unrelated", "404", "404", "-"),
    ("member", "ownerless", "404", "404", "-"),
    ("member", "deleted", "404", "404", "404"),
    ("member", "asset_in", "-", "-", "ok"),
    ("member", "asset_out", "-", "-", "404"),
    ("admin", "own", "ok", "ok", "ok"),
    ("admin", "shared", "ok", "ok", "ok"),
    ("admin", "unrelated", "ok", "ok", "ok"),
    ("admin", "ownerless", "ok", "ok", "ok"),
    ("admin", "deleted", "404", "404", "404"),
    ("admin", "asset_in", "ok", "ok", "ok"),
    ("admin", "asset_out", "ok", "ok", "ok"),
    ("no_role", "own", "404", "404", "404"),
    ("no_role", "shared", "404", "404", "404"),
    ("no_role", "unrelated", "404", "404", "404"),
    ("no_role", "ownerless", "404", "404", "404"),
    ("no_role", "deleted", "404", "404", "404"),
    ("no_role", "asset_in", "404", "404", "404"),
    ("no_role", "asset_out", "404", "404", "404"),
]


def _ids(rows: list[tuple]) -> list[str]:
    return [f"{row[0]}-{row[1]}" for row in rows]


def _ensure_outcome(func, principal: Principal, target: AccessTarget) -> Outcome:
    try:
        func(principal, target, ErrorCode.PERSONA_NOT_FOUND)
    except AccessDeniedError:
        return "403"
    except CodedError as e:
        assert e.code is ErrorCode.PERSONA_NOT_FOUND
        return "404"
    return "ok"


def _column(index: int) -> list[tuple[str, str, Outcome]]:
    """Rows of one column, leaving out the cells the table marks "—"."""
    return [(row[0], row[1], row[index]) for row in MATRIX if row[index] != "-"]


READ_ROWS = _column(2)
MANAGE_ROWS = _column(3)
ASSET_ROWS = _column(4)


@pytest.mark.parametrize(("actor", "kind", "expected"), READ_ROWS, ids=_ids(READ_ROWS))
def test_read(actor, kind, expected):
    principal = ACTORS[actor]
    target = _target(principal, kind)
    assert AccessPolicy.can_read(principal, target) is (expected == "ok")
    assert _ensure_outcome(AccessPolicy.ensure_readable, principal, target) == expected


@pytest.mark.parametrize(
    ("actor", "kind", "expected"), MANAGE_ROWS, ids=_ids(MANAGE_ROWS)
)
def test_manage(actor, kind, expected):
    principal = ACTORS[actor]
    target = _target(principal, kind)
    assert AccessPolicy.can_manage(principal, target) is (expected == "ok")
    assert (
        _ensure_outcome(AccessPolicy.ensure_manageable, principal, target) == expected
    )


@pytest.mark.parametrize(
    ("actor", "kind", "expected"), ASSET_ROWS, ids=_ids(ASSET_ROWS)
)
def test_use_asset(actor, kind, expected):
    principal = ACTORS[actor]
    allowed = AccessPolicy.can_use_asset(principal, _target(principal, kind))
    assert allowed is (expected == "ok")


PERMISSION_ROWS: list[tuple[str, Permission, bool]] = [
    ("member", Permission.AI_EXECUTE, True),
    ("member", Permission.SETTINGS_MANAGE, False),
    ("admin", Permission.AI_EXECUTE, True),
    ("admin", Permission.SETTINGS_MANAGE, True),
    ("no_role", Permission.AI_EXECUTE, False),
    ("no_role", Permission.SETTINGS_MANAGE, False),
]


@pytest.mark.parametrize(
    ("actor", "permission", "allowed"),
    PERMISSION_ROWS,
    ids=[f"{a}-{p.value}" for a, p, _ in PERMISSION_ROWS],
)
def test_operation_permission(actor, permission, allowed):
    principal = ACTORS[actor]
    if allowed:
        AccessPolicy.require(principal, permission)
    else:
        with pytest.raises(AccessDeniedError):
            AccessPolicy.require(principal, permission)


# NFR1.7: 判定の入力が欠けている・おかしいときは、読む・管理する・資産を使うのすべてが不可。
_EMPTY_SUB_MEMBER = Principal(
    sub="", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"x"})
)
MALFORMED: list[tuple[str, Principal, AccessTarget]] = [
    (
        "empty-sub",
        _EMPTY_SUB_MEMBER,
        AccessTarget(
            ResourceType.PERSONA, "r", owner_sub="", shared_group_ids=frozenset({"x"})
        ),
    ),
    (
        "owner-none-and-sub-empty",
        _EMPTY_SUB_MEMBER,
        AccessTarget(ResourceType.PERSONA, "r", owner_sub=None),
    ),
    (
        "owner-empty",
        Principal(sub="member-1", roles=frozenset({Role.MEMBER})),
        AccessTarget(ResourceType.PERSONA, "r", owner_sub=""),
    ),
    (
        "no-role",
        Principal(sub="member-1", group_ids=frozenset({"sales"})),
        AccessTarget(
            ResourceType.PERSONA,
            "r",
            owner_sub="member-1",
            shared_group_ids=frozenset({"sales"}),
        ),
    ),
    (
        "unknown-role-only",
        Principal(
            sub="member-1",
            roles=frozenset({"owner"}),  # type: ignore[arg-type]
            group_ids=frozenset({"sales"}),
        ),
        AccessTarget(
            ResourceType.PERSONA,
            "r",
            owner_sub="member-1",
            shared_group_ids=frozenset({"sales"}),
        ),
    ),
    (
        "no-groups-and-no-shares",
        Principal(sub="member-1", roles=frozenset({Role.MEMBER})),
        AccessTarget(ResourceType.KNOWLEDGE_BASE, "r", owner_sub="someone"),
    ),
]


@pytest.mark.parametrize(
    ("name", "principal", "target"), MALFORMED, ids=[m[0] for m in MALFORMED]
)
def test_malformed_inputs_allow_nothing(name, principal, target):
    assert not AccessPolicy.can_read(principal, target)
    assert not AccessPolicy.can_manage(principal, target)
    assert not AccessPolicy.can_use_asset(principal, target)
    assert _ensure_outcome(AccessPolicy.ensure_readable, principal, target) == "404"


# 境界の補助（hypothesis）: どんな入力でも、管理できるなら読める。権限のない人は何も許されない。
_subs = st.sampled_from(["", "member-1", "someone"])
_groups = st.frozensets(st.sampled_from(["sales", "dev", "hr"]))
_principals = st.builds(
    Principal,
    sub=_subs,
    roles=st.frozensets(st.sampled_from(list(Role))),
    group_ids=_groups,
)
_targets = st.builds(
    AccessTarget,
    resource_type=st.sampled_from(list(ResourceType)),
    resource_id=st.just("r"),
    owner_sub=st.one_of(st.none(), _subs),
    shared_group_ids=_groups,
    exists=st.booleans(),
)


@given(principal=_principals, target=_targets)
def test_manage_implies_read(principal, target):
    if AccessPolicy.can_manage(principal, target):
        assert AccessPolicy.can_read(principal, target)


@given(principal=_principals, target=_targets)
def test_nothing_is_allowed_without_permissions_or_sub(principal, target):
    if not permissions_of(principal.roles) or not principal.sub:
        assert not AccessPolicy.can_read(principal, target)
        assert not AccessPolicy.can_manage(principal, target)
        assert not AccessPolicy.can_use_asset(principal, target)


@given(principal=_principals, target=_targets)
def test_deleted_targets_are_never_allowed(principal, target):
    if not target.exists:
        assert not AccessPolicy.can_read(principal, target)
        assert not AccessPolicy.can_manage(principal, target)
        assert not AccessPolicy.can_use_asset(principal, target)


# =============================================================================
# 共有先の変更（U4、ResourceSharing 経由。ロール × 関係 × 1 件・一括）
# 「sales を足す」を、上の表と同じ 5 つの関係のペルソナに当てる。
# 結果: ok / 404 / 403 / owner（所有者なしは共有できない、BR1.10）/
#       no_dept（選べる部署がない、BR1.4。どの対象も読まない）
# =============================================================================

SHARE_ROWS: list[tuple[str, str, str]] = [
    ("member", "own", "ok"),
    ("member", "shared", "403"),
    ("member", "unrelated", "404"),
    ("member", "ownerless", "404"),
    ("member", "deleted", "404"),
    ("admin", "own", "ok"),
    ("admin", "shared", "ok"),
    ("admin", "unrelated", "ok"),
    ("admin", "ownerless", "owner"),
    ("admin", "deleted", "404"),
    ("no_role", "own", "no_dept"),
    ("no_role", "shared", "no_dept"),
    ("no_role", "unrelated", "no_dept"),
    ("no_role", "ownerless", "no_dept"),
    ("no_role", "deleted", "no_dept"),
]

_SHARE_CODES: dict[ErrorCode, str] = {
    ErrorCode.PERSONA_NOT_FOUND: "404",
    ErrorCode.ACCESS_DENIED: "403",
    ErrorCode.SHARE_OWNER_MISSING: "owner",
    ErrorCode.SHARE_NO_DEPARTMENT: "no_dept",
}


class _Departments:
    """The department list (no Cognito, NFR9.13)."""

    def list_all(self) -> list[Department]:
        return [Department("sales", "営業部"), Department("hr", "人事部")]

    def get_many(self, group_ids: frozenset[str]) -> dict[str, Department]:
        return {d.group_id: d for d in self.list_all() if d.group_id in group_ids}


def _sharing_with(actor: Principal, kind: str) -> tuple[ResourceSharing, str]:
    from tests.unit.test_resource_sharing import TABLE, FakeStore

    store = FakeStore()
    target = _target(actor, kind)
    if target.exists:
        store.add("r", target.owner_sub, set(target.shared_group_ids))
    sharing = ResourceSharing(store, _Departments(), sleep=lambda _s: None)
    sharing.register(TABLE)
    return sharing, "r"


_ADD_SALES = ShareChange(add=frozenset({"sales"}))


@pytest.mark.parametrize(
    ("actor", "kind", "expected"), SHARE_ROWS, ids=_ids(SHARE_ROWS)
)
def test_share_one(actor, kind, expected):
    principal = ACTORS[actor]
    sharing, rid = _sharing_with(principal, kind)
    try:
        sharing.update_shares(
            principal, AccessTarget(ResourceType.PERSONA, rid), _ADD_SALES
        )
        outcome = "ok"
    except CodedError as e:
        outcome = _SHARE_CODES[e.code]
    assert outcome == expected


@pytest.mark.parametrize(
    ("actor", "kind", "expected"), SHARE_ROWS, ids=_ids(SHARE_ROWS)
)
def test_share_bulk(actor, kind, expected):
    principal = ACTORS[actor]
    sharing, rid = _sharing_with(principal, kind)
    try:
        result = sharing.bulk_update_shares(
            principal, [AccessTarget(ResourceType.PERSONA, rid)], _ADD_SALES
        )
    except CodedError as e:  # 全体の確認（BR2.2）で断られた
        assert _SHARE_CODES[e.code] == expected
        return
    if expected == "ok":
        assert result.updated == [rid] and result.failed == []
        return
    (failure,) = result.failed
    assert _SHARE_CODES[failure.code] == expected
    # 読めない対象の名前は出さない（BR2.4）
    assert (failure.display_name is None) is (expected == "404")
