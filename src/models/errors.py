"""
Error codes and the coded-exception base class for the AI Persona System.

Design principle: an exception message belongs to the developer, and the
user-facing wording belongs to the presentation layer.

- Exception message: a technical fact for logs. Never rendered to a response.
- ``ErrorCode``: a machine-readable error kind, resolved to user-facing wording
  by ``web/error_messages.py``.
- ``ErrorCode.kind``: what the user can do about it, which determines *how* the
  error should be presented (see :class:`ErrorKind`).
- ``CodedError.context``: structured values used both for diagnostic logging and
  for interpolating the wording template.

See ``docs/note/exception-message-design.md`` for the full rationale.
"""

from enum import StrEnum


class ErrorKind(StrEnum):
    """What the user can do about an error, which decides how to present it.

    The wording catalog answers "what do we say"; this answers "how do we show
    it". Presentation code should branch on the kind instead of on the HTTP
    status or on individual codes (Issue #117).
    """

    #: The user can fix it by correcting their input. Show it inline next to the
    #: field and keep what they typed.
    VALIDATION = "validation"
    #: The user can fix it by reducing the amount of input. Show it near the
    #: form, with the limit spelled out.
    CAPACITY = "capacity"
    #: The target does not exist or is not ready yet. Replace the affected
    #: region and offer a way back.
    NOT_FOUND = "not_found"
    #: An operator must change configuration. Point at the settings screen.
    CONFIG = "config"
    #: May succeed on retry. Show it without destroying the user's input.
    TRANSIENT = "transient"


class ErrorCode(StrEnum):
    """Machine-readable error kinds shared across all layers.

    Granularity follows what the user can do about the error: an error that
    calls for a distinct user action gets its own code, while internal
    operation failures collapse into a per-feature catch-all code.

    Each member carries an :class:`ErrorKind` so that presentation code can
    decide how to display it without hard-coding per-code branches. The kind is
    part of the member definition (rather than a separate mapping) so that a new
    code cannot be added without classifying it.

    Every member except ``UNKNOWN`` must have an entry in the wording catalog
    (``web/error_messages.py``); ``tests/unit/test_error_messages.py`` enforces
    this.
    """

    _kind: ErrorKind

    def __new__(cls, value: str, kind: ErrorKind) -> "ErrorCode":
        member = str.__new__(cls, value)
        member._value_ = value
        member._kind = kind
        return member

    @property
    def kind(self) -> ErrorKind:
        """How this error should be presented."""
        return self._kind

    @classmethod
    def parse(cls, value: str | None) -> "ErrorCode | None":
        """Look up a member by its stored value, or ``None`` if unknown.

        Needed for codes that are persisted and read back later (see
        ``Survey.error_code``). ``ErrorCode(value)`` cannot be used for that:
        ``__new__`` takes the kind as a second argument, so a one-argument call
        does not type-check even though it works at runtime.
        """
        if not value:
            return None
        return cls._value2member_map_.get(value)  # type: ignore[return-value]

    # Fallback for exceptions that carry no code yet. Treated as TRANSIENT
    # because an unclassified failure must not silently look like a validation
    # error the user can fix.
    UNKNOWN = ("unknown", ErrorKind.TRANSIENT)

    # --- Generic ---
    NETWORK_ERROR = ("network_error", ErrorKind.TRANSIENT)
    JOB_NOT_FOUND = ("job_not_found", ErrorKind.NOT_FOUND)

    # --- Generation capacity ---
    GENERATION_CAPACITY_EXCEEDED = (
        "generation_capacity_exceeded",
        ErrorKind.CAPACITY,
    )
    REPORT_CAPACITY_EXCEEDED = ("report_capacity_exceeded", ErrorKind.CAPACITY)

    # --- Persona generation ---
    GENERATION_PERSONA_COUNT_INVALID = (
        "generation_persona_count_invalid",
        ErrorKind.VALIDATION,
    )
    GENERATION_DATA_DESCRIPTION_REQUIRED = (
        "generation_data_description_required",
        ErrorKind.VALIDATION,
    )
    GENERATION_FILES_REQUIRED = ("generation_files_required", ErrorKind.VALIDATION)
    # Generated personas live in a TTLCache pending user confirmation to save.
    # Once the entry ages out, retrying "save" cannot recover it and no input
    # fixes it either; the only path is regenerating, same treatment as
    # INTERVIEW_SESSION_AGENTS_MISSING.
    GENERATION_PERSONA_CACHE_EXPIRED = (
        "generation_persona_cache_expired",
        ErrorKind.NOT_FOUND,
    )
    # Catch-all for generation failures that are not capacity-related (agent
    # setup, unexpected SDK errors).
    GENERATION_OPERATION_FAILED = (
        "generation_operation_failed",
        ErrorKind.TRANSIENT,
    )

    # --- Files ---
    FILE_TOO_LARGE = ("file_too_large", ErrorKind.CAPACITY)
    FILE_FORMAT_NOT_ALLOWED = ("file_format_not_allowed", ErrorKind.VALIDATION)
    FILE_MIME_UNSUPPORTED = ("file_mime_unsupported", ErrorKind.VALIDATION)
    FILE_EMPTY = ("file_empty", ErrorKind.VALIDATION)
    # Text-bearing uploads: too little content to work with, or undecodable.
    # The guidance wording belongs in the catalog.
    INTERVIEW_FILE_CONTENT_TOO_SHORT = (
        "interview_file_content_too_short",
        ErrorKind.VALIDATION,
    )
    # Security checks on the filename and the payload. VALIDATION because the
    # user resolves them by choosing or renaming the file.
    FILE_NAME_INVALID = ("file_name_invalid", ErrorKind.VALIDATION)
    FILE_NAME_TOO_LONG = ("file_name_too_long", ErrorKind.VALIDATION)
    FILE_HIDDEN_NOT_ALLOWED = ("file_hidden_not_allowed", ErrorKind.VALIDATION)
    FILE_BINARY_NOT_ALLOWED = ("file_binary_not_allowed", ErrorKind.VALIDATION)
    # Catch-all for file operations that failed for internal reasons.
    FILE_OPERATION_FAILED = ("file_operation_failed", ErrorKind.TRANSIENT)

    # --- Surveys ---
    SURVEY_NOT_FOUND = ("survey_not_found", ErrorKind.NOT_FOUND)
    SURVEY_RESULT_NOT_READY = ("survey_result_not_ready", ErrorKind.NOT_FOUND)
    SURVEY_TEMPLATE_NOT_FOUND = ("survey_template_not_found", ErrorKind.NOT_FOUND)
    SURVEY_TEMPLATE_NAME_BLANK = (
        "survey_template_name_blank",
        ErrorKind.VALIDATION,
    )
    SURVEY_TEMPLATE_NO_QUESTIONS = (
        "survey_template_no_questions",
        ErrorKind.VALIDATION,
    )
    SURVEY_TEMPLATE_QUESTIONS_INVALID = (
        "survey_template_questions_invalid",
        ErrorKind.VALIDATION,
    )
    SURVEY_TEMPLATE_TOO_FEW_OPTIONS = (
        "survey_template_too_few_options",
        ErrorKind.VALIDATION,
    )
    SURVEY_TEMPLATE_TOO_MANY_IMAGES = (
        "survey_template_too_many_images",
        ErrorKind.VALIDATION,
    )
    SURVEY_TEMPLATE_IMAGE_NAME_MISSING = (
        "survey_template_image_name_missing",
        ErrorKind.VALIDATION,
    )
    SURVEY_PERSONA_COUNT_INVALID = (
        "survey_persona_count_invalid",
        ErrorKind.VALIDATION,
    )
    SURVEY_TARGET_COUNT_TOO_LOW = (
        "survey_target_count_too_low",
        ErrorKind.VALIDATION,
    )
    SURVEY_TARGET_COUNT_TOO_HIGH = (
        "survey_target_count_too_high",
        ErrorKind.VALIDATION,
    )
    SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES = (
        "survey_target_count_too_high_with_images",
        ErrorKind.VALIDATION,
    )
    # The requested count passes, but the filters (or the dataset itself) yield
    # fewer personas than Bedrock batch inference accepts. The user can fix it by
    # loosening the filters, hence VALIDATION rather than CAPACITY.
    SURVEY_AVAILABLE_PERSONAS_TOO_FEW = (
        "survey_available_personas_too_few",
        ErrorKind.VALIDATION,
    )
    # The uploaded dataset itself is below the batch-inference minimum, so no
    # filter change can rescue it; a different file is needed.
    SURVEY_DATASET_TOO_FEW_ROWS = (
        "survey_dataset_too_few_rows",
        ErrorKind.VALIDATION,
    )
    SURVEY_DATASET_CSV_UNREADABLE = (
        "survey_dataset_csv_unreadable",
        ErrorKind.VALIDATION,
    )
    SURVEY_DATASET_CSV_EMPTY = ("survey_dataset_csv_empty", ErrorKind.VALIDATION)
    SURVEY_DATASET_NO_TEXT_COLUMN = (
        "survey_dataset_no_text_column",
        ErrorKind.VALIDATION,
    )
    # The operator must download the dataset first (settings screen).
    SURVEY_DATASET_NOT_DOWNLOADED = (
        "survey_dataset_not_downloaded",
        ErrorKind.CONFIG,
    )
    # The batch-inference IAM role is not configured. Only an operator can fix
    # this, and the variable name itself stays out of the wording.
    SURVEY_BATCH_ROLE_NOT_CONFIGURED = (
        "survey_batch_role_not_configured",
        ErrorKind.CONFIG,
    )
    # Bedrock reported the job as Failed/Stopped. Its own message goes to the
    # logs only: it carries bucket names and role ARNs.
    SURVEY_BATCH_JOB_FAILED = ("survey_batch_job_failed", ErrorKind.TRANSIENT)
    SURVEY_BATCH_JOB_TIMED_OUT = ("survey_batch_job_timed_out", ErrorKind.TRANSIENT)
    # AI-assisted template authoring.
    SURVEY_AI_UNAVAILABLE = ("survey_ai_unavailable", ErrorKind.CONFIG)
    # The AI produced nothing usable; retrying can succeed.
    SURVEY_AI_NO_QUESTIONS = ("survey_ai_no_questions", ErrorKind.TRANSIENT)
    SURVEY_AI_CONVERSATION_INVALID = (
        "survey_ai_conversation_invalid",
        ErrorKind.VALIDATION,
    )
    SURVEY_AI_CONVERSATION_TOO_LONG = (
        "survey_ai_conversation_too_long",
        ErrorKind.CAPACITY,
    )
    SURVEY_AI_MESSAGE_TOO_LONG = ("survey_ai_message_too_long", ErrorKind.CAPACITY)
    # Catch-alls for survey operations that failed for internal reasons.
    SURVEY_OPERATION_FAILED = ("survey_operation_failed", ErrorKind.TRANSIENT)
    SURVEY_EXECUTION_FAILED = ("survey_execution_failed", ErrorKind.TRANSIENT)
    SURVEY_REPORT_GENERATION_FAILED = (
        "survey_report_generation_failed",
        ErrorKind.TRANSIENT,
    )

    # --- Personas ---
    # Field-level validation is expressed as a validation *kind* plus a stable
    # field key in ``context["field"]``; the catalog maps that key to a label.
    # This keeps every Japanese string in the presentation layer instead of
    # spreading one code per field across the enum.
    PERSONA_FIELD_REQUIRED = ("persona_field_required", ErrorKind.VALIDATION)
    PERSONA_FIELD_TOO_LONG = ("persona_field_too_long", ErrorKind.VALIDATION)
    PERSONA_LIST_EMPTY = ("persona_list_empty", ErrorKind.VALIDATION)
    PERSONA_SELECTION_REQUIRED = (
        "persona_selection_required",
        ErrorKind.VALIDATION,
    )
    PERSONA_LIST_HAS_EMPTY_ITEM = (
        "persona_list_has_empty_item",
        ErrorKind.VALIDATION,
    )
    PERSONA_LIST_TOO_MANY_ITEMS = (
        "persona_list_too_many_items",
        ErrorKind.VALIDATION,
    )
    PERSONA_LIST_ITEM_TOO_LONG = ("persona_list_item_too_long", ErrorKind.VALIDATION)
    PERSONA_AGE_OUT_OF_RANGE = ("persona_age_out_of_range", ErrorKind.VALIDATION)
    PERSONA_GENDER_INVALID = ("persona_gender_invalid", ErrorKind.VALIDATION)
    PERSONA_COUNTRY_INVALID = ("persona_country_invalid", ErrorKind.VALIDATION)
    PERSONA_TAG_COMMA_NOT_ALLOWED = (
        "persona_tag_comma_not_allowed",
        ErrorKind.VALIDATION,
    )
    PERSONA_INVALID = ("persona_invalid", ErrorKind.VALIDATION)
    PERSONA_ID_INVALID = ("persona_id_invalid", ErrorKind.VALIDATION)
    PERSONA_NOT_FOUND = ("persona_not_found", ErrorKind.NOT_FOUND)
    PERSONA_UPDATE_FAILED = ("persona_update_failed", ErrorKind.TRANSIENT)
    PERSONA_OPERATION_FAILED = ("persona_operation_failed", ErrorKind.TRANSIENT)
    # The user picked a column that is not in the dataset: correctable input.
    DATASET_COLUMN_NOT_FOUND = ("dataset_column_not_found", ErrorKind.VALIDATION)
    DATASET_NOT_FOUND = ("dataset_not_found", ErrorKind.NOT_FOUND)
    DATASET_BINDING_NOT_FOUND = ("dataset_binding_not_found", ErrorKind.NOT_FOUND)
    # Catch-all for dataset operations that failed for internal reasons
    # (including a binding referencing a column that no longer passes the
    # server-side safety check -- not something the user can fix by re-entering
    # input on this screen).
    DATASET_OPERATION_FAILED = ("dataset_operation_failed", ErrorKind.TRANSIENT)

    # --- Persona long-term memory ---
    MEMORY_TOPIC_NAME_REQUIRED = ("memory_topic_name_required", ErrorKind.VALIDATION)
    MEMORY_CONTENT_REQUIRED = ("memory_content_required", ErrorKind.VALIDATION)
    MEMORY_TOPIC_NAME_TOO_LONG = ("memory_topic_name_too_long", ErrorKind.CAPACITY)
    MEMORY_CONTENT_TOO_LONG = ("memory_content_too_long", ErrorKind.CAPACITY)
    MEMORY_FEATURE_DISABLED = ("memory_feature_disabled", ErrorKind.CONFIG)
    MEMORY_STRATEGY_NOT_CONFIGURED = (
        "memory_strategy_not_configured",
        ErrorKind.CONFIG,
    )
    # The delete call reached the service and got ResourceNotFoundException:
    # the record is already gone (double-click, stale tab). Retrying or
    # editing input cannot change that; the item itself must be removed from
    # the screen, same treatment as INTERVIEW_SESSION_ALREADY_SAVED.
    MEMORY_ALREADY_DELETED = ("memory_already_deleted", ErrorKind.NOT_FOUND)
    MEMORY_SERVICE_UNAVAILABLE = ("memory_service_unavailable", ErrorKind.TRANSIENT)
    MEMORY_OPERATION_FAILED = ("memory_operation_failed", ErrorKind.TRANSIENT)

    # --- Discussions ---
    DISCUSSION_INTERVIEW_MODE_UNSUPPORTED = (
        "discussion_interview_mode_unsupported",
        ErrorKind.VALIDATION,
    )
    DISCUSSION_PERSONAS_REQUIRED = (
        "discussion_personas_required",
        ErrorKind.VALIDATION,
    )
    DISCUSSION_TOO_FEW_PERSONAS = (
        "discussion_too_few_personas",
        ErrorKind.VALIDATION,
    )
    DISCUSSION_TOO_MANY_PERSONAS = (
        "discussion_too_many_personas",
        ErrorKind.VALIDATION,
    )
    DISCUSSION_PERSONA_INVALID = ("discussion_persona_invalid", ErrorKind.VALIDATION)
    DISCUSSION_PERSONA_DUPLICATED = (
        "discussion_persona_duplicated",
        ErrorKind.VALIDATION,
    )
    DISCUSSION_TOPIC_REQUIRED = ("discussion_topic_required", ErrorKind.VALIDATION)
    DISCUSSION_TOPIC_TOO_SHORT = ("discussion_topic_too_short", ErrorKind.VALIDATION)
    DISCUSSION_TOPIC_TOO_LONG = ("discussion_topic_too_long", ErrorKind.VALIDATION)
    DISCUSSION_DOCUMENTS_TOO_LARGE = (
        "discussion_documents_too_large",
        ErrorKind.CAPACITY,
    )
    DISCUSSION_NOT_FOUND = ("discussion_not_found", ErrorKind.NOT_FOUND)
    DISCUSSION_ROUNDS_TOO_FEW = ("discussion_rounds_too_few", ErrorKind.VALIDATION)
    DISCUSSION_ROUNDS_TOO_MANY = ("discussion_rounds_too_many", ErrorKind.VALIDATION)
    DISCUSSION_AGENT_SETUP_FAILED = (
        "discussion_agent_setup_failed",
        ErrorKind.TRANSIENT,
    )
    DISCUSSION_OPERATION_FAILED = (
        "discussion_operation_failed",
        ErrorKind.TRANSIENT,
    )
    # The caller passed a blank/missing discussion object or id -- this is a
    # programming error at the call site, not a user-correctable form input,
    # but VALIDATION is still the closest kind (no retry will fix it).
    DISCUSSION_INVALID = ("discussion_invalid", ErrorKind.VALIDATION)
    DISCUSSION_ID_INVALID = ("discussion_id_invalid", ErrorKind.VALIDATION)
    DISCUSSION_DOCUMENT_NOT_FOUND = (
        "discussion_document_not_found",
        ErrorKind.NOT_FOUND,
    )
    # The generated discussion/insights failed a quality check (too few
    # messages, insight missing fields, etc). Retrying the same generation
    # call may produce a passing result.
    DISCUSSION_RESULT_INVALID = ("discussion_result_invalid", ErrorKind.TRANSIENT)
    DISCUSSION_INSIGHT_GENERATION_FAILED = (
        "discussion_insight_generation_failed",
        ErrorKind.TRANSIENT,
    )
    DISCUSSION_MEMORY_MODE_INVALID = (
        "discussion_memory_mode_invalid",
        ErrorKind.VALIDATION,
    )

    # --- Discussion reports ---
    REPORT_NOT_FOUND = ("report_not_found", ErrorKind.NOT_FOUND)
    # The user resolves it by deleting an existing report, not by editing input.
    REPORT_LIMIT_REACHED = ("report_limit_reached", ErrorKind.CAPACITY)
    REPORT_OPERATION_FAILED = ("report_operation_failed", ErrorKind.TRANSIENT)

    # --- Survey persona datasets (DWH segment extraction) ---
    SEGMENT_CONDITION_REQUIRED = ("segment_condition_required", ErrorKind.VALIDATION)
    SEGMENT_ROW_COUNT_TOO_LOW = ("segment_row_count_too_low", ErrorKind.VALIDATION)
    SEGMENT_ROW_COUNT_TOO_HIGH = ("segment_row_count_too_high", ErrorKind.VALIDATION)
    # The data agent returned no CSV URL: an internal outcome, retry may work.
    SEGMENT_CSV_URL_MISSING = ("segment_csv_url_missing", ErrorKind.TRANSIENT)
    DATA_AGENT_NOT_CONFIGURED = ("data_agent_not_configured", ErrorKind.CONFIG)
    # The runtime ARN is set, but the service failed to initialize or the
    # connection test itself failed. Retry can succeed once the transient
    # condition clears.
    DATA_AGENT_CONNECTION_FAILED = (
        "data_agent_connection_failed",
        ErrorKind.TRANSIENT,
    )
    # The URL a tool tries to download from failed the scheme/domain allowlist
    # check. This is an internal safety guard, not a user input to correct.
    DATA_AGENT_DOWNLOAD_URL_REJECTED = (
        "data_agent_download_url_rejected",
        ErrorKind.TRANSIENT,
    )

    # --- Interviews ---
    INTERVIEW_PERSONAS_REQUIRED = (
        "interview_personas_required",
        ErrorKind.VALIDATION,
    )
    INTERVIEW_TOO_MANY_PERSONAS = (
        "interview_too_many_personas",
        ErrorKind.VALIDATION,
    )
    INTERVIEW_PERSONA_INVALID = ("interview_persona_invalid", ErrorKind.VALIDATION)
    INTERVIEW_USER_ID_INVALID = ("interview_user_id_invalid", ErrorKind.VALIDATION)
    INTERVIEW_MEMORY_MODE_INVALID = (
        "interview_memory_mode_invalid",
        ErrorKind.VALIDATION,
    )
    INTERVIEW_MESSAGE_REQUIRED = ("interview_message_required", ErrorKind.VALIDATION)
    INTERVIEW_MESSAGE_TOO_LONG = ("interview_message_too_long", ErrorKind.CAPACITY)
    INTERVIEW_SESSION_NAME_REQUIRED = (
        "interview_session_name_required",
        ErrorKind.VALIDATION,
    )
    INTERVIEW_SESSION_NAME_TOO_LONG = (
        "interview_session_name_too_long",
        ErrorKind.CAPACITY,
    )
    INTERVIEW_SESSION_NOT_FOUND = (
        "interview_session_not_found",
        ErrorKind.NOT_FOUND,
    )
    # The user cannot fix this by editing input; they can only start a new
    # session, so it gets the same "replace region + link back" treatment as
    # NOT_FOUND rather than VALIDATION.
    INTERVIEW_SESSION_ALREADY_SAVED = (
        "interview_session_already_saved",
        ErrorKind.NOT_FOUND,
    )
    # Agents are cleaned up after save; a stale tab still pointing at the old
    # session hits this. Reloading (which re-fetches session state) resolves it.
    INTERVIEW_SESSION_AGENTS_MISSING = (
        "interview_session_agents_missing",
        ErrorKind.NOT_FOUND,
    )
    # Reachable by clicking "save" before exchanging any messages.
    INTERVIEW_SAVE_PRECONDITION_NOT_MET = (
        "interview_save_precondition_not_met",
        ErrorKind.VALIDATION,
    )
    # Router-side pre-check on save, finer-grained than the manager's own
    # INTERVIEW_SAVE_PRECONDITION_NOT_MET: which side is missing decides what
    # the user should do next (ask a question vs. wait for/retry a response).
    INTERVIEW_NO_MESSAGES = ("interview_no_messages", ErrorKind.VALIDATION)
    INTERVIEW_NO_USER_MESSAGES = (
        "interview_no_user_messages",
        ErrorKind.VALIDATION,
    )
    INTERVIEW_NO_PERSONA_RESPONSES = (
        "interview_no_persona_responses",
        ErrorKind.VALIDATION,
    )
    # Catch-alls for interview operations that failed for internal reasons.
    INTERVIEW_SESSION_OPERATION_FAILED = (
        "interview_session_operation_failed",
        ErrorKind.TRANSIENT,
    )
    INTERVIEW_AGENT_SETUP_FAILED = (
        "interview_agent_setup_failed",
        ErrorKind.TRANSIENT,
    )
    INTERVIEW_AGENT_UNAVAILABLE = (
        "interview_agent_unavailable",
        ErrorKind.TRANSIENT,
    )
    INTERVIEW_SAVE_FAILED = ("interview_save_failed", ErrorKind.TRANSIENT)

    # --- Service layer (agent/AI/database/storage infrastructure) ---
    # These codes are for CodedError compliance in the Service layer. The
    # Manager layer always converts Service exceptions into its own coded
    # exception before they reach the presentation layer, so these never
    # resolve to user-facing wording; they exist so no CodedError subclass in
    # this codebase is raised without a code.
    AGENT_SDK_UNAVAILABLE = ("agent_sdk_unavailable", ErrorKind.CONFIG)
    AGENT_INITIALIZATION_FAILED = (
        "agent_initialization_failed",
        ErrorKind.TRANSIENT,
    )
    AGENT_COMMUNICATION_FAILED = ("agent_communication_failed", ErrorKind.TRANSIENT)
    AI_BEDROCK_UNAVAILABLE = ("ai_bedrock_unavailable", ErrorKind.CONFIG)
    AI_BEDROCK_CONNECTION_FAILED = (
        "ai_bedrock_connection_failed",
        ErrorKind.TRANSIENT,
    )
    AI_BEDROCK_API_FAILED = ("ai_bedrock_api_failed", ErrorKind.TRANSIENT)
    AI_OPERATION_FAILED = ("ai_operation_failed", ErrorKind.TRANSIENT)
    DATABASE_CREDENTIALS_INVALID = (
        "database_credentials_invalid",
        ErrorKind.CONFIG,
    )
    DATABASE_TABLES_NOT_FOUND = ("database_tables_not_found", ErrorKind.CONFIG)
    DATABASE_OPERATION_FAILED = ("database_operation_failed", ErrorKind.TRANSIENT)
    S3_OPERATION_FAILED = ("s3_operation_failed", ErrorKind.TRANSIENT)
    S3_OBJECT_NOT_FOUND = ("s3_object_not_found", ErrorKind.NOT_FOUND)


class CodedError(Exception):
    """Base class for exceptions carrying an :class:`ErrorCode`.

    Args:
        message: A technical fact for logs. May be in English. This is never
            rendered into a response.
        code: Overrides the class-level default. Use this when a single
            exception type covers several distinct user-facing situations,
            instead of declaring one subclass per wording.
        context: Structured values for logging and wording interpolation. Only
            put values here that are safe to show a user (size limits, allowed
            formats, counts) -- never IDs, paths, or third-party exception text.
    """

    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.context: dict[str, object] = dict(context or {})
