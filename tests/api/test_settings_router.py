"""Settings ルーターの API テスト（U3 のダイアログ、U7 の admin 限定と利用範囲）。

本物の DatasetManager・SettingsManager を moto の上で動かす（tests/api/u7_world.py）。
U7 の対応: BR3.1、BR3.2、BR6.3、NFR1.72、NFR3.13、NFR4.14、AC5.2.1〜AC5.2.3、AC5.3.1〜AC5.3.3。
"""

import re
from contextlib import ExitStack
from datetime import datetime

import pytest

from src.models.persona_binding import PersonaBinding

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
        bindings = aw.world.binding_store
        bindings.put(PersonaBinding.for_dataset("p1", dataset.id, now=datetime.now()))
        bindings.put(PersonaBinding.for_kb("p1", kb.id, now=datetime.now()))
        assert client.delete(f"/settings/datasets/{dataset.id}").status_code == 200
        assert client.delete(f"/settings/knowledge-bases/{kb.id}").status_code == 200
        assert aw.world.dataset_store.get(dataset.id, consistent=True) is None
        assert aw.world.kb_store.get(kb.id, consistent=True) is None
        assert bindings.list_for_persona("p1") == []


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

    def test_kb_register_offers_both_kb_types(self, aw, client):
        html = client.get("/settings/").text
        dialog_markup(html, "kb-register-modal")
        form = html[html.index('hx-post="/settings/knowledge-bases"') :]
        form = form[: form.index("</form>")]

        assert 'name="kb_type"' in form
        assert 'value="self_managed"' in form
        assert 'value="managed"' in form

    def test_kb_register_preselects_the_recommended_managed_kb(self, aw, client):
        html = client.get("/settings/").text
        form = html[html.index('hx-post="/settings/knowledge-bases"') :]
        form = form[: form.index("</form>")]

        assert re.search(r'<option value="managed"[^>]*\sselected', form)
        assert not re.search(r'<option value="self_managed"[^>]*\sselected', form)
        assert form.index('value="managed"') < form.index('value="self_managed"')

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


class TestManagedKnowledgeBase:
    def test_register_a_managed_kb_and_list_its_type(self, aw, client):
        response = client.post(
            "/settings/knowledge-bases",
            data={"knowledge_base_id": "KB9", "name": "n", "kb_type": "managed"},
        )

        assert response.status_code == 200
        assert 'data-testid="kb-type"' in response.text
        assert "Managed" in response.text
        body = client.get("/settings/api/knowledge-bases").json()
        assert [kb["kb_type"] for kb in body] == ["managed"]

    def test_register_without_a_type_keeps_the_self_managed_kb(self, aw, client):
        client.post(
            "/settings/knowledge-bases", data={"knowledge_base_id": "KB9", "name": "n"}
        )

        body = client.get("/settings/api/knowledge-bases").json()
        assert [kb["kb_type"] for kb in body] == ["self_managed"]

    def test_unknown_type_registers_nothing(self, aw, client):
        response = client.post(
            "/settings/knowledge-bases",
            data={"knowledge_base_id": "KB9", "name": "n", "kb_type": "kendra"},
        )

        assert response.status_code == 400
        assert client.get("/settings/api/knowledge-bases").json() == []

    def test_list_labels_each_kb_type(self, aw, client):
        for kb_id, kb_type in (("KB1", "self_managed"), ("KB2", "managed")):
            client.post(
                "/settings/knowledge-bases",
                data={"knowledge_base_id": kb_id, "name": kb_id, "kb_type": kb_type},
            )

        html = client.get("/settings/knowledge-bases").text
        assert "種類: Self-managed KB" in html
        assert "種類: Managed KB" in html


# =============================================================================
# 閉域対応 U3: Data Agent を使ってよくない環境（BR3.2 の 1・2、BR3.8）
# =============================================================================

HTMX = {"HX-Request": "true"}


@pytest.fixture
def agent_config(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "ENABLE_DATA_AGENT", True)
    monkeypatch.setattr(config, "DATA_AGENT_RUNTIME_ARN", "arn:agent")
    monkeypatch.setattr(config, "DATA_AGENT_REGION", "ap-northeast-1")
    monkeypatch.setattr(config, "DATA_AGENT_ALLOWED", True)
    return config


class TestDataAgentNotAllowedRoutes:
    def test_save_is_forbidden_and_changes_nothing(self, aw, client, agent_config):
        agent_config.DATA_AGENT_ALLOWED = False
        response = client.post(
            "/settings/data-agent",
            data={"runtime_arn": "arn:new", "enabled": "true"},
            headers=HTMX,
        )
        assert response.status_code == 403
        assert "HX-Trigger" in response.headers
        assert response.text == ""
        assert agent_config.DATA_AGENT_RUNTIME_ARN == "arn:agent"
        assert agent_config.DATA_AGENT_ALLOWED is False

    def test_connection_test_is_forbidden_without_connecting(
        self, aw, client, agent_config
    ):
        from unittest.mock import patch

        agent_config.DATA_AGENT_ALLOWED = False
        with patch("src.managers.settings_manager.service_factory") as factory:
            response = client.post("/settings/data-agent/test", headers=HTMX)
        assert response.status_code == 403
        assert "HX-Trigger" in response.headers
        factory.get_data_agent_service.assert_not_called()

    @pytest.mark.parametrize("path", ["/settings/", "/settings/data-agent/status"])
    def test_not_allowed_screen_shows_a_notice_instead_of_the_form(
        self, aw, client, agent_config, path
    ):
        agent_config.DATA_AGENT_ALLOWED = False
        html = client.get(path).text
        assert 'data-testid="data-agent-not-allowed"' in html
        assert 'name="runtime_arn"' not in html
        assert 'data-testid="data-agent-save-button"' not in html
        assert "/settings/data-agent/test" not in html
        assert "arn:agent" not in html

    def test_public_screen_keeps_the_form(self, aw, client, agent_config):
        html = client.get("/settings/data-agent/status").text
        assert 'name="runtime_arn"' in html
        assert 'data-testid="data-agent-save-button"' in html
        assert "/settings/data-agent/test" in html
        assert 'data-testid="data-agent-not-allowed"' not in html

    def test_public_default_still_saves(self, aw, client, agent_config):
        response = client.post(
            "/settings/data-agent",
            data={"runtime_arn": "arn:new", "enabled": "true"},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert agent_config.DATA_AGENT_RUNTIME_ARN == "arn:new"
        assert agent_config.ENABLE_DATA_AGENT is True


# =============================================================================
# U6: 高度な生成（ベータ）の区画（C11、BR34、NFR3.31、NFR3.32、AC11.1.6）
# =============================================================================

_SECTION = "/settings/advanced-generation"


@pytest.fixture
def ag(aw):
    """The settings section on a mocked AppSettings store (version 2 saved)."""
    from unittest.mock import Mock

    from src.services.stores.app_settings_store import AppSettingsStore

    store = Mock(spec=AppSettingsStore)
    store.get.return_value = {
        "version": 2,
        "audience_mode": "departments",
        "department_groups": ["group:sales", "group:gone"],
        "allow_detailed_analysis": False,
        "updated_by": ADMIN_USER.sub,
        "updated_at": "2026-11-20T09:00:00+00:00",
    }
    store.put_if_version.return_value = 3
    aw.settings._settings_store = store
    aw.settings._owners = Mock(label=Mock(return_value="管理 太郎"))
    return store


def _save(client, **form):
    data = {
        "audience_mode": "departments",
        "departments": ["sales"],
        "allow_detailed_analysis": "true",
        "version": "2",
    }
    data.update(form)
    return client.post(_SECTION, data=data, headers=HTMX)


class TestAdvancedGenerationSection:
    def test_the_settings_page_loads_the_section(self, aw, ag, client):
        html = client.get("/settings/").text
        assert f'hx-get="{_SECTION}"' in html

    def test_admin_gets_the_section(self, aw, ag, client):
        response = client.get(_SECTION)

        html = response.text
        assert response.status_code == 200
        assert 'name="version" value="2"' in html
        assert 'role="switch"' in html and 'aria-checked="false"' in html
        assert "営業部" in html and "管理 太郎" in html
        assert 'data-testid="advanced-setting-missing"' in html

    def test_the_detailed_switch_carries_the_beta_caution(self, aw, ag, client):
        """Security review: the sandbox can reach some AWS services."""
        html = client.get(_SECTION).text

        assert "advanced-setting-detailed-caution" not in html
        assert "信頼できるデータ" not in html

    def test_a_save_returns_the_new_version_with_a_toast(self, aw, ag, client):
        response = _save(client)

        assert response.status_code == 200
        assert 'name="version" value="3"' in response.text
        assert "showToast" in response.headers["HX-Trigger"]
        _, item, expected = ag.put_if_version.call_args.args
        assert expected == 2 and item["department_groups"] == ["group:sales"]
        assert item["allow_detailed_analysis"] is True

    def test_a_bad_input_is_a_400_section_keeping_the_input(self, aw, ag, client):
        response = _save(
            client, departments=["gone-forever"], audience_mode="departments"
        )

        assert response.status_code == 400
        assert response.headers["X-Render-Response"] == "true"
        assert 'data-testid="advanced-setting-error"' in response.text
        assert 'value="everyone"' in response.text
        ag.put_if_version.assert_not_called()

    def test_a_vanished_department_tells_the_cause_in_the_section(self, aw, ag, client):
        """BR34.3: a department gone since the screen opened, still checked."""
        from src.models.errors import CodedError, ErrorCode
        from web.error_messages import user_message_for

        response = _save(client, departments=["sales", "gone"])

        assert response.status_code == 400
        assert response.headers["X-Render-Response"] == "true"
        expected = user_message_for(
            CodedError(
                "diag",
                code=ErrorCode.REQUEST_INVALID,
                context={"reason": "vanished_departments", "count": 1},
            )
        )
        generic = user_message_for(CodedError("diag", code=ErrorCode.REQUEST_INVALID))
        error_area = re.search(
            r'data-testid="advanced-setting-error"[^>]*>(.*?)</div>',
            response.text,
            re.S,
        )
        assert error_area is not None
        assert error_area.group(1).strip() == expected != generic
        assert "gone" not in error_area.group(1)
        ag.put_if_version.assert_not_called()

    def test_a_lost_version_is_a_409_with_the_latest_values(self, aw, ag, client):
        from src.services.stores.app_settings_store import SettingsVersionConflict

        ag.put_if_version.side_effect = SettingsVersionConflict("lost")
        ag.get.return_value = {**ag.get.return_value, "version": 7}

        response = _save(client)

        assert response.status_code == 409
        assert response.headers["X-Render-Response"] == "true"
        assert 'name="version" value="7"' in response.text
        assert 'data-testid="advanced-setting-conflict"' in response.text
        assert "lost" not in response.text

    def test_member_gets_404_and_403_and_nothing_is_written(self, aw, ag, client):
        ME.login(client)

        assert client.get(_SECTION).status_code == 404
        assert _save(client).status_code == 403
        ag.put_if_version.assert_not_called()
        ag.get.assert_not_called()


class TestAdvancedGenerationSectionWhenTheFeatureIsOff:
    """The closed network Stage passes ENABLE_ADVANCED_GENERATION=false (BR1.3)."""

    @pytest.fixture
    def off(self, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "ENABLE_ADVANCED_GENERATION", False)

    def test_the_page_labels_which_features_each_block_serves(
        self, aw, ag, off, client
    ):
        html = client.get("/settings/").text

        # dataset and knowledge base: interview and discussion (not the survey)
        assert html.count('data-testid="feature-badges"') == 3
        assert html.count(">インタビュー</li>") == 2
        assert html.count(">議論</li>") == 2
        assert (
            "アンケート"
            not in html.split("</h1>")[1].split("<!-- ナレッジベース登録")[0]
        )
        # the advanced generation block is first, the data analysis agent last
        if "advanced-generation-settings-title" in html:
            assert html.index("高度な生成（ベータ）") < html.index("データ連携機能")
        assert html.index("データ分析エージェント 連携") > html.index(
            "ナレッジベース管理"
        )
        assert ">レポート</li>" in html

    def test_the_settings_page_has_no_section(self, aw, ag, off, client):
        response = client.get("/settings/")

        assert response.status_code == 200
        assert f'hx-get="{_SECTION}"' not in response.text
        assert 'data-testid="advanced-generation-settings"' not in response.text
        assert "advanced-generation-settings-title" not in response.text

    def test_the_section_is_404_and_nothing_is_read_or_written(
        self, aw, ag, off, client
    ):
        assert client.get(_SECTION).status_code == 404
        assert _save(client).status_code == 404
        ag.get.assert_not_called()
        ag.put_if_version.assert_not_called()

    def test_member_still_gets_404_and_403(self, aw, ag, off, client):
        ME.login(client)

        assert client.get(_SECTION).status_code == 404
        assert _save(client).status_code == 403
        ag.put_if_version.assert_not_called()


def test_the_section_save_needs_the_csrf_header(test_app):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as no_header:
        ADMIN_USER.login(no_header)
        response = no_header.post(_SECTION, data={"version": "0"})

    assert response.status_code == 403
