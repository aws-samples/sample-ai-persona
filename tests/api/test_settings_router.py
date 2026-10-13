"""Settings ルーターの API テスト"""

from unittest.mock import Mock, patch

from tests.dialog_helpers import dialog_markup


class TestSettingsPage:
    @patch("web.routers.settings.get_dataset_manager")
    @patch("web.routers.settings.get_settings_manager")
    def test_settings_page_loads(self, mock_get_sm, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        mock_sm = Mock()
        mock_sm.get_all_knowledge_bases.return_value = []
        mock_get_sm.return_value = mock_sm
        resp = client.get("/settings/")
        assert resp.status_code == 200


class TestSettingsDatasetsList:
    @patch("web.routers.settings.get_dataset_manager")
    def test_datasets_list(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        resp = client.get("/settings/datasets")
        assert resp.status_code == 200


class TestSettingsKnowledgeBases:
    @patch("web.routers.settings.get_settings_manager")
    def test_knowledge_bases_list(self, mock_get_sm, client):
        mock_sm = Mock()
        mock_sm.get_all_knowledge_bases.return_value = []
        mock_get_sm.return_value = mock_sm
        resp = client.get("/settings/knowledge-bases")
        assert resp.status_code == 200


class TestSettingsAPIEndpoints:
    @patch("web.routers.settings.get_dataset_manager")
    def test_api_datasets(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        resp = client.get("/settings/api/datasets")
        assert resp.status_code == 200

    @patch("web.routers.settings.get_settings_manager")
    def test_api_knowledge_bases(self, mock_get_sm, client):
        mock_sm = Mock()
        mock_sm.get_all_knowledge_bases.return_value = []
        mock_get_sm.return_value = mock_sm
        resp = client.get("/settings/api/knowledge-bases")
        assert resp.status_code == 200


class TestSettingsUpdateDataset:
    @patch("web.routers.settings.get_dataset_manager")
    def test_update_nonexistent_dataset_returns_404(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.update_dataset.return_value = None
        mock_get_dm.return_value = mock_dm
        resp = client.put(
            "/settings/datasets/nonexistent",
            data={"name": "n", "columns_json": "[]"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
        assert 'role="alert"' in resp.text


class TestSettingsDeleteDataset:
    @patch("web.routers.settings.get_dataset_manager")
    def test_delete_dataset(self, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.delete_dataset.return_value = True
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        resp = client.delete(
            "/settings/datasets/d1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestSettingsDeleteKnowledgeBase:
    @patch("web.routers.settings.get_settings_manager")
    def test_delete_knowledge_base(self, mock_get_sm, client):
        mock_sm = Mock()
        mock_sm.delete_knowledge_base.return_value = None
        mock_sm.get_all_knowledge_bases.return_value = []
        mock_get_sm.return_value = mock_sm
        resp = client.delete(
            "/settings/knowledge-bases/kb1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestSettingsModalsUseSharedDialog:
    """U3 WF3: the settings modals are rendered by components/dialog.html."""

    @staticmethod
    def _settings_page(client) -> str:
        with (
            patch("web.routers.settings.get_dataset_manager") as get_dm,
            patch("web.routers.settings.get_settings_manager") as get_sm,
        ):
            get_dm.return_value.get_datasets.return_value = []
            get_sm.return_value.get_all_knowledge_bases.return_value = []
            return client.get("/settings/").text

    def test_kb_register_and_upload_are_hidden_dialogs(self, client):
        html = self._settings_page(client)

        for dialog_id in ("kb-register-modal", "upload-modal"):
            outer = dialog_markup(html, dialog_id)
            assert "data-dialog-backdrop-close" not in outer
            assert f'data-dialog-open="{dialog_id}"' in html
        # 開閉は app.js の Dialog が持つ（hidden クラスの付け外しをやめた）
        assert "classList.remove('hidden')" not in html
        assert "classList.add('hidden')" not in html

    def test_kb_register_still_posts_with_htmx(self, client):
        html = self._settings_page(client)

        assert 'hx-post="/settings/knowledge-bases"' in html
        assert "dialogClose" in html

    def test_upload_still_uses_fetch_with_requested_with(self, client):
        html = self._settings_page(client)

        assert "fetch('/settings/datasets'" in html
        assert "'X-Requested-With': 'XMLHttpRequest'" in html
        assert "dialog:closed" in html

    @patch("web.routers.settings.get_dataset_manager")
    def test_dataset_edit_is_an_inserted_dialog(self, mock_get_dm, client):
        dataset = Mock()
        dataset.to_dict.return_value = {
            "id": "d1",
            "name": "購買履歴",
            "description": "",
            "notes": "",
            "columns": [{"name": "user_id", "data_type": "string"}],
        }
        mock_get_dm.return_value.get_dataset.return_value = dataset

        html = client.get("/settings/datasets/form?dataset_id=d1").text

        outer = dialog_markup(html, "edit-dataset-modal")
        assert "data-dialog-autoopen" in outer
        assert "data-dialog-remove-on-close" in outer
        assert 'hx-put="/settings/datasets/d1"' in html
        assert ".remove()" not in html
