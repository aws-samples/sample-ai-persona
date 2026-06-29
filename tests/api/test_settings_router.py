"""Settings ルーターの API テスト"""

from unittest.mock import Mock, patch


class TestSettingsPage:
    @patch("web.routers.settings.get_dataset_manager")
    @patch("web.routers.settings.get_settings_manager")
    def test_settings_page_loads(self, mock_get_sm, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        mock_sm = Mock()
        mock_sm.is_mcp_running.return_value = False
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
