"""共有の入口（/share/...）の API テスト（U4、M7）。

Manager と Component は本物にし、Store はメモリ上の偽物、部署の一覧は差し替える。
利用者は AUTH_MODE=local のログイン（login_as）で作る。対応: BR1.1、BR1.4、
BR2.1〜BR2.4、BR7.3、NFR1.10、NFR1.14、NFR4.11、AC3.1.1〜AC3.1.3、AC3.1.6、
AC3.2.2、AC3.3.1、AC3.3.2。
"""

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from src.managers.components.resource_sharing import ResourceSharing
from src.managers.sharing_manager import SharingManager
from src.models.errors import LookupUnavailableError
from tests.conftest import login_as
from tests.unit.test_resource_sharing import DEPARTMENTS, TABLE, FakeStore

A = "local:a@example.com"  # member（sales）
B = "local:b@example.com"  # member（sales・plan）


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def departments() -> Mock:
    directory = Mock()
    directory.list_all.return_value = list(DEPARTMENTS)
    directory.get_many.side_effect = lambda ids: {
        d.group_id: d for d in DEPARTMENTS if d.group_id in ids
    }
    return directory


@pytest.fixture(autouse=True)
def sharing_manager(store, departments):
    sharing = ResourceSharing(store, departments, sleep=lambda _s: None)
    sharing.register(TABLE)
    manager = SharingManager(resource_sharing=sharing)
    with patch("web.routers.sharing.get_sharing_manager", return_value=manager):
        yield manager


def _as(client: Any, email: str, groups: tuple[str, ...], roles=("member",)) -> Any:
    login_as(client, email=email, roles=roles, groups=groups)
    return client


@pytest.fixture
def member_a(client):
    return _as(client, "a@example.com", ("sales",))


@pytest.fixture
def member_b(client):
    return _as(client, "b@example.com", ("sales", "plan"))


def _triggers(response: Any) -> dict[str, Any]:
    return json.loads(response.headers["HX-Trigger"])


# =============================================================================
# ダイアログの中身（GET）
# =============================================================================


class TestDialog:
    def test_single_dialog_has_the_picker_and_notes(self, member_b, store):
        store.add("p1", B, {"sales"})
        response = member_b.get("/share/persona/p1/dialog")

        assert response.status_code == 200
        html = response.text
        assert "data-department-picker" in html
        assert 'name="departments_initial" value="sales"' in html
        assert 'hx-post="/share/persona/p1"' in html
        # ペルソナの注意文と member の注記が保存ボタンに結び付く（BR6.5、BR7.5）
        assert "share-persona-notice" in html and "share-scope-note" in html
        assert 'aria-describedby="share-persona-notice share-scope-note' in html
        assert "選べるのは所属部署だけです" in html
        assert "生成過程" in html

    def test_bulk_dialog_counts_without_names(self, member_b, store):
        store.add("p1", B, {"sales"})
        store.add("p2", B)
        store.add("p3", A)  # 読めない
        response = member_b.get("/share/persona/dialog?ids=p1&ids=p2,p3")

        assert response.status_code == 200
        html = response.text
        assert "2 件中 1 件で共有中" in html
        assert "1 件は共有先を変えられません" in html
        assert "名前-p3" not in html
        assert 'name="ids" value="p1"' in html and 'name="ids" value="p3"' not in html
        assert 'hx-post="/share/persona"' in html

    def test_member_without_department_cannot_save(self, client, store):
        _as(client, "c@example.com", ())
        store.add("p1", "local:c@example.com")
        html = client.get("/share/persona/p1/dialog").text
        assert "所属部署がないため共有できません" in html
        assert "data-share-save" in html and "disabled" in html

    def test_shared_only_target_is_forbidden(self, member_b, store):
        store.add("p1", A, {"sales"})
        assert member_b.get("/share/persona/p1/dialog").status_code == 403

    def test_unreadable_and_missing_get_the_same_answer(self, member_b, store):
        store.add("p1", A)
        unreadable = member_b.get("/share/persona/p1/dialog")
        missing = member_b.get("/share/persona/nope/dialog")
        assert unreadable.status_code == missing.status_code == 404
        assert unreadable.text == missing.text
        assert unreadable.headers["HX-Trigger"] == missing.headers["HX-Trigger"]

    @pytest.mark.parametrize(
        "path", ["/share/discussion/p1/dialog", "/share/nope/dialog?ids=p1"]
    )
    def test_unregistered_type_is_not_found(self, member_a, path):
        assert member_a.get(path).status_code == 404

    def test_department_list_unavailable_is_retryable(
        self, member_a, store, departments
    ):
        store.add("p1", A)
        departments.list_all.side_effect = LookupUnavailableError("down")
        response = member_a.get("/share/persona/p1/dialog")
        assert response.status_code == 503


# =============================================================================
# 保存（POST）
# =============================================================================


class TestSave:
    def test_single_save_closes_and_reloads(self, member_a, store):
        store.add("p1", A)
        response = member_a.post("/share/persona/p1", data={"departments": "sales"})

        assert response.status_code == 200 and response.text == ""
        triggers = _triggers(response)
        assert triggers["dialogClose"] == {"id": "share-dialog"}
        assert triggers["listReload"] == {"target": "#persona-list"}
        assert triggers["showToast"]["message"] == "共有先を更新しました"
        assert store.bodies["p1"]["shared_group_ids"] == {"sales"}

    def test_bulk_save_toast_has_the_count(self, member_a, store):
        store.add("p1", A)
        store.add("p2", A)
        response = member_a.post(
            "/share/persona", data={"ids": ["p1", "p2"], "departments": "sales"}
        )
        assert _triggers(response)["showToast"]["message"] == (
            "2 件の共有先を更新しました"
        )

    def test_partial_failure_is_shown_in_the_dialog(self, member_a, store):
        store.add("p1", A)
        store.add("p2", B, {"sales"})  # 共有されているだけ（名前は読める）
        store.add("p3", B)  # 読めない
        response = member_a.post(
            "/share/persona",
            data={"ids": ["p1", "p2", "p3"], "departments": "sales"},
        )

        assert response.status_code == 200
        html = response.text
        assert "3 件中 1 件を更新しました" in html
        assert "名前-p2" in html and "名前-p3" not in html
        assert "名前を表示できない対象 1 件" in html
        assert 'data-failed-ids="p2,p3"' in html
        assert "HX-Trigger" not in response.headers

    def test_out_of_scope_value_is_shown_in_the_dialog(self, member_a, store):
        store.add("p1", A)
        response = member_a.post("/share/persona/p1", data={"departments": "hr"})

        assert response.status_code == 400
        assert response.headers["X-Render-Response"] == "true"
        assert "選べない部署が含まれています" in response.text
        assert "write_shares" not in store.calls

    def test_retryable_failure_is_shown_in_the_dialog(
        self, member_a, store, departments
    ):
        store.add("p1", A)
        departments.list_all.side_effect = LookupUnavailableError("down")
        response = member_a.post("/share/persona/p1", data={"departments": "sales"})

        assert response.status_code == 503
        assert response.headers["X-Render-Response"] == "true"
        assert 'role="alert"' in response.text

    def test_forbidden_is_shown_and_the_list_is_reloaded_on_close(
        self, member_b, store
    ):
        store.add("p1", A, {"sales"})
        response = member_b.post("/share/persona/p1", data={"departments": "plan"})

        assert response.status_code == 403
        assert response.headers["X-Render-Response"] == "true"
        assert "この操作の権限がありません" in response.text
        assert _triggers(response)["shareReloadOnClose"] == {"target": "#persona-list"}

    def test_unreadable_and_missing_save_get_the_same_answer(self, member_b, store):
        store.add("p1", A)
        unreadable = member_b.post("/share/persona/p1", data={"departments": "sales"})
        missing = member_b.post("/share/persona/nope", data={"departments": "sales"})
        assert unreadable.status_code == missing.status_code == 404
        assert unreadable.text == missing.text
        assert unreadable.headers["HX-Trigger"] == missing.headers["HX-Trigger"]

    def test_bulk_limits_are_shown_in_the_dialog(self, member_a):
        response = member_a.post("/share/persona", data={"departments": "sales"})
        assert response.status_code == 400
        assert "1件以上選択してください" in response.text

    def test_unexpected_error_is_a_toast(self, member_a, sharing_manager):
        with patch.object(sharing_manager, "save", side_effect=RuntimeError("bug")):
            response = member_a.post("/share/persona/p1", data={"departments": "x"})
        assert response.status_code == 500
        assert "showToast" in _triggers(response)
        assert "bug" not in response.text

    def test_unregistered_type_save_is_not_found(self, member_a):
        response = member_a.post("/share/survey/p1", data={"departments": "sales"})
        assert response.status_code == 404


class TestEntryChecks:
    def test_post_without_the_custom_header_is_rejected(self, test_app, store):
        store.add("p1", A)
        with TestClient(test_app) as plain:
            login_as(plain, email="a@example.com", groups=("sales",))
            response = plain.post("/share/persona/p1", data={"departments": "sales"})
        assert response.status_code == 403
        assert "shared_group_ids" not in store.bodies["p1"]

    def test_signed_out_user_is_stopped_at_the_entrance(self, test_app):
        with TestClient(test_app, headers={"HX-Request": "true"}) as anonymous:
            response = anonymous.get("/share/persona/p1/dialog")
        assert response.status_code == 401
