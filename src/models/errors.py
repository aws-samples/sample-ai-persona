"""
Error codes and the coded-exception base class for the AI Persona System.

Design principle: an exception message belongs to the developer, and the
user-facing wording belongs to the presentation layer.

- Exception message: a technical fact for logs. Never rendered to a response.
- ``ErrorCode``: a machine-readable error kind, resolved to user-facing wording
  by ``web/error_messages.py``.
- ``CodedError.context``: structured values used both for diagnostic logging and
  for interpolating the wording template.

See ``docs/note/exception-message-design.md`` for the full rationale.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error kinds shared across all layers.

    Granularity follows what the user can do about the error: an error that
    calls for a distinct user action gets its own code, while internal
    operation failures collapse into a per-feature catch-all code.

    Every member except ``UNKNOWN`` must have an entry in the wording catalog
    (``web/error_messages.py``); ``tests/unit/test_error_messages.py`` enforces
    this.
    """

    # Fallback for exceptions that carry no code yet.
    UNKNOWN = "unknown"

    # --- Generic ---
    NETWORK_ERROR = "network_error"

    # --- Generation capacity ---
    GENERATION_CAPACITY_EXCEEDED = "generation_capacity_exceeded"
    REPORT_CAPACITY_EXCEEDED = "report_capacity_exceeded"

    # --- Files ---
    FILE_TOO_LARGE = "file_too_large"
    FILE_FORMAT_NOT_ALLOWED = "file_format_not_allowed"
    FILE_MIME_UNSUPPORTED = "file_mime_unsupported"
    FILE_EMPTY = "file_empty"
    FILE_NOT_FOUND = "file_not_found"
    # Text-bearing uploads: too little content to work with, or undecodable.
    # The two "too short" codes are separate because the guidance differs by
    # upload purpose, and the guidance wording belongs in the catalog.
    INTERVIEW_FILE_CONTENT_TOO_SHORT = "interview_file_content_too_short"
    MARKET_REPORT_CONTENT_TOO_SHORT = "market_report_content_too_short"
    FILE_ENCODING_UNSUPPORTED = "file_encoding_unsupported"
    CSV_ENCODING_UNSUPPORTED = "csv_encoding_unsupported"
    # Security checks on the filename and the payload.
    FILE_NAME_INVALID = "file_name_invalid"
    FILE_NAME_TOO_LONG = "file_name_too_long"
    FILE_HIDDEN_NOT_ALLOWED = "file_hidden_not_allowed"
    FILE_BINARY_NOT_ALLOWED = "file_binary_not_allowed"
    FILE_DELETE_NOT_ALLOWED = "file_delete_not_allowed"
    # Catch-all for file operations that failed for internal reasons.
    FILE_OPERATION_FAILED = "file_operation_failed"

    # --- Surveys ---
    SURVEY_NOT_FOUND = "survey_not_found"
    SURVEY_RESULT_NOT_READY = "survey_result_not_ready"
    SURVEY_TEMPLATE_NOT_FOUND = "survey_template_not_found"
    SURVEY_TEMPLATE_NAME_BLANK = "survey_template_name_blank"
    SURVEY_TEMPLATE_NO_QUESTIONS = "survey_template_no_questions"
    SURVEY_TEMPLATE_TOO_FEW_OPTIONS = "survey_template_too_few_options"
    SURVEY_TEMPLATE_TOO_MANY_IMAGES = "survey_template_too_many_images"
    SURVEY_TEMPLATE_IMAGE_NAME_MISSING = "survey_template_image_name_missing"
    SURVEY_TARGET_COUNT_TOO_LOW = "survey_target_count_too_low"
    SURVEY_TARGET_COUNT_TOO_HIGH = "survey_target_count_too_high"
    SURVEY_TARGET_COUNT_TOO_HIGH_WITH_IMAGES = (
        "survey_target_count_too_high_with_images"
    )
    SURVEY_DATASET_NOT_DOWNLOADED = "survey_dataset_not_downloaded"
    # AI-assisted template authoring.
    SURVEY_AI_UNAVAILABLE = "survey_ai_unavailable"
    SURVEY_AI_NO_QUESTIONS = "survey_ai_no_questions"
    SURVEY_AI_CONVERSATION_INVALID = "survey_ai_conversation_invalid"
    SURVEY_AI_CONVERSATION_TOO_LONG = "survey_ai_conversation_too_long"
    SURVEY_AI_MESSAGE_TOO_LONG = "survey_ai_message_too_long"
    # Catch-alls for survey operations that failed for internal reasons.
    SURVEY_OPERATION_FAILED = "survey_operation_failed"
    SURVEY_EXECUTION_FAILED = "survey_execution_failed"
    SURVEY_REPORT_GENERATION_FAILED = "survey_report_generation_failed"

    # --- Personas ---
    # Field-level validation is expressed as a validation *kind* plus a stable
    # field key in ``context["field"]``; the catalog maps that key to a label.
    # This keeps every Japanese string in the presentation layer instead of
    # spreading one code per field across the enum.
    PERSONA_FIELD_REQUIRED = "persona_field_required"
    PERSONA_FIELD_TOO_LONG = "persona_field_too_long"
    PERSONA_LIST_EMPTY = "persona_list_empty"
    PERSONA_LIST_HAS_EMPTY_ITEM = "persona_list_has_empty_item"
    PERSONA_LIST_TOO_MANY_ITEMS = "persona_list_too_many_items"
    PERSONA_LIST_ITEM_TOO_LONG = "persona_list_item_too_long"
    PERSONA_AGE_OUT_OF_RANGE = "persona_age_out_of_range"
    PERSONA_GENDER_INVALID = "persona_gender_invalid"
    PERSONA_COUNTRY_INVALID = "persona_country_invalid"
    PERSONA_TAG_COMMA_NOT_ALLOWED = "persona_tag_comma_not_allowed"
    PERSONA_INVALID = "persona_invalid"
    PERSONA_ID_INVALID = "persona_id_invalid"
    PERSONA_NOT_FOUND = "persona_not_found"
    PERSONA_UPDATE_FAILED = "persona_update_failed"
    PERSONA_OPERATION_FAILED = "persona_operation_failed"
    DATASET_COLUMN_NOT_FOUND = "dataset_column_not_found"

    # --- Persona long-term memory ---
    MEMORY_TOPIC_NAME_REQUIRED = "memory_topic_name_required"
    MEMORY_CONTENT_REQUIRED = "memory_content_required"
    MEMORY_TOPIC_NAME_TOO_LONG = "memory_topic_name_too_long"
    MEMORY_CONTENT_TOO_LONG = "memory_content_too_long"
    MEMORY_FEATURE_DISABLED = "memory_feature_disabled"
    MEMORY_STRATEGY_NOT_CONFIGURED = "memory_strategy_not_configured"
    MEMORY_SERVICE_UNAVAILABLE = "memory_service_unavailable"
    MEMORY_OPERATION_FAILED = "memory_operation_failed"

    # --- Discussions ---
    DISCUSSION_PERSONAS_REQUIRED = "discussion_personas_required"
    DISCUSSION_TOO_FEW_PERSONAS = "discussion_too_few_personas"
    DISCUSSION_TOO_MANY_PERSONAS = "discussion_too_many_personas"
    DISCUSSION_PERSONA_INVALID = "discussion_persona_invalid"
    DISCUSSION_PERSONA_DUPLICATED = "discussion_persona_duplicated"
    DISCUSSION_TOPIC_REQUIRED = "discussion_topic_required"
    DISCUSSION_TOPIC_TOO_SHORT = "discussion_topic_too_short"
    DISCUSSION_TOPIC_TOO_LONG = "discussion_topic_too_long"
    DISCUSSION_DOCUMENTS_TOO_LARGE = "discussion_documents_too_large"
    DISCUSSION_NOT_FOUND = "discussion_not_found"
    DISCUSSION_ROUNDS_TOO_FEW = "discussion_rounds_too_few"
    DISCUSSION_ROUNDS_TOO_MANY = "discussion_rounds_too_many"
    DISCUSSION_AGENT_SETUP_FAILED = "discussion_agent_setup_failed"
    DISCUSSION_OPERATION_FAILED = "discussion_operation_failed"

    # --- Discussion reports ---
    REPORT_NOT_FOUND = "report_not_found"
    REPORT_LIMIT_REACHED = "report_limit_reached"
    REPORT_OPERATION_FAILED = "report_operation_failed"

    # --- Survey persona datasets (DWH segment extraction) ---
    SEGMENT_CONDITION_REQUIRED = "segment_condition_required"
    SEGMENT_ROW_COUNT_TOO_LOW = "segment_row_count_too_low"
    SEGMENT_ROW_COUNT_TOO_HIGH = "segment_row_count_too_high"
    SEGMENT_CSV_URL_MISSING = "segment_csv_url_missing"
    DATA_AGENT_NOT_CONFIGURED = "data_agent_not_configured"


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
