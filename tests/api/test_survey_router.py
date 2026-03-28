"""Survey ルーターの API テスト"""
from unittest.mock import Mock, patch


class TestSurveyIndexPage:
    def test_survey_index_loads(self, client):
        resp = client.get("/survey/")
        assert resp.status_code == 200


class TestSurveyTemplatesPage:
    @patch("web.routers.survey.get_survey_manager")
    def test_templates_list_loads(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_all_templates.return_value = []
        mock_get_mgr.return_value = mock_mgr
        resp = client.get("/survey/templates")
        assert resp.status_code == 200

    def test_template_new_form_loads(self, client):
        resp = client.get("/survey/templates/new")
        assert resp.status_code == 200


class TestSurveyResultsPage:
    @patch("web.routers.survey.get_survey_manager")
    def test_results_list_loads(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_all_surveys.return_value = []
        mock_get_mgr.return_value = mock_mgr
        resp = client.get("/survey/results")
        assert resp.status_code == 200


class TestSurveyStartPage:
    @patch("web.routers.survey.get_survey_manager")
    def test_start_page_loads(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_all_templates.return_value = []
        mock_get_mgr.return_value = mock_mgr
        resp = client.get("/survey/start")
        assert resp.status_code == 200


class TestSurveyPersonaDataPage:
    @patch("web.routers.survey.service_factory")
    def test_persona_data_page_loads(self, mock_sf, client):
        mock_survey_svc = Mock()
        mock_survey_svc.check_nemotron_dataset_status.return_value = {"exists": False, "size_mb": 0}
        mock_survey_svc.list_custom_datasets.return_value = []
        mock_sf.get_survey_service.return_value = mock_survey_svc
        resp = client.get("/survey/persona-data")
        assert resp.status_code == 200


class TestSurveyTemplateCreate:
    @patch("web.routers.survey.get_survey_manager")
    def test_create_template(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.create_template.return_value = Mock(id="t1")
        mock_get_mgr.return_value = mock_mgr
        resp = client.post(
            "/survey/templates",
            data={
                "name": "テストテンプレート",
                "question_text_1": "質問1",
                "question_type_1": "free_text",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code in (200, 303)


class TestSurveyTemplateDelete:
    @patch("web.routers.survey.get_survey_manager")
    def test_delete_template(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.delete_template.return_value = True
        mock_get_mgr.return_value = mock_mgr
        resp = client.delete(
            "/survey/templates/t1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestSurveyResultDelete:
    @patch("web.routers.survey.get_survey_manager")
    def test_delete_result(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.delete_survey.return_value = True
        mock_get_mgr.return_value = mock_mgr
        resp = client.delete(
            "/survey/results/s1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
