"""Classification of every public Manager method.

``tests/unit/test_authz_classification.py`` checks this list against the actual code.

- ``CHECKS``: the body (or one private method of the same class) calls an authorization check
- ``exempt(Reason.NO_USER_DATA)``: does not handle user data
- ``exempt(Reason.ORG_ASSET_LISTING)``: lists organization assets to everyone
- ``pending(...)``: no check yet; once added, switch the entry to ``CHECKS`` and lower
    ``PENDING_BASELINE``.

When adding a public method, add it here too (the check fails otherwise).
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
#: At the final Unit no ``pending_unit`` may remain.
FINAL_UNIT = "U7"

#: Components / Managers whose methods count as a decision when called
#: (matched on the receiver's name, e.g. ``self._job_manager.get``).
#: ``ResourceSharing``: every listed method calls ``AccessPolicy``
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
    # Every listed method decides who may use a file (owner, expiry,
    # attached parent) before it touches anything.
    # attachable_file / is_attachable are the one rule shared by a
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
    # The claim is one conditional update whose condition is the owner
    # check; release / delete / mark_saved only act on
    # items that hold the request's unguessable claim token.
    # confirm_claim checks the same token (and "not saved") right before
    # each candidate is saved.
    "PersonaCandidateStore": frozenset(
        {"claim", "release", "delete", "mark_saved", "confirm_claim"}
    ),
    # Every listed method decides on the discussion (ensure_readable /
    # ensure_manageable / can_manage / can_read on each body) before it reads
    # messages or reports or writes anything.
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
    # Every listed method reads the participants' decision columns
    # (ResourceLookup.get_many) and judges them with AccessPolicy.can_read
    # before any persona body is read.
    "ParticipantReader": frozenset(
        {"ensure_all_readable", "read_for_use", "can_read_all", "statuses"}
    ),
    # ensure_available checks ai:execute (AccessPolicy.require) and the public
    # availability rule. is_available returns the same check as a bool (tab/page display).
    # Each SourceFileCatalog method reads/writes conditioned on owner_sub, so
    # someone else's file and a missing one both give the same NOT_FOUND.
    "AdvancedGenerationPolicy": frozenset(
        {"ensure_available", "is_available", "availability"}
    ),
    "SourceFileCatalog": frozenset(
        {
            "get_owned",
            "ready_table",
            # the owner's ready files of one generation
            "ready_sources",
            "record_url_refresh",
            "begin_inspection",
            "cancel_upload",
            # read/delete only the caller's own files
            "list_owned",
            "delete_owned",
        }
    ),
}

#: ``pending_unit`` count per Unit. Lower it when a Unit adds its decisions.
#: Lower the count when a Unit adds its decisions; currently all resolved.
PENDING_BASELINE: dict[str, int] = {}

CLASSIFICATION: dict[str, Entry] = {
    # advanced_generation/manager.py: start uses the availability check, reads are
    # owner-only; someone else's generation and a missing one give the same GENERATION_NOT_FOUND
    "AdvancedPersonaGenerationManager.is_available": CHECKS,  # is_available
    "AdvancedPersonaGenerationManager.start_generation": CHECKS,  # ensure_available
    # Cancelling an accepted start and running it (pool thread). Takes no user request and
    # returns nothing; it only advances or closes a generation that passed the start check (same as SurveyRunManager)
    "AdvancedPersonaGenerationManager.abort_start": NO_USER_DATA,
    "AdvancedPersonaGenerationManager.run": NO_USER_DATA,
    "AdvancedPersonaGenerationManager.running_generation_id": CHECKS,  # is_owner
    "AdvancedPersonaGenerationManager.elapsed_clock": CHECKS,  # _owned → is_owner
    "AdvancedPersonaGenerationManager.stream_events": CHECKS,  # _owned → is_owner
    "AdvancedPersonaGenerationManager.get_result": CHECKS,  # _owned → is_owner
    "AdvancedPersonaGenerationManager.get_record": CHECKS,  # _owned -> is_owner
    # cancel / approve / replan are owner-only too
    "AdvancedPersonaGenerationManager.request_control": CHECKS,  # _owned → is_owner
    # Entry for plan approval and replanning only (cancel goes through request_control)
    "AdvancedPersonaGenerationManager.decide_plan": CHECKS,  # _owned → is_owner
    # agent_discussion_manager.py: agent assembly, start, save and release are
    # private; only the two methods the Router calls remain
    "AgentDiscussionManager.run_agent_discussion_streaming": CHECKS,  # ai:execute
    "AgentDiscussionManager.run_agent_discussion_full": CHECKS,  # ai:execute
    # auth_manager.py: login itself; does not handle user resources
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
    # Closed-network login (pre-login; does not handle user data)
    "AuthManager.password_policy": NO_USER_DATA,
    "AuthManager.login_admin_api": NO_USER_DATA,
    "AuthManager.check_challenge": NO_USER_DATA,
    "AuthManager.complete_new_password": NO_USER_DATA,
    # dataset_manager.py: settings need settings:manage, else 404
    "DatasetManager.analyze_schema": CHECKS,
    "DatasetManager.upload_csv": CHECKS,
    "DatasetManager.get_datasets": CHECKS,
    "DatasetManager.get_dataset": CHECKS,
    "DatasetManager.update_dataset": CHECKS,
    "DatasetManager.delete_dataset": CHECKS,
    "DatasetManager.scope_dialog": CHECKS,  # scope dialog
    "DatasetManager.update_scope": CHECKS,  # ADD / DELETE
    # The five binding methods moved to PersonaManager
    # discussion_manager.py
    "DiscussionManager.run_classic_discussion_streaming": CHECKS,  # ai:execute
    "DiscussionManager.run_classic_discussion": CHECKS,  # ai:execute
    "DiscussionManager.list_discussions": CHECKS,  # ResourceSharing + can_read
    "DiscussionManager.list_accessible_discussions": CHECKS,  # same (API)
    "DiscussionManager.get_discussion": CHECKS,  # DiscussionRecords.get
    "DiscussionManager.export_discussion": CHECKS,  # DiscussionRecords
    "DiscussionManager.servable_document_ids": CHECKS,  # FileAttachments.readable_file
    # Reads the discussion + its attachments + readable_file
    "DiscussionManager.deliver_document": CHECKS,
    "DiscussionManager.delete_discussion": CHECKS,  # DiscussionRecords.delete
    "DiscussionManager.regenerate_insights": CHECKS,  # ai:execute + can manage
    "DiscussionManager.check_insights_allowed": CHECKS,  # same (before the API job)
    "DiscussionManager.get_default_categories": NO_USER_DATA,  # fixed defaults
    # file_manager.py
    "FileManager.validate_persona_source_file": NO_USER_DATA,  # content validation only
    "FileManager.convert_file_to_markdown": NO_USER_DATA,  # converts only; does not store
    "FileManager.upload_discussion_document": CHECKS,  # resource:create, caller's own key
    "FileManager.upload_survey_image": CHECKS,  # resource:create, caller's own key
    "FileManager.upload_interview_attachment": CHECKS,  # resource:create
    "FileManager.check_preview_path": CHECKS,  # FileAttachments.readable_file
    "FileManager.deliver_preview": CHECKS,  # serves after the same check
    # interview_manager.py: owner-only (no manual cleanup or raw-file path)
    "InterviewManager.start_interview_session": CHECKS,  # ai:execute
    "InterviewManager.send_user_message": CHECKS,  # ensure_owner + re-verify participants
    "InterviewManager.send_user_message_streaming": CHECKS,  # same
    "InterviewManager.save_interview_session": CHECKS,  # ensure_owner + participants
    "InterviewManager.get_interview_session": CHECKS,  # ensure_owner
    "InterviewManager.end_interview_session": CHECKS,  # ensure_owner
    "InterviewManager.get_session_status": CHECKS,  # ensure_owner
    "InterviewManager.get_active_sessions_count": NO_USER_DATA,  # local count only
    # job_manager.py: submit needs ai:execute, get checks the submitter
    "JobManager.submit": CHECKS,
    "JobManager.get": CHECKS,
    # persona_generation_manager.py: candidates go through PersonaCandidates
    "PersonaGenerationManager.generate": CHECKS,  # ai:execute
    "PersonaGenerationManager.ensure_data_agent_allowed": CHECKS,  # ai:execute
    "PersonaGenerationManager.claim_candidates": CHECKS,  # verifies the caller (conditional update)
    "PersonaGenerationManager.finish_candidates": CHECKS,  # only the one matching the claim token
    # persona_manager.py: methods with ownership/sharing checks are CHECKS,
    # including bindings, preview, save from candidates, export and avatar.
    # Discussion/interview read through get_personas_for_use
    "PersonaManager.save_persona": CHECKS,  # resource:create
    "PersonaManager.get_persona": CHECKS,  # ensure_readable
    # analysis record of a saved persona (after ensure_readable)
    "PersonaManager.get_analysis_record": CHECKS,
    "PersonaManager.get_personas_for_use": CHECKS,  # ParticipantReader
    "PersonaManager.save_generated_personas": CHECKS,  # resource:create
    "PersonaManager.preview_binding_data": CHECKS,  # ensure_readable + can_use_asset
    "PersonaManager.export_persona": CHECKS,  # ensure_readable
    "PersonaManager.list_personas": CHECKS,  # ResourceSharing + can_read
    "PersonaManager.search_personas": CHECKS,
    "PersonaManager.list_accessible_personas": CHECKS,
    "PersonaManager.get_persona_detail": CHECKS,  # ensure_readable
    "PersonaManager.describe_persona": CHECKS,  # ensure_readable + can_manage
    "PersonaManager.get_persona_for_edit": CHECKS,  # ensure_manageable
    "PersonaManager.delete_persona": CHECKS,  # delete_with_shares
    "PersonaManager.update_persona": CHECKS,  # ensure_manageable
    "PersonaManager.avatar_upload_enabled": NO_USER_DATA,  # whether the setting is on only
    "PersonaManager.set_avatar": CHECKS,  # ensure_manageable
    "PersonaManager.delete_avatar": CHECKS,  # ensure_manageable
    "PersonaManager.deliver_avatar": CHECKS,  # ensure_readable
    "PersonaManager.get_kb_binding": CHECKS,  # ensure_readable
    "PersonaManager.create_kb_binding": CHECKS,  # ensure_manageable + can_use_asset
    "PersonaManager.delete_kb_binding": CHECKS,  # ensure_manageable
    "PersonaManager.get_dataset_bindings": CHECKS,  # ensure_readable
    "PersonaManager.create_dataset_binding": CHECKS,  # ensure_manageable + can_use_asset
    "PersonaManager.delete_dataset_binding": CHECKS,  # ensure_manageable
    # persona_memory_manager.py: managers only
    "PersonaMemoryManager.ensure_memory_manageable": CHECKS,
    # Only checks the length of the given string (the Router calls ensure_memory_manageable first)
    "PersonaMemoryManager.check_knowledge_file_content": NO_USER_DATA,
    "PersonaMemoryManager.add_knowledge": CHECKS,
    "PersonaMemoryManager.get_memories": CHECKS,
    "PersonaMemoryManager.delete_memory": CHECKS,
    "PersonaMemoryManager.delete_all_memories": CHECKS,
    "PersonaMemoryManager.safe_get_memories": CHECKS,
    # report_manager.py: generate needs read access, changes need manage access
    "ReportManager.get_discussion": CHECKS,  # DiscussionRecords.get
    "ReportManager.ensure_data_agent_allowed": CHECKS,  # ai:execute
    "ReportManager.generate_report_streaming": CHECKS,  # ai:execute + reader
    "ReportManager.generate_followup_report_streaming": CHECKS,  # same
    "ReportManager.get_report": CHECKS,  # DiscussionRecords.get_report (reader)
    "ReportManager.export_report": CHECKS,  # same
    "ReportManager.can_manage": CHECKS,  # DiscussionRecords.can_manage (control display)
    "ReportManager.save_report": CHECKS,  # DiscussionRecords (manager)
    "ReportManager.update_report_content": CHECKS,  # same
    "ReportManager.delete_report": CHECKS,  # same
    # sharing_manager.py: decisions delegated to ResourceSharing
    "SharingManager.resource_type_of": NO_USER_DATA,  # registered-type lookup only
    "SharingManager.dialog": CHECKS,
    "SharingManager.bulk_dialog": CHECKS,
    "SharingManager.save": CHECKS,
    "SharingManager.bulk_save": CHECKS,
    # settings_manager.py: settings need settings:manage, else 404
    # source_upload_manager.py: availability check + owner-only
    "SourceUploadManager.start_upload": CHECKS,  # ensure_available
    "SourceUploadManager.refresh_part_urls": CHECKS,  # ensure_available + owner
    "SourceUploadManager.complete_upload": CHECKS,  # ensure_available + owner
    "SourceUploadManager.get_upload": CHECKS,  # SourceFileCatalog.get_owned
    "SourceUploadManager.cancel_upload": CHECKS,  # SourceFileCatalog.get_owned
    # Own files only
    "SourceUploadManager.list_uploads": CHECKS,  # ensure_available + owner_sub GSI
    "SourceUploadManager.delete_source": CHECKS,  # SourceFileCatalog.get_owned + condition expression
    # Only converts already-read own files into list-row values (no reads)
    "SourceUploadManager.list_item": NO_USER_DATA,
    "SourceUploadManager.inspection_stuck": NO_USER_DATA,
    "SettingsManager.get_all_knowledge_bases": CHECKS,
    "SettingsManager.create_knowledge_base": CHECKS,
    "SettingsManager.delete_knowledge_base": CHECKS,
    "SettingsManager.scope_dialog": CHECKS,  # scope dialog
    "SettingsManager.update_scope": CHECKS,  # ADD / DELETE
    "SettingsManager.save_data_agent_settings": CHECKS,
    "SettingsManager.is_data_agent_available": NO_USER_DATA,  # whether the setting is on only
    "SettingsManager.is_data_agent_allowed": NO_USER_DATA,  # environment fact only
    # Environment fact only (ENABLE_ADVANCED_GENERATION)
    "SettingsManager.is_advanced_generation_section_offered": NO_USER_DATA,
    "SettingsManager.test_data_agent_connection": CHECKS,
    "SettingsManager.get_data_agent_settings": CHECKS,
    # GET returns 404 without settings:manage; save returns 403
    "AdvancedPersonaGenerationManager.detailed_analysis_offered": CHECKS,  # detailed analysis offered
    "SettingsManager.advanced_generation_section": CHECKS,
    "SettingsManager.save_advanced_generation_setting": CHECKS,
    # survey_analysis_manager.py: CSV and cache only after the check
    "SurveyAnalysisManager.result_cache": NO_USER_DATA,  # returns the cache object only
    "SurveyAnalysisManager.get_visual_analysis": CHECKS,  # ensure_readable
    "SurveyAnalysisManager.get_persona_statistics": CHECKS,  # ensure_readable
    "SurveyAnalysisManager.generate_insight_report_streaming": CHECKS,  # ai:execute + manage
    "SurveyAnalysisManager.save_insight_report": CHECKS,  # same
    # survey_dataset_manager.py: manage needs settings:manage, reads need can_use_asset
    "SurveyDatasetManager.check_nemotron_status": NO_USER_DATA,  # whether data is imported only
    "SurveyDatasetManager.download_nemotron_dataset": CHECKS,
    "SurveyDatasetManager.ensure_manageable": CHECKS,  # guard before background work
    "SurveyDatasetManager.ensure_nemotron_downloadable": CHECKS,  # same
    "SurveyDatasetManager.ensure_data_agent_allowed": CHECKS,  # guard before streaming
    "SurveyDatasetManager.import_scope_choices": CHECKS,  # import scope field
    "SurveyDatasetManager.list_persona_data": CHECKS,  # can_use_asset
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
    "SurveyDatasetManager.get_standard_columns": NO_USER_DATA,  # fixed column definitions
    "SurveyDatasetManager.preview_system_prompt": CHECKS,
    "SurveyDatasetManager.get_available_filter_values": CHECKS,  # data-source check
    "SurveyDatasetManager.get_filtered_count": CHECKS,  # same
    "SurveyDatasetManager.get_preview_stats": CHECKS,  # same
    "SurveyDatasetManager.get_datasource_count": CHECKS,  # same
    # survey_execution_manager.py: start needs ai:execute, template and source data.
    # execute_survey / start_survey are private; list_surveys replaces get_all_surveys
    "SurveyExecutionManager.result_cache": NO_USER_DATA,  # returns the cache object only
    "SurveyExecutionManager.create_survey": CHECKS,
    "SurveyExecutionManager.list_surveys": CHECKS,  # ResourceSharing + can_read
    "SurveyExecutionManager.get_survey_detail": CHECKS,  # ensure_readable + can_manage
    "SurveyExecutionManager.deliver_result_csv": CHECKS,  # same
    # same + copied images only
    "SurveyExecutionManager.deliver_result_image": CHECKS,
    "SurveyExecutionManager.delete_survey": CHECKS,  # delete_with_shares
    "SurveyExecutionManager.normalize_filters": NO_USER_DATA,  # input normalization only
    "SurveyExecutionManager.generate_default_survey_name": NO_USER_DATA,  # name generation only
    # survey_run_manager.py: runner (web-task background). Takes no user request and
    # returns nothing; it only advances surveys that passed the check at creation
    "SurveyRunManager.advance": NO_USER_DATA,
    "SurveyRunManager.release_lease": NO_USER_DATA,  # releases the lease record only
    # survey_template_manager.py: ownership, sharing and checks.
    # list_templates replaces get_all_templates
    "SurveyTemplateManager.create_template": CHECKS,  # resource:create
    "SurveyTemplateManager.list_templates": CHECKS,  # ResourceSharing + can_read
    "SurveyTemplateManager.get_template": CHECKS,  # ensure_readable
    "SurveyTemplateManager.get_template_for_edit": CHECKS,  # ensure_manageable
    "SurveyTemplateManager.update_template": CHECKS,  # ensure_manageable
    "SurveyTemplateManager.delete_template": CHECKS,  # delete_with_shares
    "SurveyTemplateManager.generate_ai_chat_response": CHECKS,  # ai:execute
    "SurveyTemplateManager.generate_ai_questions_draft": CHECKS,  # ai:execute
}
