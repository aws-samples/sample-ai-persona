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
        mock_mgr.check_nemotron_status.return_value = {"exists": False, "size_mb": 0}
        mock_mgr.list_custom_datasets.return_value = []
        mock_mgr.get_available_filter_values.return_value = {}
        mock_mgr.get_datasource_count.return_value = 0
        mock_get_mgr.return_value = mock_mgr
        resp = client.get("/survey/start")
        assert resp.status_code == 200


class TestSurveyPersonaDataPage:
    @patch("web.routers.survey.get_survey_manager")
    def test_persona_data_page_loads(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.check_nemotron_status.return_value = {
            "exists": False,
            "size_mb": 0,
        }
        mock_mgr.list_custom_datasets.return_value = []
        mock_get_mgr.return_value = mock_mgr
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


class TestSurveyTemplateEdit:
    @patch("web.routers.survey.get_survey_manager")
    def test_edit_page_loads(self, mock_get_mgr, client):
        from src.models.survey_template import SurveyTemplate, Question

        tmpl = SurveyTemplate.create_new("テスト", [Question.create_free_text("Q1")])
        mock_mgr = Mock()
        mock_mgr.get_template.return_value = tmpl
        mock_get_mgr.return_value = mock_mgr

        resp = client.get(f"/survey/templates/{tmpl.id}/edit")
        assert resp.status_code == 200
        assert "テンプレート編集" in resp.text

    @patch("web.routers.survey.get_survey_manager")
    def test_edit_page_not_found(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_template.return_value = None
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/templates/nonexistent/edit")
        assert resp.status_code == 404


class TestSurveyResultDetail:
    @patch("web.routers.survey.get_survey_manager")
    def test_result_detail_loads(self, mock_get_mgr, client):
        from src.models.survey import Survey
        from src.models.survey_template import SurveyTemplate, Question

        survey = Survey.create_new("テスト", "", "tmpl-1", 100)
        survey.status = "completed"
        tmpl = SurveyTemplate.create_new("T", [Question.create_free_text("Q")])
        mock_mgr = Mock()
        mock_mgr.get_survey.return_value = survey
        mock_mgr.get_template.return_value = tmpl
        mock_get_mgr.return_value = mock_mgr

        resp = client.get(f"/survey/results/{survey.id}")
        assert resp.status_code == 200

    @patch("web.routers.survey.get_survey_manager")
    def test_result_detail_not_found(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_survey.return_value = None
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/nonexistent")
        assert resp.status_code == 404


class TestSurveyDownload:
    @patch("web.routers.survey.get_survey_manager")
    def test_download_redirects(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_download_url.return_value = "https://s3.example.com/signed"
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/s1/download", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "https://s3.example.com/signed"

    @patch("web.routers.survey.get_survey_manager")
    def test_download_not_found(self, mock_get_mgr, client):
        from src.managers.survey_manager import SurveyManagerError

        mock_mgr = Mock()
        mock_mgr.get_download_url.side_effect = SurveyManagerError("見つかりません")
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/bad-id/download")
        assert resp.status_code == 404


class TestSurveyPersonaStatistics:
    @patch("web.routers.survey.get_survey_manager")
    def test_statistics_success(self, mock_get_mgr, client):
        from src.models.survey import PersonaStatistics

        stats = PersonaStatistics(
            total_count=100,
            sex_distribution={"男性": 50, "女性": 50},
            age_distribution={"20代": 30, "30代": 40, "40代": 30},
            occupation_distribution={"エンジニア": 50},
            region_distribution={"関東": 60},
            prefecture_distribution={"東京都": 40},
            marital_status_distribution={"未婚": 60},
            age_stats={"min": 20, "max": 49, "average": 32.5},
        )
        mock_mgr = Mock()
        mock_mgr.get_persona_statistics.return_value = stats
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/s1/personas")
        assert resp.status_code == 200

    @patch("web.routers.survey.get_survey_manager")
    def test_statistics_error(self, mock_get_mgr, client):
        from src.managers.survey_manager import SurveyManagerError

        mock_mgr = Mock()
        mock_mgr.get_persona_statistics.side_effect = SurveyManagerError(
            "見つかりません"
        )
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/bad-id/personas")
        assert resp.status_code == 400


class TestSurveyVisualAnalysis:
    @patch("web.routers.survey.get_survey_manager")
    def test_visual_analysis_success(self, mock_get_mgr, client):
        from src.models.survey import VisualAnalysisData

        data = VisualAnalysisData(
            multiple_choice_charts=[],
            scale_rating_charts=[],
        )
        mock_mgr = Mock()
        mock_mgr.get_visual_analysis.return_value = data
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/s1/visual")
        assert resp.status_code == 200

    @patch("web.routers.survey.get_survey_manager")
    def test_visual_analysis_error(self, mock_get_mgr, client):
        from src.managers.survey_manager import SurveyManagerError

        mock_mgr = Mock()
        mock_mgr.get_visual_analysis.side_effect = SurveyManagerError("結果なし")
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/results/bad-id/visual")
        assert resp.status_code == 400


class TestSurveyAIChat:
    @patch("web.routers.survey.get_survey_manager")
    def test_ai_chat_success(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.generate_ai_chat_response.return_value = "AIの回答です"
        mock_get_mgr.return_value = mock_mgr

        resp = client.post(
            "/survey/templates/ai-chat",
            json={"messages": [{"role": "user", "content": "質問です"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["assistant_message"] == "AIの回答です"

    @patch("web.routers.survey.get_survey_manager")
    def test_ai_chat_invalid_messages(self, mock_get_mgr, client):
        resp = client.post(
            "/survey/templates/ai-chat",
            json={"messages": "not a list"},
        )
        assert resp.status_code == 400

    def test_ai_chat_no_body(self, client):
        resp = client.post("/survey/templates/ai-chat", content=b"not json")
        assert resp.status_code in (400, 422)


class TestSurveyAIGenerate:
    @patch("web.routers.survey.get_survey_manager")
    def test_ai_generate_success(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.generate_ai_questions_draft.return_value = {
            "summary": "テスト",
            "template_name": "調査",
            "questions": [{"id": "q1", "text": "Q", "question_type": "free_text"}],
        }
        mock_get_mgr.return_value = mock_mgr

        resp = client.post(
            "/survey/templates/ai-generate",
            json={"messages": [{"role": "user", "content": "設問を生成して"}]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "questions" in data

    @patch("web.routers.survey.get_survey_manager")
    def test_ai_generate_validation_error(self, mock_get_mgr, client):
        from src.managers.survey_manager import SurveyValidationError

        mock_mgr = Mock()
        mock_mgr.generate_ai_questions_draft.side_effect = SurveyValidationError(
            "会話履歴が空です"
        )
        mock_get_mgr.return_value = mock_mgr

        resp = client.post(
            "/survey/templates/ai-generate",
            json={"messages": [{"role": "user", "content": "x"}]},
        )
        assert resp.status_code == 400


class TestSurveyFilterOptions:
    @patch("web.routers.survey.get_survey_manager")
    def test_filter_options_loads(self, mock_get_mgr, client):
        mock_mgr = Mock()
        mock_mgr.get_available_filter_values.return_value = {
            "sex": ["男性", "女性"],
            "occupation": ["エンジニア"],
        }
        mock_get_mgr.return_value = mock_mgr

        resp = client.get("/survey/filter-options?datasource=nemotron")
        assert resp.status_code == 200


class TestSurveyExecute:
    @patch("web.routers.survey.get_survey_manager")
    def test_execute_success(self, mock_get_mgr, client):
        from src.models.survey import Survey

        survey = Survey.create_new("テスト", "", "tmpl-1", 100)
        mock_mgr = Mock()
        mock_mgr.create_survey.return_value = survey
        mock_get_mgr.return_value = mock_mgr

        resp = client.post(
            "/survey/execute",
            data={
                "template_id": "tmpl-1",
                "name": "テスト実行",
                "persona_count": "100",
                "datasource": "nemotron",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
