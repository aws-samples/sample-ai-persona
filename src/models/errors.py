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
    #: The user must sign in (again). Pages move to the login screen, htmx gets
    #: ``HX-Redirect``, the JSON API answers 401 (contract C5 / C13).
    UNAUTHORIZED = "unauthorized"
    #: Signed in but not allowed. Answer 403 without replacing the page body.
    FORBIDDEN = "forbidden"


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

    # --- Login (v1.0.0 U1; the part of contract C5 that U1 uses) ---
    # No valid LoginSession on a URL that requires one (BR1.1, BR1.2).
    AUTH_REQUIRED = ("auth_required", ErrorKind.UNAUTHORIZED)
    # The session hit the 8-hour idle or 7-day absolute limit (BR1.7, BR1.8).
    SESSION_EXPIRED = ("session_expired", ErrorKind.UNAUTHORIZED)
    # A state-changing request without the custom header or own Origin (BR1.19).
    CSRF_FAILED = ("csrf_failed", ErrorKind.FORBIDDEN)
    # The lookup needed to decide (Sessions table, Cognito) is unreachable;
    # fail closed with 503 instead of treating the user as signed in (BR1.18).
    LOOKUP_UNAVAILABLE = ("lookup_unavailable", ErrorKind.TRANSIENT)
    # Login settings are missing or malformed (AUTH_MODE, token key list).
    # Only an operator can fix it; the app refuses to start (BR1.15).
    AUTH_CONFIG_INVALID = ("auth_config_invalid", ErrorKind.CONFIG)

    # --- Closed-network login (v1.0.0 U8, AUTH_MODE=admin_api; contract C5) ---
    # M2 and M3 are full-page forms: VALIDATION and TRANSIENT are both shown
    # inside the form, never as a toast (there is no page header before login).
    # Wrong password, disabled / unknown user, reset required, bad input,
    # unsupported challenge or an unacceptable ID token: one answer (BR1.4).
    LOGIN_FAILED = ("login_failed", ErrorKind.VALIDATION)
    # Cognito's attempt limit or throttling (BR1.5).
    LOGIN_RATE_LIMITED = ("login_rate_limited", ErrorKind.TRANSIENT)
    # Cognito unreachable, timed out, 5xx, breaker open, or an unexpected
    # answer; never treated as success (BR1.5, NFR4.22).
    LOGIN_UNAVAILABLE = ("login_unavailable", ErrorKind.TRANSIENT)
    # The new password misses a rule of the User Pool (BR2.5, BR2.6).
    PASSWORD_POLICY_NOT_MET = ("password_policy_not_met", ErrorKind.VALIDATION)
    # The confirmation field differs from the new password (BR2.5).
    PASSWORD_CONFIRMATION_MISMATCH = (
        "password_confirmation_mismatch",
        ErrorKind.VALIDATION,
    )
    # The first-password-change challenge expired or was used (BR2.4, BR2.7):
    # start again from the login screen.
    LOGIN_CHALLENGE_EXPIRED = ("login_challenge_expired", ErrorKind.UNAUTHORIZED)

    # --- Authorization (v1.0.0 U2; the rest of contract C5) ---
    # Signed in without any role: only the no-role page is usable (BR2.8).
    ROLE_REQUIRED = ("role_required", ErrorKind.FORBIDDEN)
    # The user can read the target but not manage it, or lacks the permission
    # for the operation (BR2.2, BR2.7).
    ACCESS_DENIED = ("access_denied", ErrorKind.FORBIDDEN)
    # The JSON API request did not match its schema (422, contract C13).
    REQUEST_INVALID = ("request_invalid", ErrorKind.VALIDATION)
    # Another send (another task, or a double click) is answering in the same
    # interview session, or a save arrived while a reply is in progress. Waiting
    # for the reply and trying again resolves it (contract C5, ADR-004, U6
    # NFR1.59 / NFR1.60). The input is kept (toast).
    CONCURRENT_UPDATE = ("concurrent_update", ErrorKind.TRANSIENT)
    # FastAPI's own HTTPException (unknown URL 404, 405, ...) carried in the
    # JSON API body. The HTTP status stays as raised (contract C13).
    HTTP_ERROR = ("http_error", ErrorKind.NOT_FOUND)

    # --- Department sharing (v1.0.0 U4, functional-spec.md §5) ---
    # A department outside the user's choices was added or removed, a removed
    # department no longer exists, or the same one is added and removed (BR1.2,
    # BR1.5). Reachable only through a tampered form; nothing is written.
    SHARE_SCOPE_INVALID = ("share_scope_invalid", ErrorKind.VALIDATION)
    # The user belongs to no department, so there is nothing to choose (BR1.4).
    SHARE_NO_DEPARTMENT = ("share_no_department", ErrorKind.VALIDATION)
    # The resource has no owner_sub (made before v1.0.0); it cannot be shared
    # (BR1.10).
    SHARE_OWNER_MISSING = ("share_owner_missing", ErrorKind.VALIDATION)
    # A bulk share without any target (BR2.1).
    SHARE_NO_TARGET = ("share_no_target", ErrorKind.VALIDATION)
    # More than 100 targets in one bulk share (BR2.1).
    SHARE_TOO_MANY_TARGETS = ("share_too_many_targets", ErrorKind.CAPACITY)
    # A resource would be shared with more than 99 departments (BR1.11).
    SHARE_TOO_MANY_GROUPS = ("share_too_many_groups", ErrorKind.CAPACITY)
    # Concurrent changes kept the conditional write from succeeding three
    # times, or DynamoDB failed (BR1.9). Retrying can succeed.
    SHARE_UPDATE_FAILED = ("share_update_failed", ErrorKind.TRANSIENT)

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
    # Generated personas wait in PersonaCandidates (8 hours, U5 BR1.3) until
    # the user who generated them saves them. An expired, someone else's or
    # already-being-saved candidate all get this code (they are not told
    # apart). Retrying "save" cannot recover it and no input fixes it either;
    # the only path is regenerating, same treatment as
    # INTERVIEW_SESSION_AGENTS_MISSING.
    GENERATION_PERSONA_CACHE_EXPIRED = (
        "generation_persona_cache_expired",
        ErrorKind.NOT_FOUND,
    )
    # One candidate would exceed the 350KB item limit (U5 BR1.6). The user
    # generates fewer personas or changes the conditions.
    GENERATION_RESULT_TOO_LARGE = ("generation_result_too_large", ErrorKind.CAPACITY)
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
    # CSV header row cannot be mapped 1:1 to queryable columns (empty/whitespace
    # header names or duplicates). DuckDB read_csv_auto would rename such columns
    # (column1 / name_1), diverging from the allowlist shown to the model and
    # silently breaking analyze_dataset. VALIDATION: the user fixes the CSV header.
    FILE_CSV_HEADER_INVALID = ("file_csv_header_invalid", ErrorKind.VALIDATION)
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
    # An upload that does not exist, is someone else's, has expired or is
    # already attached (U5 BR5.3〜BR5.5, BR5.7). Not told apart (NFR1.14).
    FILE_NOT_FOUND = ("file_not_found", ErrorKind.NOT_FOUND)
    # More than 100 files attached to one parent (the transaction limit,
    # security-design.md §3). The user attaches fewer files.
    FILE_ATTACH_LIMIT_EXCEEDED = ("file_attach_limit_exceeded", ErrorKind.CAPACITY)
    # Closed network U4 (BR2.3): a stored file is above the limit for streaming
    # it through the app. The user cannot fix a stored file, so an operator
    # handles it (CONFIG, not CAPACITY).
    ASSET_TOO_LARGE = ("asset_too_large", ErrorKind.CONFIG)

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
    # Closed network U3: the Parquet must be placed beforehand and is not at
    # NEMOTRON_DATASET_KEY. An operator places it (the cdk/README.md procedure);
    # the settings screen cannot fix it.
    NEMOTRON_DATASET_NOT_PLACED = ("nemotron_dataset_not_placed", ErrorKind.CONFIG)
    # Closed network U3: this environment never downloads from Hugging Face.
    NEMOTRON_DOWNLOAD_NOT_ALLOWED = (
        "nemotron_download_not_allowed",
        ErrorKind.FORBIDDEN,
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
    # U7 (NFR2.25): a template body over 200KB, or a survey body (with its
    # template snapshot) over 350KB. The user shortens the questions.
    SURVEY_TEMPLATE_TOO_LARGE = ("survey_template_too_large", ErrorKind.CAPACITY)
    # U7 (NFR2.25): the insight report would push the survey body over 350KB.
    SURVEY_REPORT_TOO_LARGE = ("survey_report_too_large", ErrorKind.CAPACITY)
    # U7 (BR2.2 (b)): an image of the template snapshot could not be read while
    # the survey ran. The survey stops instead of answering without the image.
    SURVEY_TEMPLATE_IMAGE_UNAVAILABLE = (
        "survey_template_image_unavailable",
        ErrorKind.TRANSIENT,
    )
    # --- Custom persona data (U7, BR4.2〜BR4.4) ---
    # Missing, or outside the user's usage scope (not told apart, NFR1.79).
    CUSTOM_PERSONA_DATA_NOT_FOUND = (
        "custom_persona_data_not_found",
        ErrorKind.NOT_FOUND,
    )
    CUSTOM_PERSONA_DATA_NAME_INVALID = (
        "custom_persona_data_name_invalid",
        ErrorKind.VALIDATION,
    )
    CUSTOM_PERSONA_DATA_NAME_TAKEN = (
        "custom_persona_data_name_taken",
        ErrorKind.VALIDATION,
    )
    # --- Settings (U7, BR3.1) ---
    # A settings screen or API for a user without ``settings:manage``: the same
    # 404 as a missing page, so the screen's existence is not revealed.
    SETTINGS_NOT_FOUND = ("settings_not_found", ErrorKind.NOT_FOUND)

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
    # The uploaded file could not be decoded as an allowed image format.
    PERSONA_AVATAR_INVALID_IMAGE = (
        "persona_avatar_invalid_image",
        ErrorKind.VALIDATION,
    )
    # Decodable but too many pixels: the user can fix it by resizing the image.
    PERSONA_AVATAR_TOO_MANY_PIXELS = (
        "persona_avatar_too_many_pixels",
        ErrorKind.CAPACITY,
    )
    PERSONA_AVATAR_NOT_FOUND = ("persona_avatar_not_found", ErrorKind.NOT_FOUND)
    # Another upload/revert changed the avatar first: retrying resolves it.
    PERSONA_AVATAR_CONFLICT = ("persona_avatar_conflict", ErrorKind.TRANSIENT)
    # Avatar images live in S3 only; the feature is off without a bucket.
    PERSONA_AVATAR_UNAVAILABLE = ("persona_avatar_unavailable", ErrorKind.CONFIG)
    # The user picked a column that is not in the dataset: correctable input.
    DATASET_COLUMN_NOT_FOUND = ("dataset_column_not_found", ErrorKind.VALIDATION)
    DATASET_NOT_FOUND = ("dataset_not_found", ErrorKind.NOT_FOUND)
    DATASET_BINDING_NOT_FOUND = ("dataset_binding_not_found", ErrorKind.NOT_FOUND)
    # The persona has no knowledge-base binding to remove (U5 BR2.5).
    KB_BINDING_NOT_FOUND = ("kb_binding_not_found", ErrorKind.NOT_FOUND)
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
    # Same limit as MEMORY_CONTENT_TOO_LONG, but the fix is different: a
    # converted file is shortened by splitting the file, not by editing text.
    KNOWLEDGE_FILE_CONTENT_TOO_LONG = (
        "knowledge_file_content_too_long",
        ErrorKind.CAPACITY,
    )
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
    # The body of the discussion would exceed 350KB (BR2.7, U6). Fewer
    # insights or attachments make it fit; nothing was written.
    DISCUSSION_TOO_LARGE = ("discussion_too_large", ErrorKind.CAPACITY)
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
    # 選択された model_id が model_registry に未登録（フォーム改竄・古いUI等）。
    DISCUSSION_MODEL_UNSUPPORTED = (
        "discussion_model_unsupported",
        ErrorKind.VALIDATION,
    )
    # 追加ペルソナベースモデル（非Anthropicモデル。Bedrock Mantle経由）が選択されたが、
    # 運用者がENABLE_ADDITIONAL_PERSONA_MODELSを有効化していない。設定画面での対応を要する。
    DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED = (
        "discussion_model_additional_models_disabled",
        ErrorKind.CONFIG,
    )
    # ドキュメント（画像含む）合計サイズが選択モデルの上限（Gemma4=3.5MB等）を超過。
    # モデルを変えるか添付を減らせば解決するため CAPACITY。
    DISCUSSION_MODEL_INPUT_TOO_LARGE = (
        "discussion_model_input_too_large",
        ErrorKind.CAPACITY,
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
    # Closed network U3: DATA_AGENT_ALLOWED=false. No setting can turn it on.
    DATA_AGENT_NOT_ALLOWED = ("data_agent_not_allowed", ErrorKind.FORBIDDEN)
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
    # 選択された model_id が model_registry に未登録（フォーム改竄・古いUI等）。
    INTERVIEW_MODEL_UNSUPPORTED = (
        "interview_model_unsupported",
        ErrorKind.VALIDATION,
    )
    # 追加ペルソナベースモデル（非Anthropicモデル。Bedrock Mantle経由）が選択されたが、
    # 運用者がENABLE_ADDITIONAL_PERSONA_MODELSを有効化していない。設定画面での対応を要する。
    INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED = (
        "interview_model_additional_models_disabled",
        ErrorKind.CONFIG,
    )
    # ドキュメント（画像含む）合計サイズが選択モデルの上限（Gemma4=3.5MB等）を超過。
    # モデルを変えるか添付を減らせば解決するため CAPACITY。
    INTERVIEW_MODEL_INPUT_TOO_LARGE = (
        "interview_model_input_too_large",
        ErrorKind.CAPACITY,
    )

    # --- Service layer (agent/AI/database/storage infrastructure) ---
    # These codes are for CodedError compliance in the Service layer. The
    # Manager layer always converts Service exceptions into its own coded
    # exception before they reach the presentation layer, so these never
    # resolve to user-facing wording; they exist so no CodedError subclass in
    # this codebase is raised without a code.
    AGENT_SDK_UNAVAILABLE = ("agent_sdk_unavailable", ErrorKind.CONFIG)
    # 追加ペルソナベースモデルがENABLE_ADDITIONAL_PERSONA_MODELS無効時に選択された場合。
    # Managerが DISCUSSION_MODEL_ADDITIONAL_MODELS_DISABLED /
    # INTERVIEW_MODEL_ADDITIONAL_MODELS_DISABLED に変換する。
    AGENT_MODEL_ADDITIONAL_MODELS_DISABLED = (
        "agent_model_additional_models_disabled",
        ErrorKind.CONFIG,
    )
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
    DATABASE_OPERATION_FAILED = ("database_operation_failed", ErrorKind.TRANSIENT)
    S3_OPERATION_FAILED = ("s3_operation_failed", ErrorKind.TRANSIENT)
    S3_OBJECT_NOT_FOUND = ("s3_object_not_found", ErrorKind.NOT_FOUND)
    # Sessions table unreachable (timeout, throttling past retries).
    SESSION_STORE_UNAVAILABLE = ("session_store_unavailable", ErrorKind.TRANSIENT)
    # Cognito unreachable, timed out, 5xx, 429, or the circuit breaker is open.
    OIDC_UNAVAILABLE = ("oidc_unavailable", ErrorKind.TRANSIENT)
    # Cognito rejected the grant (refresh token revoked/expired, user disabled):
    # permanent, the session must end (BR1.11).
    OIDC_GRANT_INVALID = ("oidc_grant_invalid", ErrorKind.UNAUTHORIZED)
    # ID token failed signature / iss / aud / exp / nonce verification (BR1.4).
    OIDC_TOKEN_INVALID = ("oidc_token_invalid", ErrorKind.UNAUTHORIZED)


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


class AuthRequiredError(CodedError):
    """The request needs a valid login (contract C5).

    ``SESSION_EXPIRED`` is expressed by overriding ``code`` rather than by a
    separate class, so callers catch one type for both.
    """

    code = ErrorCode.AUTH_REQUIRED


class LookupUnavailableError(CodedError):
    """A lookup needed for the auth decision is unreachable (fail closed, 503)."""

    code = ErrorCode.LOOKUP_UNAVAILABLE


class AccessDeniedError(CodedError):
    """Readable but not manageable, or the permission is missing (403, C5)."""

    code = ErrorCode.ACCESS_DENIED


class RoleRequiredError(CodedError):
    """Signed in without any role (403 on the JSON API, BR2.8)."""

    code = ErrorCode.ROLE_REQUIRED


class ConcurrentUpdateError(CodedError):
    """A reply is in progress in the same interview session (C5, ADR-004)."""

    code = ErrorCode.CONCURRENT_UPDATE


class ParticipantNotFoundError(CodedError):
    """A participant of a discussion or interview can no longer be read (U6).

    The same ``PERSONA_NOT_FOUND`` as an unreadable persona (NFR1.56); the
    type lets the screen show the stop panel (M12, BR7.5) without reading
    the code of the exception.
    """

    code = ErrorCode.PERSONA_NOT_FOUND
