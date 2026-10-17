"""U5 のペルソナの入口（記憶・紐付け・プレビュー・アバター・エクスポート・保存・詳細の出し分け）。

PersonaManager・PersonaMemoryManager・AccessPolicy・ResourceLookup は本物、
DynamoDB の紐付けは辞書の偽物（``tests/unit/persona_u5_world.py``）、候補は moto。
対応: BR1.3、BR2.1〜BR2.6、BR4.1〜BR4.4、BR7.1、NFR1.22〜NFR1.28、NFR1.32、
AC2.3.2、AC4.1.2、AC4.2.4、AC4.4.4、AC5.4.3、M8。
"""

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Iterator
from unittest.mock import Mock, patch

import pytest

from src.managers.persona_memory_manager import PersonaMemoryManager
from src.models.memory import MemoryEntry
from src.models.persona_binding import PersonaBinding
from tests.conftest import ADMIN, MEMBER_A, MEMBER_B, login_as
from tests.unit.persona_u5_world import World, make_persona

PEER_EMAIL = "peer@example.com"  # sales の member（共有された人）


class U5:
    """The world, the persona under test and the patched managers."""

    def __init__(self) -> None:
        self.world = World()
        self.world.sharing.department_names.side_effect = lambda ids: [
            {"sales": "営業部"}.get(i, i) for i in ids
        ]
        owners = Mock()
        owners.labels.side_effect = lambda subs: {s: f"氏名-{s}" for s in subs}
        self.manager = self.world.manager(owners=owners)
        memory = self.world.memory
        memory.list_memories.return_value = [
            MemoryEntry(
                id="m1",
                actor_id="x",
                session_id="d1",
                content="記憶の内容",
                created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                metadata={"strategy_type": "summary"},
            )
        ]
        memory.delete_memory.return_value = True
        memory.save_knowledge.return_value = "m2"
        memory.is_semantic_enabled = True
        self.memory_manager = PersonaMemoryManager(
            database_service=self.world.db,
            memory_service=memory,
            resource_lookup=self.world.lookup,
        )
        self.files = Mock()
        self.files.convert_file_to_markdown.return_value = "# 変換した知識"
        # member A のペルソナ。sales に共有する（PEER は読める、B は無関係）
        self.persona = self.world.add_persona(
            make_persona(MEMBER_A.sub, ["sales"], name="佐藤 花子")
        ).with_avatar("s3://bucket/persona_avatars/p/abcd-1234.webp")
        self.world.personas[self.persona.id] = self.persona
        self.world.s3.generate_presigned_url.return_value = "https://signed/a.webp"
        # このペルソナ専用のデータセットと、組織のデータセット（利用範囲なし）
        self.dedicated = self.world.add_dataset(
            owner_persona_id=self.persona.id, name="行動データ"
        )
        self.org_dataset = self.world.add_dataset(name="組織の購買データ")
        self.kb = self.world.add_kb()

    def bind(self, binding: PersonaBinding) -> None:
        self.world.bindings.put(binding)


@pytest.fixture
def u5() -> Iterator[U5]:
    state = U5()
    with (
        patch("web.routers.persona.get_persona_manager", return_value=state.manager),
        patch(
            "web.routers.persona.get_persona_memory_manager",
            return_value=state.memory_manager,
        ),
        patch("web.routers.persona.get_file_manager", return_value=state.files),
    ):
        yield state


def _as(client: Any, who: str) -> Any:
    if who == "owner":
        MEMBER_A.login(client)
    elif who == "peer":
        login_as(client, email=PEER_EMAIL, groups=("sales",))
    elif who == "stranger":
        MEMBER_B.login(client)
    elif who == "admin":
        ADMIN.login(client)
    else:  # pragma: no cover - test helper
        raise AssertionError(who)
    return client


# =============================================================================
# 記憶の 5 つの入口: 管理できる人だけ。共有された人・無関係は 404（BR4.1、AC4.2.4）
# =============================================================================


def _memory_requests(pid: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("get", f"/persona/{pid}/memories", {}),
        ("delete", f"/persona/{pid}/memories/m1", {}),
        ("delete", f"/persona/{pid}/memories", {}),
        (
            "post",
            f"/persona/{pid}/memories",
            {"data": {"topic_name": "好物", "topic_content": "ラーメン"}},
        ),
        (
            "post",
            f"/persona/{pid}/memories/upload-preview",
            {"files": {"file": ("知識.txt", BytesIO(b"text"), "text/plain")}},
        ),
    ]


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("who", ["peer", "stranger"])
def test_memory_endpoints_are_not_found_for_non_managers(client, u5, who, index):
    method, url, kwargs = _memory_requests(u5.persona.id)[index]
    response = getattr(_as(client, who), method)(url, **kwargs)

    assert response.status_code == 404
    u5.world.memory.delete_memory.assert_not_called()
    u5.world.memory.save_knowledge.assert_not_called()
    # 判定の前にファイルを変換しない（BR5.9）
    u5.files.convert_file_to_markdown.assert_not_called()


@pytest.mark.parametrize("index", range(5))
def test_memory_endpoints_work_for_the_owner(client, u5, index):
    method, url, kwargs = _memory_requests(u5.persona.id)[index]
    response = getattr(_as(client, "owner"), method)(url, **kwargs)

    assert response.status_code == 200


def test_knowledge_preview_converts_without_saving(client, u5):
    response = _as(client, "owner").post(
        f"/persona/{u5.persona.id}/memories/upload-preview",
        files={"file": ("知識.txt", BytesIO(b"text"), "text/plain")},
    )

    assert response.status_code == 200
    assert "変換した知識" in response.text
    u5.files.convert_file_to_markdown.assert_called_once_with(b"text", "知識.txt")


# =============================================================================
# 紐付け: 読める人は一覧、管理できる人だけ作成・削除（BR2.1〜BR2.5、BR4.2）
# =============================================================================


@pytest.mark.parametrize(
    ("who", "expected"), [("owner", 200), ("peer", 200), ("stranger", 404)]
)
@pytest.mark.parametrize("path", ["kb-binding", "dataset-bindings"])
def test_binding_lists_need_read_access(client, u5, who, expected, path):
    response = _as(client, who).get(f"/persona/{u5.persona.id}/{path}")
    assert response.status_code == expected


def test_shared_reader_sees_bindings_without_operations(client, u5):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.dedicated.id, dedicated=True))
    html = _as(client, "peer").get(f"/persona/{pid}/dataset-bindings").text

    assert "行動データ" in html
    assert "hx-delete=" not in html and "hx-post=" not in html
    # 見るだけのプレビューは出す（BR2.6）
    assert f"/persona/{pid}/dataset-bindings/{u5.dedicated.id}/preview" in html


def test_owner_sees_the_remove_operation_with_the_dataset_id(client, u5):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.dedicated.id, dedicated=True))
    html = _as(client, "owner").get(f"/persona/{pid}/dataset-bindings").text

    assert f'hx-delete="/persona/{pid}/dataset-bindings/{u5.dedicated.id}"' in html


def test_unusable_organization_asset_says_so_in_text(client, u5):
    """使えない組織資産には「利用権限なし」を文字で出す（BR2.7、AC5.3.3）"""
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.org_dataset.id))
    u5.bind(PersonaBinding.for_kb(pid, u5.kb.id))
    client = _as(client, "owner")

    datasets = client.get(f"/persona/{pid}/dataset-bindings").text
    kb = client.get(f"/persona/{pid}/kb-binding").text

    assert "組織の購買データ" in datasets and "〔利用権限なし〕" in datasets
    # 使えないものはプレビューを出さない
    assert f"dataset-bindings/{u5.org_dataset.id}/preview" not in datasets
    assert "社内KB" in kb and "〔利用権限なし〕" in kb


def test_kb_remove_url_has_no_binding_id(client, u5):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_kb(pid, u5.kb.id))
    html = _as(client, "admin").get(f"/persona/{pid}/kb-binding").text

    assert f'hx-delete="/persona/{pid}/kb-binding"' in html


@pytest.mark.parametrize(
    ("who", "expected"), [("owner", 200), ("peer", 403), ("stranger", 404)]
)
def test_create_dataset_binding(client, u5, who, expected):
    pid = u5.persona.id
    response = _as(client, who).post(
        f"/persona/{pid}/dataset-bindings", data={"dataset_id": u5.dedicated.id}
    )

    assert response.status_code == expected
    bound = (pid, f"DATASET#{u5.dedicated.id}") in u5.world.bindings.items
    assert bound is (expected == 200)


@pytest.mark.parametrize(
    ("who", "expected"), [("admin", 200), ("owner", 404), ("peer", 403)]
)
def test_create_kb_binding(client, u5, who, expected):
    """組織資産の紐付けは U7 まで admin だけ（BR2.3）。member には同じ 404"""
    pid = u5.persona.id
    response = _as(client, who).post(
        f"/persona/{pid}/kb-binding", data={"kb_id": u5.kb.id}
    )

    assert response.status_code == expected


@pytest.mark.parametrize(
    ("who", "expected"), [("owner", 200), ("peer", 403), ("stranger", 404)]
)
def test_delete_dataset_binding_by_dataset_id(client, u5, who, expected):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.org_dataset.id))
    response = _as(client, who).delete(
        f"/persona/{pid}/dataset-bindings/{u5.org_dataset.id}"
    )

    assert response.status_code == expected
    still_bound = (pid, f"DATASET#{u5.org_dataset.id}") in u5.world.bindings.items
    assert still_bound is (expected != 200)


@pytest.mark.parametrize(
    ("who", "expected"), [("admin", 200), ("peer", 403), ("stranger", 404)]
)
def test_delete_kb_binding(client, u5, who, expected):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_kb(pid, u5.kb.id))
    response = _as(client, who).delete(f"/persona/{pid}/kb-binding")

    assert response.status_code == expected


@pytest.mark.parametrize(
    ("who", "expected"), [("owner", 200), ("peer", 200), ("stranger", 404)]
)
def test_preview_of_a_dedicated_dataset(client, u5, who, expected):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.dedicated.id, dedicated=True))
    response = _as(client, who).get(
        f"/persona/{pid}/dataset-bindings/{u5.dedicated.id}/preview"
    )

    assert response.status_code == expected
    if expected == 200:
        assert "U1" in response.text


def test_preview_of_an_unusable_asset_shows_no_rows(client, u5):
    pid = u5.persona.id
    u5.bind(PersonaBinding.for_dataset(pid, u5.org_dataset.id))
    response = _as(client, "owner").get(
        f"/persona/{pid}/dataset-bindings/{u5.org_dataset.id}/preview"
    )

    assert "U1" not in response.text
    u5.world.preview.execute_dataset_preview.assert_not_called()


# =============================================================================
# アバター: 読めなければ 404 で、配信 URL へ移さない（BR4.4）
# =============================================================================


@pytest.mark.parametrize(
    ("who", "expected"), [("peer", 302), ("stranger", 404), ("admin", 302)]
)
def test_avatar_redirect_needs_read_access(client, u5, who, expected):
    response = _as(client, who).get(
        f"/persona/{u5.persona.id}/avatar", follow_redirects=False
    )

    assert response.status_code == expected
    assert ("location" in response.headers) is (expected == 302)


@pytest.mark.parametrize(("who", "expected"), [("peer", 403), ("stranger", 404)])
def test_avatar_change_needs_manage_access(client, u5, who, expected):
    pid = u5.persona.id
    client = _as(client, who)

    assert client.delete(f"/persona/{pid}/avatar").status_code == expected
    upload = client.post(
        f"/persona/{pid}/avatar",
        files={"file": ("a.png", BytesIO(b"png"), "image/png")},
    )
    assert upload.status_code == expected


# =============================================================================
# エクスポート（BR7.1、AC4.1.2）
# =============================================================================


@pytest.mark.parametrize("who", ["owner", "peer", "admin"])
def test_export_is_a_markdown_download_for_readers(client, u5, who):
    response = _as(client, who).get(f"/persona/{u5.persona.id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # 日本語の名前は RFC 5987 の形で渡す
    assert "filename*=UTF-8''" in disposition
    assert "%E4%BD%90%E8%97%A4" in disposition  # 佐藤
    assert "# 佐藤 花子" in response.text
    # 記憶・紐付け・所有者は入れない（BR7.2）
    assert MEMBER_A.sub not in response.text


def test_export_of_an_unreadable_persona_is_not_found(client, u5):
    response = _as(client, "stranger").get(f"/persona/{u5.persona.id}/export")
    assert response.status_code == 404


# =============================================================================
# 詳細の出し分け（BR4.2、M8）
# =============================================================================


def test_shared_reader_detail_hides_memory_and_avatar_change(client, u5):
    html = _as(client, "peer").get(f"/persona/{u5.persona.id}").text

    assert "memory-section-content" not in html
    assert f"/persona/{u5.persona.id}/memories" not in html
    assert "画像を変更" not in html
    assert f'href="/persona/{u5.persona.id}/export"' in html


def test_owner_detail_has_memory_avatar_change_and_export(client, u5):
    html = _as(client, "owner").get(f"/persona/{u5.persona.id}").text

    assert "memory-section-content" in html
    assert "画像を変更" in html
    assert f'href="/persona/{u5.persona.id}/export"' in html


def test_delete_confirmation_names_what_goes_with_it(client, u5):
    """削除の確認に一緒に消えるものを出す（BR3.5、M8）"""
    html = _as(client, "owner").get(f"/persona/{u5.persona.id}").text

    assert 'id="persona-delete-dialog"' in html
    assert "データセット・ナレッジベースの紐付け" in html
    assert "このペルソナの長期記憶" in html
    assert "共有先のグループからも見えなくなります" in html
    # ブラウザの confirm() ではなく共通ダイアログで確かめる
    assert 'hx-confirm="ペルソナ「佐藤 花子」を削除しますか？"' not in html
    assert 'data-dialog-open="persona-delete-dialog"' in html


def test_delete_confirmation_of_an_unshared_persona_has_no_share_line(client, u5):
    world = u5.world
    unshared = world.add_persona(make_persona(MEMBER_A.sub, name="未共有"))
    html = _as(client, "owner").get(f"/persona/{unshared.id}").text

    assert "このペルソナの長期記憶" in html
    assert "共有先のグループからも見えなくなります" not in html


# =============================================================================
# 新しい変更の入口は CSRF の印（HX-Request）がなければ断る（NFR3.11）
# =============================================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("delete", "/persona/p1/kb-binding"),
        ("delete", "/persona/p1/dataset-bindings/d1"),
        ("post", "/persona/p1/dataset-bindings"),
        ("post", "/persona/save"),
        ("post", "/persona/save-selected"),
    ],
)
def test_new_changes_need_the_csrf_header(test_app, method, path):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as no_header:
        MEMBER_A.login(no_header)
        response = getattr(no_header, method)(path)

    assert response.status_code == 403


# =============================================================================
# 候補の保存: 本人の候補だけ。他人・期限切れ・二重送信は同じトースト（BR1.3、AC2.3.2）
# =============================================================================

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

REGION = "ap-northeast-1"
TABLE = "PersonaCandidatesApiTest"


class Saving:
    """A real PersonaGenerationManager over moto, and the U5 PersonaManager."""

    def __init__(self, client: Any) -> None:
        from src.managers.persona_generation_manager import PersonaGenerationManager
        from src.services.persona_candidate_store import PersonaCandidateStore

        self.store = PersonaCandidateStore(
            region=REGION, table_name=TABLE, client=client
        )
        self.gen = PersonaGenerationManager(
            agent_service=Mock(),
            database_service=Mock(),
            dataset_analysis_service=Mock(),
            candidate_store=self.store,
            s3_service=None,
        )
        self.world = World()
        self.manager = self.world.manager()

    def candidate(self, owner: str, *, age_hours: float = 0, name: str = "候補") -> str:
        from src.models.persona_candidate import PersonaCandidate

        persona = make_persona(None, name=name)
        candidate = PersonaCandidate.from_persona(
            persona,
            owner_sub=owner,
            generation_id="gen-1",
            ttl_seconds=8 * 3600,
            now=datetime.now(timezone.utc) - timedelta(hours=age_hours),
            generation_log=[{"type": "thinking", "content": "考え中"}],
            generation_context={"data_type": "interview", "persona_count": 1},
        )
        self.store.put_all([candidate])
        return candidate.candidate_id


@pytest.fixture
def saving(monkeypatch) -> Iterator[Saving]:
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "candidate_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "candidate_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        state = Saving(ddb)
        with (
            patch(
                "web.routers.persona.get_persona_generation_manager",
                return_value=state.gen,
            ),
            patch(
                "web.routers.persona.get_persona_manager", return_value=state.manager
            ),
        ):
            yield state


_FORM = {
    "name": "保存する名前",
    "age": 30,
    "occupation": "職業",
    "background": "背景",
    "values": "v",
    "pain_points": "p",
    "goals": "g",
}


def _toast(response: Any) -> str:
    return str(json.loads(response.headers["HX-Trigger"])["showToast"]["message"])


def test_own_candidate_is_saved_under_a_new_id(client, saving):
    cid = saving.candidate(MEMBER_A.sub)
    response = _as(client, "owner").post(
        "/persona/save", data={**_FORM, "persona_id": cid}
    )

    assert response.status_code == 200
    (saved,) = saving.world.personas.values()
    assert saved.id != cid and saved.owner_sub == MEMBER_A.sub
    assert saved.name == "保存する名前"
    # 保存できた候補は消える
    assert "Item" not in saving.store.client.get_item(
        TableName=TABLE, Key={"candidate_id": {"S": cid}}
    )


def test_someone_elses_expired_and_double_submit_get_the_same_toast(client, saving):
    other = saving.candidate(MEMBER_B.sub)
    expired = saving.candidate(MEMBER_A.sub, age_hours=9)
    mine = saving.candidate(MEMBER_A.sub)
    client = _as(client, "owner")

    def save(cid: str) -> Any:
        return client.post("/persona/save", data={**_FORM, "persona_id": cid})

    assert save(mine).status_code == 200
    answers = [save(other), save(expired), save(mine)]

    assert {r.status_code for r in answers} == {404}
    assert len({_toast(r) for r in answers}) == 1
    assert "期限切れ" in _toast(answers[0])
    # 他人の候補は残り、本人以外の保存は起きていない
    assert len(saving.world.personas) == 1
    assert "Item" in saving.store.client.get_item(
        TableName=TABLE, Key={"candidate_id": {"S": other}}
    )


def test_bulk_save_reports_the_count(client, saving):
    ids = [saving.candidate(MEMBER_A.sub, name=f"候補{i}") for i in range(2)]
    response = _as(client, "owner").post(
        "/persona/save-selected", data={"persona_ids": ",".join(ids)}
    )

    assert response.status_code == 200
    assert "2人のペルソナを保存しました" in response.text
    assert {p.name for p in saving.world.personas.values()} == {"候補0", "候補1"}


def test_bulk_save_with_someone_elses_candidate_saves_nothing(client, saving):
    mine = saving.candidate(MEMBER_A.sub)
    other = saving.candidate(MEMBER_B.sub)
    response = _as(client, "owner").post(
        "/persona/save-selected", data={"persona_ids": f"{mine},{other}"}
    )

    assert response.status_code == 404
    assert "期限切れ" in _toast(response)
    assert saving.world.personas == {}
    # 押さえた分は外してある（保存し直せる）
    again = client.post("/persona/save", data={**_FORM, "persona_id": mine})
    assert again.status_code == 200


def test_member_owner_without_choices_is_not_sent_to_the_settings_screen(client, u5):
    """member の所有者に、開けない設定画面（/settings）への導線を出さない（U7 BR3.2）"""
    pid = u5.persona.id
    html = _as(client, "owner").get(f"/persona/{pid}/dataset-bindings").text

    assert 'href="/settings"' not in html
    assert "管理者に依頼" in html
