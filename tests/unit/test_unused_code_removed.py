"""本番のコードから呼ばれなくなったものを消したことの検査。

どれも別の入口に置き換わったか、呼び手が先の整理で消えていた。
- ``get_survey`` → ``get_survey_detail``
- ``download_results_csv`` → ``get_download_url``（署名付き URL）
- ``generate_insight_report`` → ``generate_insight_report_streaming`` と
  ``save_insight_report``
- ``download_csv_from_url`` → 各 Manager が ``DataAgentService.download_csv`` を呼ぶ
"""

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module, owner, name",
    [
        (
            "src.managers.survey_execution_manager",
            "SurveyExecutionManager",
            "get_survey",
        ),
        (
            "src.managers.survey_execution_manager",
            "SurveyExecutionManager",
            "download_results_csv",
        ),
        (
            "src.managers.survey_analysis_manager",
            "SurveyAnalysisManager",
            "generate_insight_report",
        ),
        ("src.managers.dataset_manager", "DatasetManager", "download_csv_from_url"),
        ("src.services.agent_service", "AgentService", "create_data_agent_tools"),
        (
            "src.services.custom_persona_data_store",
            "CustomPersonaDataStore",
            "list_ids",
        ),
        ("src.services.discussion_store", "DiscussionStore", "message_discussion_ids"),
        ("src.services.discussion_store", "DiscussionStore", "report_discussion_ids"),
        ("src.services.resource_share_store", "ResourceShareStore", "get_items"),
        ("src.services.resource_share_store", "ResourceShareStore", "_batch_get"),
        ("src.models.errors", "ErrorCode", "SURVEY_REPORT_GENERATION_FAILED"),
        ("src.models.persona_candidate", "PersonaCandidate", "behavior_dataset"),
        ("src.services.dataset_analysis.service", None, "BindingDescriptor"),
        ("src.services.dataset_analysis.service", None, "SourceDescriptor"),
        ("web.routers.api", None, "DiscussionResponse"),
        ("web.routers.persona", None, "MemoryUIError"),
    ],
)
def test_is_gone(module, owner, name):
    target = importlib.import_module(module)
    if owner is not None:
        target = getattr(target, owner)
    assert not hasattr(target, name)


def test_pagination_helpers_are_gone():
    assert not (ROOT / "web/routers/_pagination.py").exists()
