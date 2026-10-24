"""Settings ルーターの API テスト（U3 のダイアログ、U7 の admin 限定と利用範囲）。

本物の DatasetManager・SettingsManager を moto の上で動かす（tests/api/u7_world.py）。
U7 の対応: BR3.1、BR3.2、BR6.3、NFR1.72、NFR3.13、NFR4.14、AC5.2.1〜AC5.2.3、AC5.3.1〜AC5.3.3。
"""

from contextlib import ExitStack

import pytest

from tests.api.u7_world import ADMIN_USER, ME, ApiWorld
from tests.dialog_helpers import dialog_markup

_COLUMNS = '[{"name": "a", "data_type": "string"}]'


@pytest.fixture
def aw(client, survey_world):
    """The admin signed in, with the real Managers behind the settings router."""
    ADMIN_USER.login(client)
    world = ApiWorld(survey_world)
    with ExitStack() as stack:
        world.patch_routers(stack)
        yield world


# =============================================================================
# admin は今のまま使える
# =============================================================================


class TestAdminScreens:
    @pytest.mark.parametrize(
        "path",
        [
            "/settings/",
            "/settings/datasets",
            "/settings/knowledge-bases",
            "/settings/api/datasets",
            "/settings/api/knowledge-bases",
            "/settings/data-agent/status",
        ],
    )
    def test_admin_opens_every_screen(self, aw, client, path):
        assert client.get(path).status_code == 200

    def test_lists_show_the_scope(self, aw, client):
        aw.world.dataset("sales")
        aw.world.knowledge_base()
        html = client.get("/settings/").text
        assert "利用範囲: 営業部" in html
        assert "利用範囲: admin のみ" in html
        assert 'data-testid="dataset-scope-button"' in html
        assert 'data-testid="kb-scope-button"' in html

    def test_api_lists_keep_their_shape(self, aw, client):
        dataset = aw.world.dataset()
        body = client.get("/settings/api/datasets").json()
        assert [d["id"] for d in body] == [dataset.id]

    def test_update_of_a_missing_dataset_is_404(self, aw, client):
        response = client.put(
            "/settings/datasets/nonexistent",
            data={"name": "n", "columns_json": _COLUMNS},
        )
        assert response.status_code == 404

    def test_delete_dataset_and_knowledge_base(self, aw, client):
        dataset = aw.world.dataset()
        kb = aw.world.knowledge_base()
        assert client.delete(f"/settings/datasets/{dataset.id}").status_code == 200
        assert client.delete(f"/settings/knowledge-bases/{kb.id}").status_code == 200
        assert aw.world.dataset_store.get(dataset.id, consistent=True) is None
        assert aw.world.kb_store.get(kb.id, consistent=True) is None


# =============================================================================
# 利用範囲のダイアログ（BR3.2、AC5.3.1〜AC5.3.3）
# =============================================================================


class TestScopeDialog:
    @pytest.mark.parametrize("kind", ["datasets", "knowledge-bases"])
    def test_dialog_has_the_picker_initial_values_and_warning(self, aw, client, kind):
        asset = (
            aw.world.dataset("sales")
            if kind == "datasets"
            else aw.world.knowledge_base("sales")
        )
        html = client.get(f"/settings/{kind}/{asset.id}/scope").text
        outer = dialog_markup(html, "asset-scope-dialog")
        assert "data-dialog-autoopen" in outer
        assert 'name="departments"' in html
        assert 'name="departments_initial" value="sales"' in html
        assert "営業部" in html and "企画部" in html
        assert "data-scope-remove-warning" in html
        assert 'role="alert"' in html

    def test_vanished_group_is_shown_and_can_be_removed(self, aw, client):
        dataset = aw.world.dataset("gone")
        html = client.get(f"/settings/datasets/{dataset.id}/scope").text
        assert "（削除されたグループ）" in html

    def test_save_changes_only_the_difference(self, aw, client):
        dataset = aw.world.dataset("sales")
        response = client.post(
            f"/settings/datasets/{dataset.id}/scope",
            data={"departments": "plan", "departments_initial": "sales"},
        )
        assert response.status_code == 200
        assert "dialogClose" in response.headers["HX-Trigger"]
        assert "listReload" in response.headers["HX-Trigger"]
        assert aw.world.dataset_store.get(
            dataset.id, consistent=True
        ).shared_group_ids == frozenset({"plan"})

    def test_save_for_a_knowledge_base(self, aw, client):
        kb = aw.world.knowledge_base()
        response = client.post(
            f"/settings/knowledge-bases/{kb.id}/scope",
            data={"departments": "sales"},
        )
        assert response.status_code == 200
        assert aw.world.kb_store.get(
            kb.id, consistent=True
        ).shared_group_ids == frozenset({"sales"})

    def test_unknown_department_is_refused_inside_the_dialog(self, aw, client):
        dataset = aw.world.dataset()
        response = client.post(
            f"/settings/datasets/{dataset.id}/scope",
            data={"departments": "unknown"},
        )
        assert response.status_code == 400
        assert (
            aw.world.dataset_store.get(dataset.id, consistent=True).shared_group_ids
            == frozenset()
        )

    def test_missing_asset_is_404(self, aw, client):
        assert client.get("/settings/datasets/missing/scope").status_code == 404
        response = client.post(
            "/settings/knowledge-bases/missing/scope", data={"departments": "sales"}
        )
        assert response.status_code == 404


# =============================================================================
# member には /settings 以下の全経路が 404（BR3.1、AC5.2.1〜AC5.2.3）
# =============================================================================

_MEMBER_ROUTES = [
    ("get", "/settings/", {}),
    ("get", "/settings/datasets", {}),
    ("get", "/settings/datasets/form", {}),
    ("post", "/settings/datasets/analyze", {"files": {"file": ("a.csv", b"a\n1\n")}}),
    (
        "post",
        "/settings/datasets",
        {
            "files": {"file": ("a.csv", b"a\n1\n")},
            "data": {"name": "n", "columns_json": _COLUMNS},
        },
    ),
    (
        "put",
        "/settings/datasets/{dataset}",
        {"data": {"name": "x", "columns_json": _COLUMNS}},
    ),
    ("delete", "/settings/datasets/{dataset}", {}),
    ("get", "/settings/datasets/{dataset}/scope", {}),
    ("post", "/settings/datasets/{dataset}/scope", {"data": {"departments": "sales"}}),
    ("get", "/settings/api/datasets", {}),
    ("get", "/settings/knowledge-bases", {}),
    (
        "post",
        "/settings/knowledge-bases",
        {"data": {"knowledge_base_id": "KB9", "name": "n"}},
    ),
    ("delete", "/settings/knowledge-bases/{kb}", {}),
    ("get", "/settings/knowledge-bases/{kb}/scope", {}),
    (
        "post",
        "/settings/knowledge-bases/{kb}/scope",
        {"data": {"departments": "sales"}},
    ),
    ("get", "/settings/api/knowledge-bases", {}),
    ("get", "/settings/data-agent/status", {}),
    (
        "post",
        "/settings/data-agent",
        {"data": {"runtime_arn": "arn:x", "enabled": "true"}},
    ),
    ("post", "/settings/data-agent/test", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), _MEMBER_ROUTES)
def test_member_gets_404_on_every_settings_route_and_nothing_changes(
    aw, client, method, path, kwargs
):
    dataset = aw.world.dataset()
    kb = aw.world.knowledge_base()
    ME.login(client)

    response = getattr(client, method)(
        path.format(dataset=dataset.id, kb=kb.id), **kwargs
    )

    assert response.status_code == 404
    assert aw.world.dataset_store.get(dataset.id, consistent=True) == dataset
    assert aw.world.kb_store.get(kb.id, consistent=True).shared_group_ids == frozenset()
    assert len(aw.world.kb_store.list_all()) == 1
    assert "arn:x" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/settings/datasets/d1/scope"),
        ("post", "/settings/knowledge-bases/k1/scope"),
    ],
)
def test_scope_saves_need_the_csrf_header(test_app, method, path):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as no_header:
        ADMIN_USER.login(no_header)
        response = getattr(no_header, method)(path)

    assert response.status_code == 403


# =============================================================================
# U3 WF3: 設定画面のモーダルは components/dialog.html で描く（今のまま）
# =============================================================================


class TestSettingsModalsUseSharedDialog:
    def test_kb_register_and_upload_are_hidden_dialogs(self, aw, client):
        html = client.get("/settings/").text

        for dialog_id in ("kb-register-modal", "upload-modal"):
            outer = dialog_markup(html, dialog_id)
            assert "data-dialog-backdrop-close" not in outer
            assert f'data-dialog-open="{dialog_id}"' in html
        # 開閉は app.js の Dialog が持つ（hidden クラスの付け外しをやめた）
        assert "classList.remove('hidden')" not in html
        assert "classList.add('hidden')" not in html

    def test_kb_register_still_posts_with_htmx(self, aw, client):
        html = client.get("/settings/").text

        assert 'hx-post="/settings/knowledge-bases"' in html
        assert "dialogClose" in html

    def test_upload_still_uses_fetch_with_requested_with(self, aw, client):
        html = client.get("/settings/").text

        assert "fetch('/settings/datasets'" in html
        assert "'X-Requested-With': 'XMLHttpRequest'" in html
        assert "dialog:closed" in html

    def test_dataset_edit_is_an_inserted_dialog(self, aw, client):
        dataset = aw.world.dataset()

        html = client.get(f"/settings/datasets/form?dataset_id={dataset.id}").text

        outer = dialog_markup(html, "edit-dataset-modal")
        assert "data-dialog-autoopen" in outer
        assert "data-dialog-remove-on-close" in outer
        assert f'hx-put="/settings/datasets/{dataset.id}"' in html
        assert ".remove()" not in html
