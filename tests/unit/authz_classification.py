"""Manager の公開メソッドの分類の一覧（BR2.15、NFR1.2、entities.md AccessClassification）。

``tests/unit/test_authz_classification.py`` がこの一覧と実際のコードを突き合わせる。

- ``CHECKS``: 本体（か同じクラスの非公開メソッド 1 段）で判定を呼ぶ
- ``exempt(Reason.NO_USER_DATA)``: 利用者のデータを扱わない
- ``exempt(Reason.ORG_ASSET_LISTING)``: 組織資産の一覧を全員に見せる
- ``pending("U5")`` など: 判定をまだ入れていない。書いた Unit で判定を入れ、
  ``CHECKS`` に変えて ``PENDING_BASELINE`` を下げる

新しい公開メソッドを足したら、ここにも足す（足さなければ検査が失敗する）。
"""

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    CHECKS = "checks"
    EXEMPT = "exempt"


class Reason(StrEnum):
    """Why a public Manager method does not call an authorization decision."""

    ORG_ASSET_LISTING = "org_asset_listing"
    NO_USER_DATA = "no_user_data"
    PENDING_UNIT = "pending_unit"


@dataclass(frozen=True)
class Entry:
    kind: Kind
    reason: Reason | None = None
    pending_unit: str | None = None


def pending(unit: str) -> Entry:
    """Not checked yet; ``unit`` adds the decision."""
    return Entry(Kind.EXEMPT, Reason.PENDING_UNIT, unit)


CHECKS = Entry(Kind.CHECKS)
NO_USER_DATA = Entry(Kind.EXEMPT, Reason.NO_USER_DATA)

#: Units in build order. ``pending_unit`` must name one after ``CURRENT_UNIT``.
UNIT_ORDER: tuple[str, ...] = ("U1", "U2", "U3", "U4", "U5", "U6", "U7")
#: The Unit being built. Each Unit's Code Generation moves it forward.
CURRENT_UNIT = "U2"
#: At this Unit's Build and Test no ``pending_unit`` may remain (NFR1.8).
FINAL_UNIT = "U7"

#: Components / Managers whose methods count as a decision when called
#: (matched on the receiver's name, e.g. ``self._job_manager.get``).
#: U4 adds ``ResourceSharing``.
DECISION_PORTS: dict[str, frozenset[str]] = {
    "JobManager": frozenset({"get"}),
}

#: ``pending_unit`` count per Unit. Lower it when a Unit adds its decisions.
PENDING_BASELINE: dict[str, int] = {"U5": 32, "U6": 35, "U7": 55}

CLASSIFICATION: dict[str, Entry] = {
    # agent_discussion_manager.py
    "AgentDiscussionManager.create_persona_agents": pending("U6"),
    "AgentDiscussionManager.create_facilitator_agent": pending("U6"),
    "AgentDiscussionManager.start_agent_discussion": pending("U6"),
    "AgentDiscussionManager.save_agent_discussion": pending("U6"),
    "AgentDiscussionManager.start_agent_discussion_streaming": pending("U6"),
    "AgentDiscussionManager.cleanup_agents": pending("U6"),
    "AgentDiscussionManager.run_agent_discussion_streaming": pending("U6"),
    "AgentDiscussionManager.run_agent_discussion_full": pending("U6"),
    # auth_manager.py — ログインそのもの。利用者のリソースを扱わない
    "AuthManager.mode": NO_USER_DATA,
    "AuthManager.secure_cookies": NO_USER_DATA,
    "AuthManager.check_startup": NO_USER_DATA,
    "AuthManager.sanitize_next": NO_USER_DATA,
    "AuthManager.begin_login": NO_USER_DATA,
    "AuthManager.complete_login": NO_USER_DATA,
    "AuthManager.login_local": NO_USER_DATA,
    "AuthManager.start_local_session": NO_USER_DATA,
    "AuthManager.check_request": NO_USER_DATA,
    "AuthManager.logout": NO_USER_DATA,
    # dataset_manager.py
    "DatasetManager.analyze_schema": pending("U7"),
    "DatasetManager.download_csv_from_url": pending("U7"),
    "DatasetManager.upload_csv": pending("U7"),
    "DatasetManager.get_datasets": pending("U7"),
    "DatasetManager.get_dataset": pending("U7"),
    "DatasetManager.update_dataset": pending("U7"),
    "DatasetManager.delete_dataset": pending("U7"),
    "DatasetManager.save_binding": pending("U7"),
    "DatasetManager.get_bindings_by_persona": pending("U7"),
    "DatasetManager.delete_binding": pending("U7"),
    "DatasetManager.set_persona_bindings": pending("U7"),
    "DatasetManager.preview_binding_data": pending("U7"),
    # discussion_manager.py
    "DiscussionManager.start_discussion": pending("U6"),
    "DiscussionManager.generate_insights": pending("U6"),
    "DiscussionManager.save_discussion": pending("U6"),
    "DiscussionManager.get_discussion": pending("U6"),
    "DiscussionManager.get_discussion_history": pending("U6"),
    "DiscussionManager.delete_discussion": pending("U6"),
    "DiscussionManager.regenerate_insights": pending("U6"),
    "DiscussionManager.facilitate_discussion_streaming": pending("U6"),
    "DiscussionManager.get_document_presigned_urls": pending("U6"),
    "DiscussionManager.run_classic_discussion_streaming": pending("U6"),
    "DiscussionManager.run_classic_discussion": pending("U6"),
    "DiscussionManager.get_default_categories": NO_USER_DATA,  # 固定の既定値
    # file_manager.py
    "FileManager.validate_persona_source_file": pending("U5"),
    "FileManager.convert_file_to_markdown": pending("U5"),
    "FileManager.upload_discussion_document": pending("U5"),
    "FileManager.upload_survey_image": pending("U5"),
    "FileManager.upload_knowledge_file": pending("U5"),
    # interview_manager.py
    "InterviewManager.start_interview_session": pending("U6"),
    "InterviewManager.send_user_message": pending("U6"),
    "InterviewManager.send_user_message_streaming": pending("U6"),
    "InterviewManager.save_interview_session": pending("U6"),
    "InterviewManager.get_interview_session": pending("U6"),
    "InterviewManager.end_interview_session": pending("U6"),
    "InterviewManager.get_session_status": pending("U6"),
    "InterviewManager.cleanup_inactive_sessions": pending("U6"),
    "InterviewManager.get_active_sessions_count": NO_USER_DATA,  # 件数だけ
    "InterviewManager.send_user_message_with_files": pending("U6"),
    "InterviewManager.send_user_message_streaming_with_files": pending("U6"),
    # job_manager.py — U2（submit は ai:execute、get は投入者の照合）
    "JobManager.submit": CHECKS,
    "JobManager.get": CHECKS,
    # persona_generation_manager.py
    "PersonaGenerationManager.generate_and_cache": pending("U5"),
    "PersonaGenerationManager.get_cached_persona": pending("U5"),
    "PersonaGenerationManager.pop_cached_persona": pending("U5"),
    "PersonaGenerationManager.get_cached_behavior_datasets": pending("U5"),
    "PersonaGenerationManager.pop_cached_behavior_datasets": pending("U5"),
    "PersonaGenerationManager.build_and_cache_behavior_datasets": pending("U5"),
    # persona_manager.py
    "PersonaManager.save_persona": pending("U5"),
    "PersonaManager.get_persona": pending("U5"),
    "PersonaManager.get_all_personas": pending("U5"),
    "PersonaManager.get_all_personas_full": pending("U5"),
    "PersonaManager.delete_persona": pending("U5"),
    "PersonaManager.update_persona": pending("U5"),
    "PersonaManager.avatar_upload_enabled": NO_USER_DATA,  # 設定の有無だけ
    "PersonaManager.set_avatar": pending("U5"),
    "PersonaManager.delete_avatar": pending("U5"),
    "PersonaManager.get_avatar_url": pending("U5"),
    "PersonaManager.get_persona_count": pending("U5"),
    "PersonaManager.get_kb_binding": pending("U5"),
    "PersonaManager.create_kb_binding": pending("U5"),
    "PersonaManager.delete_kb_binding": pending("U5"),
    "PersonaManager.get_dataset_bindings": pending("U5"),
    "PersonaManager.create_dataset_binding": pending("U5"),
    "PersonaManager.delete_dataset_binding": pending("U5"),
    # persona_memory_manager.py
    "PersonaMemoryManager.add_knowledge": pending("U5"),
    "PersonaMemoryManager.get_memories": pending("U5"),
    "PersonaMemoryManager.delete_memory": pending("U5"),
    "PersonaMemoryManager.delete_all_memories": pending("U5"),
    "PersonaMemoryManager.safe_get_memories": pending("U5"),
    # report_manager.py
    "ReportManager.get_discussion": pending("U6"),
    "ReportManager.generate_report_streaming": pending("U6"),
    "ReportManager.generate_followup_report_streaming": pending("U6"),
    "ReportManager.save_report": pending("U6"),
    "ReportManager.update_report_content": pending("U6"),
    "ReportManager.delete_report": pending("U6"),
    # settings_manager.py
    "SettingsManager.get_all_knowledge_bases": pending("U7"),
    "SettingsManager.create_knowledge_base": pending("U7"),
    "SettingsManager.delete_knowledge_base": pending("U7"),
    "SettingsManager.save_data_agent_settings": pending("U7"),
    "SettingsManager.is_data_agent_available": NO_USER_DATA,  # 設定の有無だけ
    "SettingsManager.test_data_agent_connection": pending("U7"),
    # survey_analysis_manager.py
    "SurveyAnalysisManager.get_visual_analysis": pending("U7"),
    "SurveyAnalysisManager.get_persona_statistics": pending("U7"),
    "SurveyAnalysisManager.generate_insight_report": pending("U7"),
    "SurveyAnalysisManager.generate_insight_report_streaming": pending("U7"),
    "SurveyAnalysisManager.save_insight_report": pending("U7"),
    # survey_dataset_manager.py
    "SurveyDatasetManager.check_nemotron_status": pending("U7"),
    "SurveyDatasetManager.download_nemotron_dataset": pending("U7"),
    "SurveyDatasetManager.list_custom_datasets": pending("U7"),
    "SurveyDatasetManager.upload_custom_dataset": pending("U7"),
    "SurveyDatasetManager.load_dataset_metadata": pending("U7"),
    "SurveyDatasetManager.delete_custom_dataset": pending("U7"),
    "SurveyDatasetManager.extract_segment_from_dwh": pending("U7"),
    "SurveyDatasetManager.suggest_column_mapping": pending("U7"),
    "SurveyDatasetManager.upload_temp_file": pending("U7"),
    "SurveyDatasetManager.download_temp_file": pending("U7"),
    "SurveyDatasetManager.delete_temp_file": pending("U7"),
    "SurveyDatasetManager.parse_csv_columns": pending("U7"),
    "SurveyDatasetManager.get_standard_columns": NO_USER_DATA,  # 固定の列定義
    "SurveyDatasetManager.preview_system_prompt": pending("U7"),
    "SurveyDatasetManager.get_available_filter_values": pending("U7"),
    "SurveyDatasetManager.get_filtered_count": pending("U7"),
    "SurveyDatasetManager.get_preview_stats": pending("U7"),
    "SurveyDatasetManager.get_datasource_count": pending("U7"),
    "SurveyDatasetManager.get_image_presigned_url": pending("U7"),
    # survey_execution_manager.py
    "SurveyExecutionManager.create_survey": pending("U7"),
    "SurveyExecutionManager.execute_survey": pending("U7"),
    "SurveyExecutionManager.start_survey": pending("U7"),
    "SurveyExecutionManager.get_survey": pending("U7"),
    "SurveyExecutionManager.get_all_surveys": pending("U7"),
    "SurveyExecutionManager.delete_survey": pending("U7"),
    "SurveyExecutionManager.get_download_url": pending("U7"),
    "SurveyExecutionManager.download_results_csv": pending("U7"),
    "SurveyExecutionManager.normalize_filters": NO_USER_DATA,  # 入力の整形だけ
    "SurveyExecutionManager.generate_default_survey_name": NO_USER_DATA,  # 名前の生成だけ
    # survey_template_manager.py
    "SurveyTemplateManager.create_template": pending("U7"),
    "SurveyTemplateManager.get_template": pending("U7"),
    "SurveyTemplateManager.get_all_templates": pending("U7"),
    "SurveyTemplateManager.update_template": pending("U7"),
    "SurveyTemplateManager.delete_template": pending("U7"),
    "SurveyTemplateManager.generate_ai_chat_response": pending("U7"),
    "SurveyTemplateManager.generate_ai_questions_draft": pending("U7"),
}
