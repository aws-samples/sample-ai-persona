"""Settings ルーターの API テスト"""
from unittest.mock import Mock, patch


class TestSettingsPage:
    @patch("web.routers.settings.get_dataset_manager")
    @patch("web.routers.settings.get_mcp_manager")
    @patch("web.routers.settings.service_factory")
    def test_settings_page_loads(self, mock_sf, mock_mcp, mock_get_dm, client):
        mock_dm = Mock()
        mock_dm.get_datasets.return_value = []
        mock_get_dm.return_value = mock_dm
        mock_mcp_mgr = Mock()
        mock_mcp_mgr.is_running.return_value = False
        mock_mcp.return_value = mock_mcp_mgr
        mock_db = Mock()
        mock_db.get_all_knowledge_bases.return_value = []
        mock_sf.get_database_service.return_value = mock_db
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
    @patch("web.routers.settings.service_factory")
    def test_knowledge_bases_list(self, mock_sf, client):
        mock_db = Mock()
        mock_db.get_all_knowledge_bases.return_value = []
        mock_sf.get_database_service.return_value = mock_db
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

    @patch("web.routers.settings.service_factory")
    def test_api_knowledge_bases(self, mock_sf, client):
        mock_db = Mock()
        mock_db.get_all_knowledge_bases.return_value = []
        mock_sf.get_database_service.return_value = mock_db
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
    @patch("web.routers.settings.service_factory")
    def test_delete_knowledge_base(self, mock_sf, client):
        mock_db = Mock()
        mock_db.delete_knowledge_base.return_value = True
        mock_db.get_all_knowledge_bases.return_value = []
        mock_sf.get_database_service.return_value = mock_db
        resp = client.delete(
            "/settings/knowledge-bases/kb1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
