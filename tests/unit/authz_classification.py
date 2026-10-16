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
CURRENT_UNIT = "U7"
#: At this Unit's Build and Test no ``pending_unit`` may remain (NFR1.8).
FINAL_UNIT = "U7"

#: Components / Managers whose methods count as a decision when called
#: (matched on the receiver's name, e.g. ``self._job_manager.get``).
#: U4 adds ``ResourceSharing``: every listed method calls ``AccessPolicy``
#: (``ensure_manageable`` / ``can_read`` / ``shareable_groups`` / the
#: ``resource:read_all`` check) before it returns anything.
DECISION_PORTS: dict[str, frozenset[str]] = {
    "JobManager": frozenset({"get"}),
    "ResourceSharing": frozenset(
        {
            "list_page",
            "count",
            "collect_ids",
            "effective_tab",
            "update_shares",
            "bulk_update_shares",
            "delete_with_shares",
            "dialog_state",
            "build_change",
            "can_read_body",
            "can_manage_body",
        }
    ),
    # U5: every listed method decides who may use a file (owner, expiry,
    # attached parent; security-design.md §3) before it touches anything.
    # R-02: attachable_file / is_attachable are the one rule shared by a
    # discussion's start and attach_all (the user's own, unexpired, unattached).
    "FileAttachments": frozenset(
        {
            "attach_all",
            "readable_file",
            "can_read_file",
            "attachable_file",
            "is_attachable",
        }
    ),
    # U5: the claim is one conditional update whose condition is the owner
    # check (security-design.md §2); release / delete / mark_saved only act on
    # items that hold the request's unguessable claim token.
    # R-04: confirm_claim checks the same token (and "not saved") right before
    # each candidate is saved.
    "PersonaCandidateStore": frozenset(
        {"claim", "release", "delete", "mark_saved", "confirm_claim"}
    ),
    # U6: every listed method decides on the discussion (ensure_readable /
    # ensure_manageable / can_manage / can_read on each body) before it reads
    # messages or reports or writes anything (security-design.md §2, ADR-006).
    "DiscussionRecords": frozenset(
        {
            "save_new",
            "get",
            "get_manageable",
            "ensure_manageable",
            "can_manage",
            "list_bodies",
            "update_insights",
            "append_messages",
            "delete",
            "save_report",
            "update_report",
            "delete_report",
            "get_report",
            "export_markdown",
        }
    ),
    # U6: every listed method reads the participants' decision columns
    # (ResourceLookup.get_many) and judges them with AccessPolicy.can_read
    # before any persona body is read (BR2.8, BR7.1〜BR7.3).
    "ParticipantReader": frozenset(
        {"ensure_all_readable", "read_for_use", "can_read_all", "statuses"}
    ),
}

#: ``pending_unit`` count per Unit. Lower it when a Unit adds its decisions.
#: U4: PersonaManager の保存・更新・削除を CHECKS にし（3 件）、一覧・件数の古い
#: メソッド 3 件を消した（32 → 26）。
#: U5: pending("U5") を 0 件にした（26 件を CHECKS にするか消した）。議論・インタビュー
#: が使う PersonaManager.get_persona_unchecked を U6 に回し（35 → 36）、
#: DatasetManager の紐付けの 5 メソッドを消した（55 → 50）。
#: U6: pending("U6") を 0 件にした（36 件を CHECKS にするか、消すか非公開にした）。
#: U7: pending("U7") を 0 件にした（50 件を CHECKS / NO_USER_DATA にするか、消すか
#: 非公開にするか置き換えた）。これで残りは 0 件（FINAL_UNIT）。
PENDING_BASELINE: dict[str, int] = {}

CLASSIFICATION: dict[str, Entry] = {
    # agent_discussion_manager.py — U6（security-design.md §1。エージェントの組み立て・
    # 開始・保存・解放は非公開にし、Router が呼ぶ 2 つだけを残した）
    "AgentDiscussionManager.run_agent_discussion_streaming": CHECKS,  # ai:execute（BR2.8）
    "AgentDiscussionManager.run_agent_discussion_full": CHECKS,  # ai:execute（BR2.8）
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
    # dataset_manager.py — U7（設定は settings:manage、なければ 404。security-design.md 1.4）
    "DatasetManager.analyze_schema": CHECKS,
    "DatasetManager.download_csv_from_url": CHECKS,
    "DatasetManager.upload_csv": CHECKS,
    "DatasetManager.get_datasets": CHECKS,
    "DatasetManager.get_dataset": CHECKS,
    "DatasetManager.update_dataset": CHECKS,
    "DatasetManager.delete_dataset": CHECKS,
    "DatasetManager.scope_dialog": CHECKS,  # 利用範囲のダイアログ（M10）
    "DatasetManager.update_scope": CHECKS,  # ADD / DELETE（BR5.2）
    # 紐付けの 5 メソッドは U5 で PersonaManager へ移した（ADR-007）
    # discussion_manager.py — U6（security-design.md §1）
    "DiscussionManager.run_classic_discussion_streaming": CHECKS,  # ai:execute（BR2.8）
    "DiscussionManager.run_classic_discussion": CHECKS,  # ai:execute（BR2.8）
    "DiscussionManager.list_discussions": CHECKS,  # ResourceSharing + can_read（BR2.1）
    "DiscussionManager.list_accessible_discussions": CHECKS,  # 同上（API）
    "DiscussionManager.get_discussion": CHECKS,  # DiscussionRecords.get（BR2.4）
    "DiscussionManager.export_discussion": CHECKS,  # DiscussionRecords（BR9.1）
    "DiscussionManager.get_document_urls": CHECKS,  # FileAttachments.readable_file（BR5.1）
    "DiscussionManager.delete_discussion": CHECKS,  # DiscussionRecords.delete（BR1.5）
    "DiscussionManager.regenerate_insights": CHECKS,  # ai:execute + 管理できる人（BR2.5）
    "DiscussionManager.check_insights_allowed": CHECKS,  # 同上（API のジョブの前）
    "DiscussionManager.get_default_categories": NO_USER_DATA,  # 固定の既定値
    # file_manager.py — U5（security-design.md §1）
    "FileManager.validate_persona_source_file": NO_USER_DATA,  # 中身の検査だけ
    "FileManager.convert_file_to_markdown": NO_USER_DATA,  # 変換だけで保存しない（BR5.9）
    "FileManager.upload_discussion_document": CHECKS,  # resource:create、本人のキー
    "FileManager.upload_survey_image": CHECKS,  # resource:create、本人のキー
    "FileManager.upload_interview_attachment": CHECKS,  # resource:create（U6 BR3.6）
    "FileManager.preview_url_for_path": CHECKS,  # FileAttachments.readable_file（BR5.8）
    # interview_manager.py — U6（持ち主だけ、BR3.2。手で呼ぶ掃除・生のファイルの経路はなくした）
    "InterviewManager.start_interview_session": CHECKS,  # ai:execute（BR3.1）
    "InterviewManager.send_user_message": CHECKS,  # is_owner + 参加者の確かめ直し
    "InterviewManager.send_user_message_streaming": CHECKS,  # 同上
    "InterviewManager.save_interview_session": CHECKS,  # is_owner + 参加者（BR3.7）
    "InterviewManager.get_interview_session": CHECKS,  # is_owner（BR3.2）
    "InterviewManager.end_interview_session": CHECKS,  # is_owner（BR3.2）
    "InterviewManager.get_session_status": CHECKS,  # is_owner（BR3.2）
    "InterviewManager.get_active_sessions_count": NO_USER_DATA,  # 手元の件数だけ
    # job_manager.py — U2（submit は ai:execute、get は投入者の照合）
    "JobManager.submit": CHECKS,
    "JobManager.get": CHECKS,
    # persona_generation_manager.py — U5（候補は PersonaCandidates、BR1.1〜BR1.9）
    "PersonaGenerationManager.generate": CHECKS,  # ai:execute
    "PersonaGenerationManager.claim_candidates": CHECKS,  # 本人の照合（条件付き更新）
    "PersonaGenerationManager.finish_candidates": CHECKS,  # 押さえの印が一致するものだけ
    # persona_manager.py — U4 で所有・共有の判定を入れた分は CHECKS。
    # U5: 紐付け・プレビュー・候補からの保存・エクスポート・アバターも CHECKS。
    # U6: get_persona_unchecked をなくし、議論・インタビューは get_personas_for_use で読む
    "PersonaManager.save_persona": CHECKS,  # resource:create（BR6.1）
    "PersonaManager.get_persona": CHECKS,  # ensure_readable（BR4.5）
    "PersonaManager.get_personas_for_use": CHECKS,  # ParticipantReader（U6 BR2.8）
    "PersonaManager.save_generated_personas": CHECKS,  # resource:create（BR1.4）
    "PersonaManager.preview_binding_data": CHECKS,  # ensure_readable + can_use_asset
    "PersonaManager.export_persona": CHECKS,  # ensure_readable（BR7.1）
    "PersonaManager.list_personas": CHECKS,  # ResourceSharing + can_read（BR3.7）
    "PersonaManager.search_personas": CHECKS,
    "PersonaManager.list_accessible_personas": CHECKS,
    "PersonaManager.get_persona_detail": CHECKS,  # ensure_readable（BR6.2）
    "PersonaManager.describe_persona": CHECKS,  # ensure_readable + can_manage（R-07）
    "PersonaManager.get_persona_for_edit": CHECKS,  # ensure_manageable
    "PersonaManager.delete_persona": CHECKS,  # delete_with_shares（BR4.1）
    "PersonaManager.update_persona": CHECKS,  # ensure_manageable（BR6.7）
    "PersonaManager.avatar_upload_enabled": NO_USER_DATA,  # 設定の有無だけ
    "PersonaManager.set_avatar": CHECKS,  # ensure_manageable（BR4.3）
    "PersonaManager.delete_avatar": CHECKS,  # ensure_manageable（BR4.3）
    "PersonaManager.get_avatar_url": CHECKS,  # ensure_readable（BR4.4）
    "PersonaManager.get_kb_binding": CHECKS,  # ensure_readable（BR2.1）
    "PersonaManager.create_kb_binding": CHECKS,  # ensure_manageable + can_use_asset
    "PersonaManager.delete_kb_binding": CHECKS,  # ensure_manageable（BR2.2）
    "PersonaManager.get_dataset_bindings": CHECKS,  # ensure_readable（BR2.1）
    "PersonaManager.create_dataset_binding": CHECKS,  # ensure_manageable + can_use_asset
    "PersonaManager.delete_dataset_binding": CHECKS,  # ensure_manageable（BR2.2）
    # persona_memory_manager.py — U5（管理できる人だけ、BR4.1）
    "PersonaMemoryManager.ensure_memory_manageable": CHECKS,
    "PersonaMemoryManager.add_knowledge": CHECKS,
    "PersonaMemoryManager.get_memories": CHECKS,
    "PersonaMemoryManager.delete_memory": CHECKS,
    "PersonaMemoryManager.delete_all_memories": CHECKS,
    "PersonaMemoryManager.safe_get_memories": CHECKS,
    # report_manager.py — U6（生成は読める人、変更は管理できる人、BR4.1・BR4.2）
    "ReportManager.get_discussion": CHECKS,  # DiscussionRecords.get
    "ReportManager.generate_report_streaming": CHECKS,  # ai:execute + 読める人
    "ReportManager.generate_followup_report_streaming": CHECKS,  # 同上
    "ReportManager.get_report": CHECKS,  # DiscussionRecords.get_report（読める人）
    "ReportManager.export_report": CHECKS,  # 同上
    "ReportManager.can_manage": CHECKS,  # DiscussionRecords.can_manage（操作の出し分け）
    "ReportManager.save_report": CHECKS,  # DiscussionRecords（管理できる人）
    "ReportManager.update_report_content": CHECKS,  # 同上
    "ReportManager.delete_report": CHECKS,  # 同上
    # sharing_manager.py — U4（判定は ResourceSharing に任せる）
    "SharingManager.resource_type_of": NO_USER_DATA,  # 登録された種類の照合だけ
    "SharingManager.dialog": CHECKS,
    "SharingManager.bulk_dialog": CHECKS,
    "SharingManager.save": CHECKS,
    "SharingManager.bulk_save": CHECKS,
    # settings_manager.py — U7（設定は settings:manage、なければ 404。security-design.md 1.4）
    "SettingsManager.get_all_knowledge_bases": CHECKS,
    "SettingsManager.create_knowledge_base": CHECKS,
    "SettingsManager.delete_knowledge_base": CHECKS,
    "SettingsManager.scope_dialog": CHECKS,  # 利用範囲のダイアログ（M10）
    "SettingsManager.update_scope": CHECKS,  # ADD / DELETE（BR5.2）
    "SettingsManager.save_data_agent_settings": CHECKS,
    "SettingsManager.is_data_agent_available": NO_USER_DATA,  # 設定の有無だけ
    "SettingsManager.test_data_agent_connection": CHECKS,
    "SettingsManager.get_data_agent_settings": CHECKS,
    # survey_analysis_manager.py — U7（判定のあとだけ CSV とキャッシュ、BR2.4・BR2.11）
    "SurveyAnalysisManager.result_cache": NO_USER_DATA,  # キャッシュの実体を返すだけ
    "SurveyAnalysisManager.get_visual_analysis": CHECKS,  # ensure_readable
    "SurveyAnalysisManager.get_persona_statistics": CHECKS,  # ensure_readable
    "SurveyAnalysisManager.generate_insight_report": CHECKS,  # ai:execute + manage
    "SurveyAnalysisManager.generate_insight_report_streaming": CHECKS,  # 同上
    "SurveyAnalysisManager.save_insight_report": CHECKS,  # 同上
    # survey_dataset_manager.py — U7（管理は settings:manage、参照は can_use_asset）
    "SurveyDatasetManager.check_nemotron_status": NO_USER_DATA,  # 取り込みの有無だけ
    "SurveyDatasetManager.download_nemotron_dataset": CHECKS,
    "SurveyDatasetManager.ensure_manageable": CHECKS,  # 裏で動かす前の止め
    "SurveyDatasetManager.import_scope_choices": CHECKS,  # 取り込みの利用範囲の欄
    "SurveyDatasetManager.list_persona_data": CHECKS,  # can_use_asset（BR4.2）
    "SurveyDatasetManager.get_custom_persona_data": CHECKS,  # can_use_asset
    "SurveyDatasetManager.upload_custom_dataset": CHECKS,
    "SurveyDatasetManager.delete_custom_dataset": CHECKS,
    "SurveyDatasetManager.rename_custom_dataset": CHECKS,
    "SurveyDatasetManager.scope_dialog": CHECKS,
    "SurveyDatasetManager.update_scope": CHECKS,
    "SurveyDatasetManager.extract_segment_from_dwh": CHECKS,
    "SurveyDatasetManager.suggest_column_mapping": CHECKS,
    "SurveyDatasetManager.upload_temp_file": CHECKS,
    "SurveyDatasetManager.download_temp_file": CHECKS,
    "SurveyDatasetManager.delete_temp_file": CHECKS,
    "SurveyDatasetManager.parse_csv_columns": CHECKS,
    "SurveyDatasetManager.get_standard_columns": NO_USER_DATA,  # 固定の列定義
    "SurveyDatasetManager.preview_system_prompt": CHECKS,
    "SurveyDatasetManager.get_available_filter_values": CHECKS,  # データソースの判定
    "SurveyDatasetManager.get_filtered_count": CHECKS,  # 同上
    "SurveyDatasetManager.get_preview_stats": CHECKS,  # 同上
    "SurveyDatasetManager.get_datasource_count": CHECKS,  # 同上
    # survey_execution_manager.py — U7（開始は ai:execute・テンプレート・元データ、BR2.1。
    # execute_survey・start_survey は非公開にし、get_all_surveys は list_surveys にした）
    "SurveyExecutionManager.result_cache": NO_USER_DATA,  # キャッシュの実体を返すだけ
    "SurveyExecutionManager.create_survey": CHECKS,
    "SurveyExecutionManager.list_surveys": CHECKS,  # ResourceSharing + can_read
    "SurveyExecutionManager.get_survey": CHECKS,  # ensure_readable
    "SurveyExecutionManager.get_survey_detail": CHECKS,  # 同上 + can_manage
    "SurveyExecutionManager.get_download_url": CHECKS,  # 同上
    "SurveyExecutionManager.download_results_csv": CHECKS,  # 同上
    "SurveyExecutionManager.get_result_image_url": CHECKS,  # 同上 + 写しの画像だけ
    "SurveyExecutionManager.delete_survey": CHECKS,  # delete_with_shares
    "SurveyExecutionManager.normalize_filters": NO_USER_DATA,  # 入力の整形だけ
    "SurveyExecutionManager.generate_default_survey_name": NO_USER_DATA,  # 名前の生成だけ
    # survey_template_manager.py — U7（所有・共有・判定、BR1.1〜BR1.7、BR2.3。
    # get_all_templates は list_templates にした）
    "SurveyTemplateManager.create_template": CHECKS,  # resource:create
    "SurveyTemplateManager.list_templates": CHECKS,  # ResourceSharing + can_read
    "SurveyTemplateManager.get_template": CHECKS,  # ensure_readable
    "SurveyTemplateManager.get_template_for_edit": CHECKS,  # ensure_manageable
    "SurveyTemplateManager.update_template": CHECKS,  # ensure_manageable
    "SurveyTemplateManager.delete_template": CHECKS,  # delete_with_shares
    "SurveyTemplateManager.generate_ai_chat_response": CHECKS,  # ai:execute
    "SurveyTemplateManager.generate_ai_questions_draft": CHECKS,  # ai:execute
}
