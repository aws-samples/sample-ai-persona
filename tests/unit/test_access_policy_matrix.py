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


# =============================================================================
# U5: ペルソナに付随するもの（記憶・紐付け・プレビュー・アバター・エクスポート・ファイル）
# security-design.md §1、BR2〜BR5、BR7、NFR1.31
# =============================================================================


def _u5_world():
    from tests.unit.persona_u5_world import World, make_persona

    world = World()
    owner = ACTORS["member"]
    persona = world.add_persona(make_persona("owner-1", ["sales"]))
    own = world.add_persona(make_persona(owner.sub))
    dedicated = world.add_dataset(owner_persona_id=own.id)
    from src.models.persona_binding import PersonaBinding

    for p in (persona, own):
        world.bindings.put(
            PersonaBinding.for_dataset(p.id, dedicated.id, dedicated=True)
        )
    world.personas[own.id] = own.with_avatar("s3://bucket/a.webp")
    world.personas[persona.id] = persona.with_avatar("s3://bucket/b.webp")
    world.s3.generate_presigned_url.return_value = "https://signed"
    return world, {"own": own.id, "shared": persona.id}, dedicated.id


def _memory_manager(world):
    from src.managers.persona_memory_manager import PersonaMemoryManager

    memory = world.memory
    memory.list_memories.return_value = []
    return PersonaMemoryManager(
        database_service=world.db, memory_service=memory, resource_lookup=world.lookup
    )


#: Actors of the U5 rows: the member is in ``sales``; the ``shared`` persona
#: belongs to someone else and is shared with ``sales``.
U5_ACTORS = {
    "member": ACTORS["member"],
    "admin": ACTORS["admin"],
    "no_role": ACTORS["no_role"],
    "stranger": Principal(sub="stranger-1", roles=frozenset({Role.MEMBER})),
}

#: (operation, actor, target, outcome)
U5_MATRIX: list[tuple[str, str, str, Outcome]] = []
for _op, _rows in {
    # 記憶は管理できる人だけ。共有されただけの人も 404（BR4.1）
    "memory": {"own": "ok", "shared": "404", "admin": "ok"},
    "bindings": {"own": "ok", "shared": "ok", "admin": "ok"},
    "bind": {"own": "ok", "shared": "403", "admin": "ok"},
    "preview": {"own": "ok", "shared": "ok", "admin": "ok"},
    "avatar_url": {"own": "ok", "shared": "ok", "admin": "ok"},
    "avatar_change": {"own": "ok", "shared": "403", "admin": "ok"},
    "export": {"own": "ok", "shared": "ok", "admin": "ok"},
}.items():
    U5_MATRIX += [
        (_op, "member", "own", _rows["own"]),
        (_op, "member", "shared", _rows["shared"]),
        (_op, "admin", "shared", _rows["admin"]),
        (_op, "stranger", "shared", "404"),
        (_op, "no_role", "shared", "404"),
    ]


def _u5_call(op: str, world, principal: Principal, persona_id: str, dataset_id: str):
    manager = world.manager()
    if op == "memory":
        return _memory_manager(world).get_memories(principal, persona_id)
    if op == "bindings":
        return manager.get_dataset_bindings(principal, persona_id)
    from dataclasses import replace

    # the dataset is dedicated to the persona under test (always usable, BR2.4)
    world.datasets[dataset_id] = replace(
        world.datasets[dataset_id], owner_persona_id=persona_id
    )
    if op == "bind":
        world.bindings.delete(persona_id, f"DATASET#{dataset_id}")
        return manager.create_dataset_binding(principal, persona_id, dataset_id)
    if op == "preview":
        return manager.preview_binding_data(principal, persona_id, dataset_id)
    if op == "avatar_url":
        return manager.get_avatar_url(principal, persona_id)
    if op == "avatar_change":
        return manager.delete_avatar(principal, persona_id)
    if op == "export":
        return manager.export_persona(principal, persona_id)
    raise AssertionError(op)


@pytest.mark.parametrize(
    ("op", "actor", "kind", "expected"),
    U5_MATRIX,
    ids=[f"{op}-{actor}-{kind}" for op, actor, kind, _ in U5_MATRIX],
)
def test_u5_persona_attachments(op, actor, kind, expected):
    world, personas, dataset_id = _u5_world()
    principal = U5_ACTORS[actor]
    try:
        _u5_call(op, world, principal, personas[kind], dataset_id)
    except AccessDeniedError:
        assert expected == "403"
        return
    except CodedError as e:
        assert e.code is ErrorCode.PERSONA_NOT_FOUND
        assert expected == "404"
        return
    assert expected == "ok"


#: Upload before attaching: only the uploader, only before expiry (BR5.4).
U5_FILE_ROWS: list[tuple[str, Outcome]] = [
    ("member", "ok"),
    ("admin", "404"),
    ("stranger", "404"),
    ("no_role", "404"),
]


@pytest.mark.parametrize(("actor", "expected"), U5_FILE_ROWS)
def test_u5_unattached_file(actor, expected):
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from src.managers.components.file_attachments import FileAttachments
    from src.models.uploaded_file import UploadedFile

    uploaded = UploadedFile.create_new(
        owner_sub=ACTORS["member"].sub,
        file_type="survey_image",
        original_filename="a.png",
        file_size=1,
        file_hash="h",
        mime_type="image/png",
        now=datetime.now(timezone.utc),
    )
    store = Mock()
    store.get.return_value = uploaded
    files = FileAttachments(store, None)
    allowed = files.can_read_file(U5_ACTORS[actor], uploaded.id)
    assert ("ok" if allowed else "404") == expected


@pytest.mark.parametrize(
    ("actor", "expected"), [("member", "ok"), ("admin", "ok"), ("stranger", "404")]
)
def test_u5_attached_file_before_parent_registration(actor, expected):
    """U7 がテンプレートの判定を登録するまでは、アップロードした本人と admin だけ（BR5.7）。

    U6 で議論の判定は登録した（下の ``test_u6_discussion_documents``）。
    """
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from src.managers.components.file_attachments import FileAttachments
    from src.models.uploaded_file import UploadedFile

    uploaded = UploadedFile.create_new(
        owner_sub=ACTORS["member"].sub,
        file_type="survey_image",
        original_filename="a.png",
        file_size=1,
        file_hash="h",
        mime_type="image/png",
        now=datetime.now(timezone.utc),
    ).attached_to("survey_template", "t1")
    store = Mock()
    store.get.return_value = uploaded
    files = FileAttachments(store, None)
    allowed = files.can_read_file(U5_ACTORS[actor], uploaded.id)
    assert ("ok" if allowed else "404") == expected


# =============================================================================
# U6: 議論（読む・管理する・実行する）・レポート・資料・インタビューのセッション
# security-design.md §1・§2、BR1〜BR5、BR8.1、NFR1.55、AC4.4.5
# =============================================================================

#: The member (``a``, ``sales``) sees: ``own`` (a's), ``shared`` (b's, shared
#: with ``sales``), ``unrelated`` (c's, not shared), ``deleted`` (no body).
U6_KINDS = ("own", "shared", "unrelated", "deleted")

#: (operation, actor, kind, outcome)
U6_MATRIX: list[tuple[str, str, str, Outcome]] = []
for _op, _member, _admin, _no_role in [
    # 読む: 共有された人も読める。無関係・削除済みは同じ 404
    ("read", ("ok", "ok", "404", "404"), ("ok", "ok", "404"), "404"),
    # 管理する（削除）: 共有された人は 403
    ("manage", ("ok", "403", "404", "404"), ("ok", "ok", "404"), "404"),
    # 実行する（インサイトの再生成）: ai:execute のあとに管理できる人
    ("run", ("ok", "403", "404", "404"), ("ok", "ok", "404"), "403"),
    # レポートの生成: ai:execute のあとに読める人（共有された人も生成できる）
    ("report_generate", ("ok", "ok", "404", "404"), ("ok", "ok", "404"), "403"),
    # レポートの保存: 管理できる人
    ("report_save", ("ok", "403", "404", "404"), ("ok", "ok", "404"), "404"),
    # 資料: 親の議論を読める人に配信（BR5.1）
    ("document", ("ok", "ok", "404", "404"), ("ok", "ok", "404"), "404"),
]:
    U6_MATRIX += [(_op, "member", k, o) for k, o in zip(U6_KINDS, _member)]
    U6_MATRIX += [
        (_op, "admin", k, o) for k, o in zip(("shared", "unrelated", "deleted"), _admin)
    ]
    U6_MATRIX.append((_op, "no_role", "own", _no_role))


def _u6_world():
    from unittest.mock import Mock

    from src.models.discussion import Discussion
    from src.models.message import Message
    from tests.unit.discussion_world import OWNER, PEER, STRANGER, World

    world = World()
    ids: dict[str, str] = {"deleted": "nonexistent"}
    for kind, saver in (("own", OWNER), ("shared", PEER), ("unrelated", STRANGER)):
        discussion = Discussion.create_new(
            topic=f"{kind}の議論",
            participants=["p1", "p2"],
            mode="classic",
            participant_names={"p1": "佐藤", "p2": "鈴木"},
        )
        saved = world.records.save_new(
            saver,
            discussion,
            [Message.create_new("p1", "佐藤", "十分に長い発言です" * 3)] * 2,
        )
        ids[kind] = saved.id
    world.share(ids["shared"], "sales")
    world.files = Mock()
    return world, ids


def _u6_principal(actor: str) -> Principal:
    from tests.unit.discussion_world import ADMIN, NO_ROLE, OWNER

    return {"member": OWNER, "admin": ADMIN, "no_role": NO_ROLE}[actor]


def _u6_call(op: str, world, principal: Principal, discussion_id: str) -> None:
    from unittest.mock import Mock

    from src.managers.discussion_manager import DiscussionManager
    from src.managers.report_manager import ReportManager

    ai = Mock()
    ai.extract_insights.return_value = [
        {
            "category": "c",
            "description": "十分に長いインサイトです",
            "confidence_score": 0.5,
        }
    ]
    ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(["r"])
    discussions = DiscussionManager(
        ai_service=ai,
        database_service=Mock(),
        file_attachments=Mock(),
        discussion_records=world.records,
        participants=world.participants,
        resource_sharing=world.sharing,
        owners=world.owners,
        s3_service=Mock(),
    )
    reports = ReportManager(
        ai_service=ai,
        agent_service=Mock(),
        database_service=Mock(),
        discussion_records=world.records,
        participants=world.participants,
    )
    if op == "read":
        discussions.get_discussion(principal, discussion_id)
    elif op == "manage":
        discussions.delete_discussion(principal, discussion_id)
    elif op == "run":
        discussions.regenerate_insights(principal, discussion_id)
    elif op == "report_generate":
        list(reports.generate_report_streaming(principal, discussion_id, "summary"))
    elif op == "report_save":
        reports.save_report(principal, discussion_id, "summary", "内容")
    elif op == "document":
        _u6_document(world, principal, discussion_id)
    else:
        raise AssertionError(op)


def _u6_document(world, principal: Principal, discussion_id: str) -> None:
    """A document attached to the discussion, read through the real wiring."""
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from src.managers.components.sharing_runtime import build_access_parts
    from src.models.uploaded_file import UploadedFile
    from tests.unit.discussion_world import AssetReader, DiscussionReader

    class _Readers:
        def reader(self, table):
            return {
                ResourceType.PERSONA: world.personas,
                ResourceType.DISCUSSION: DiscussionReader(world.store),
            }.get(table.resource_type, AssetReader())

    uploaded = UploadedFile.create_new(
        owner_sub="uploader",
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=1,
        file_hash="h",
        mime_type="application/pdf",
        now=datetime.now(timezone.utc),
    ).attached_to("discussion", discussion_id)
    store = Mock()
    store.get.return_value = uploaded
    files = build_access_parts(_Readers(), store, None).files
    files.readable_file(principal, uploaded.id)


@pytest.mark.parametrize(
    ("op", "actor", "kind", "expected"),
    U6_MATRIX,
    ids=[f"{op}-{actor}-{kind}" for op, actor, kind, _ in U6_MATRIX],
)
def test_u6_discussions(op, actor, kind, expected):
    world, ids = _u6_world()
    try:
        _u6_call(op, world, _u6_principal(actor), ids[kind])
    except AccessDeniedError:
        assert expected == "403"
        return
    except CodedError as e:
        assert e.code in (ErrorCode.DISCUSSION_NOT_FOUND, ErrorCode.FILE_NOT_FOUND)
        assert expected == "404"
        return
    assert expected == "ok"


#: Interview sessions belong to the one who started them; admin included (BR3.2).
U6_INTERVIEW_ROWS: list[tuple[str, str, Outcome]] = [
    ("owner", "live", "ok"),
    ("same_department", "live", "404"),
    ("admin", "live", "404"),
    ("no_role", "live", "404"),
    ("owner", "expired", "404"),
    ("owner", "missing", "404"),
]


@pytest.mark.parametrize(("actor", "state", "expected"), U6_INTERVIEW_ROWS)
def test_u6_interview_sessions(actor, state, expected):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock

    from src.managers.interview_manager import InterviewManager
    from src.models.interview_session import InterviewSession
    from tests.unit.discussion_world import ADMIN, NO_ROLE, OWNER, PEER, World

    start = datetime(2026, 10, 15, tzinfo=timezone.utc)
    now = start + (timedelta(hours=9) if state == "expired" else timedelta(hours=1))
    session = InterviewSession.create_new(
        owner_sub=OWNER.sub,
        persona_ids=["p1"],
        persona_names={"p1": "佐藤"},
        memory_mode="disabled",
        now=start,
    )
    store = Mock()
    store.get.return_value = None if state == "missing" else session
    store.query_messages.return_value = []
    world = World()
    manager = InterviewManager(
        agent_service=Mock(),
        database_service=Mock(),
        dataset_analysis_service=Mock(),
        binding_store=Mock(),
        session_store=store,
        discussion_records=world.records,
        participants=world.participants,
        file_attachments=world.files,
        resource_lookup=world.lookup,
        clock=lambda: now,
    )
    principal = {
        "owner": OWNER,
        "same_department": PEER,
        "admin": ADMIN,
        "no_role": NO_ROLE,
    }[actor]
    try:
        manager.get_interview_session(principal, session.id)
    except CodedError as e:
        assert e.code is ErrorCode.INTERVIEW_SESSION_NOT_FOUND
        assert expected == "404"
        return
    assert expected == "ok"
