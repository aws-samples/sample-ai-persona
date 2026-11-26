"""Configuration class: manages system-wide settings."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

from src.models.nemotron_dataset import (
    DEFAULT_NEMOTRON_DATASET_KEY,
    NemotronSource,
    nemotron_dataset_key_problem,
)
from src.models.asset_delivery import AssetDeliveryMode
from src.models.model_registry import SCOPE_DEFAULTS, InferenceScope
from src.models.password_policy import PASSWORD_POLICY_SETTINGS

if TYPE_CHECKING:
    from src.models.insight_category import InsightCategory

#: Table kind (DynamoDB physical-name suffix) -> name of the setting that carries the physical name.
#: If the setting is present it is used; otherwise ``{DYNAMODB_TABLE_PREFIX}_{suffix}``.
TABLE_NAME_SETTINGS: dict[str, str] = {
    "Personas": "PERSONAS_TABLE_NAME",
    "Discussions": "DISCUSSIONS_TABLE_NAME",
    "UploadedFiles": "UPLOADED_FILES_TABLE_NAME",
    "Datasets": "DATASETS_TABLE_NAME",
    "SurveyTemplates": "SURVEY_TEMPLATES_TABLE_NAME",
    "Surveys": "SURVEYS_TABLE_NAME",
    "KnowledgeBases": "KNOWLEDGE_BASES_TABLE_NAME",
    "Jobs": "JOBS_TABLE_NAME",
}
# The old two binding tables (datasets, KBs) were merged into PersonaBindings,
# so they are no longer read (the tables are kept in CDK with RETAIN).


@dataclass
class Config:
    """System configuration class"""

    # DynamoDB settings
    DYNAMODB_TABLE_PREFIX: str = "AIPersona"
    DYNAMODB_REGION: str = "us-east-1"

    # File storage settings
    UPLOAD_DIR: str = "uploads/"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB
    MAX_IMAGE_SIZE: int = (
        5 * 1024 * 1024
    )  # 5MB per uploaded image (larger ones are shrunk before sending to fit the model's 3.75MB / 2000px long-side limits; Issue #217)
    MAX_REQUEST_PAYLOAD_SIZE: int = (
        32 * 1024 * 1024
    )  # 32MB: overall Claude (Bedrock) request payload limit, applied to the total of discussion documents
    # Persona generation sources: coarse guard on raw bytes per file.
    # Raw bytes never reach Bedrock (the extracted text is concatenated into the prompt),
    # so this limit only guards against markitdown running out of memory or taking
    # too long on a huge binary. The real limit is PERSONA_SOURCE_MAX_CHARS
    # (characters after extraction).
    PERSONA_SOURCE_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB/file
    # Persona generation sources: total characters of extracted text (the real limit).
    # A safe value derived from the default 200K-token context window of Sonnet 5 on Bedrock.
    # For real Japanese text (about 2 chars/token), 200,000 chars is about 100,000 tokens.
    # When exceeded, fail gracefully with a CAPACITY error before calling Bedrock,
    # which avoids wasted API cost.
    PERSONA_SOURCE_MAX_CHARS: int = 200_000
    S3_BUCKET_NAME: Optional[str] = None  # Set in .env; local storage when unset

    # AWS Bedrock settings
    AWS_REGION: str = "us-east-1"
    # Inference scope: global or jp. CDK passes it only for jp.
    # The defaults of the three model IDs follow from the scope (class attributes hold the global values)
    INFERENCE_SCOPE: str = InferenceScope.GLOBAL.value
    BEDROCK_MODEL_ID: str = SCOPE_DEFAULTS[InferenceScope.GLOBAL].bedrock_model_id

    # AI generation settings
    MAX_TOKENS: int = 4000
    # Output token limit for calls through the Strands Agent (persona generation, reports, etc.).
    # When unset it sticks to the Bedrock/model default (4,096 on Sonnet 5), which is
    # exceeded by bulk generation of several items or adaptive thinking (MaxTokensReachedException).
    AGENT_MAX_TOKENS: int = 32000

    # Agent Mode settings
    AGENT_MODEL_ID: str = SCOPE_DEFAULTS[InferenceScope.GLOBAL].agent_model_id
    DEFAULT_ROUNDS: int = 3
    MIN_ROUNDS: int = 1
    MAX_ROUNDS: int = 10

    # Additional persona base models for discussions and interviews (GPT5.6, Gemma4)
    ENABLE_ADDITIONAL_PERSONA_MODELS: bool = False

    # AgentCore Memory settings
    AGENTCORE_MEMORY_ID: Optional[str] = None
    AGENTCORE_MEMORY_REGION: str = "us-east-1"
    ENABLE_LONG_TERM_MEMORY: bool = False
    MEMORY_MAX_RESULTS: int = 5
    SUMMARY_MEMORY_STRATEGY_ID: Optional[str] = None
    SEMANTIC_MEMORY_STRATEGY_ID: Optional[str] = None

    # Mass survey settings (on-demand inference through Step Functions).
    # The model that generates answers (the scope default is Haiku 5.5; CDK always passes it)
    SURVEY_MODEL_ID: str = SCOPE_DEFAULTS[InferenceScope.GLOBAL].survey_model_id
    # Tables for the state machine and the worker record (passed by CDK). If either
    # is missing, surveys do not run (local runs)
    SURVEY_STATE_MACHINE_ARN: Optional[str] = None
    SURVEY_RUN_LEASE_TABLE_NAME: Optional[str] = None
    # Time limit of one execution (same value as the state machine's TimeoutSeconds)
    SURVEY_EXECUTION_TIMEOUT_SECONDS: int = 7200

    # Data analysis agent integration settings
    DATA_AGENT_RUNTIME_ARN: Optional[str] = None
    DATA_AGENT_REGION: str = "ap-northeast-1"
    ENABLE_DATA_AGENT: bool = False

    # Per-feature switches for what cannot be used in the closed network.
    # No "is closed network" flag is kept, only a value per feature. CDK passes them
    # only to closed-network tasks; the public configuration defaults keep today's behaviour.
    # Read at start-up and never rewritten while running.
    # Nemotron source: huggingface (an admin fetches it from the screen) or s3 (placed beforehand)
    NEMOTRON_SOURCE: str = NemotronSource.HUGGINGFACE.value
    # Key of the Nemotron Parquet (inside the app's S3 bucket)
    NEMOTRON_DATASET_KEY: str = DEFAULT_NEMOTRON_DATASET_KEY
    # Whether the Data Agent may be used in this environment. Not changeable from the settings screen either
    DATA_AGENT_ALLOWED: bool = True
    # File delivery mode: presigned (302 to a presigned URL, public configuration)
    # or proxy (stream through the app, passed by the closed-network Stage)
    ASSET_DELIVERY: str = AssetDeliveryMode.PRESIGNED.value

    # Global kill switch of the dataset structured-analysis tool (analyze_dataset).
    # This flag controls tool registration and prompt addition, not process start-up.
    # It is ANDed with the per-session enable_dataset flag.
    ENABLE_DATASET_ANALYSIS: bool = True

    # Number of queries the analyze_dataset DuckDB backend lets run at once (BoundedSemaphore).
    # Each query loads the whole dataset into an in-memory DB, so peak memory is proportional to
    # concurrency x dataset size. Adjust to the deployment's memory.
    DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES: int = 4

    # How long generated candidates (PersonaCandidates) are kept. Only used to compute a candidate's expires_at
    PERSONA_CACHE_TTL_SECONDS: int = 28800  # 8 hours

    # Login
    # Login method: oidc (Cognito Managed Login in the public configuration) or local (local use only).
    # An unset or unknown value, or the unimplemented admin_api, stops the start-up check.
    AUTH_MODE: Optional[str] = None
    # The app's own URL (scheme, host, port). Used to build the callback URL and
    # to check the Origin of login-related form submissions.
    APP_BASE_URL: str = "http://localhost:8000"
    COGNITO_USER_POOL_ID: Optional[str] = None
    COGNITO_CLIENT_ID: Optional[str] = None
    COGNITO_CLIENT_SECRET: Optional[str] = None
    # Managed Login domain (e.g. ai-persona-v1-dev-123.auth.ap-northeast-1.amazoncognito.com)
    COGNITO_DOMAIN: Optional[str] = None
    # List of token encryption keys (JSON: {"current": "v1", "k_v1": "..."}).
    # Received as an environment variable through the ECS task definition secrets.
    TOKEN_ENCRYPTION_KEYS: Optional[str] = None
    # Signing key of the in-progress login cookie (received through ECS secrets)
    LOGIN_COOKIE_SIGNING_KEY: Optional[str] = None
    SESSIONS_TABLE_NAME: Optional[str] = None
    # Physical name per table (the App stack passes the names CDK generated).
    # When unset, built from DYNAMODB_TABLE_PREFIX (for local development .env).
    PERSONAS_TABLE_NAME: Optional[str] = None
    DISCUSSIONS_TABLE_NAME: Optional[str] = None
    UPLOADED_FILES_TABLE_NAME: Optional[str] = None
    DATASETS_TABLE_NAME: Optional[str] = None
    SURVEY_TEMPLATES_TABLE_NAME: Optional[str] = None
    SURVEYS_TABLE_NAME: Optional[str] = None
    KNOWLEDGE_BASES_TABLE_NAME: Optional[str] = None
    JOBS_TABLE_NAME: Optional[str] = None
    # Per-department sharing index. Not put in TABLE_NAME_SETTINGS so that it is not
    # treated like the App stack's blanket grant (which includes Scan). When unset, built from the prefix
    RESOURCE_SHARES_TABLE_NAME: str = ""
    # Generated candidates and bindings. A dedicated item with restricted operations, like ResourceShares
    PERSONA_CANDIDATES_TABLE_NAME: str = ""
    PERSONA_BINDINGS_TABLE_NAME: str = ""
    # Discussion messages, reports and running interviews. Likewise a dedicated item with restricted operations
    DISCUSSION_MESSAGES_TABLE_NAME: str = ""
    DISCUSSION_REPORTS_TABLE_NAME: str = ""
    INTERVIEW_SESSIONS_TABLE_NAME: str = ""
    INTERVIEW_MESSAGES_TABLE_NAME: str = ""
    # Registration records of custom persona data. Likewise a dedicated item with restricted operations
    CUSTOM_PERSONA_DATA_TABLE_NAME: str = ""
    # Development users for AUTH_MODE=local (roles and departments are comma-separated)
    LOCAL_DEV_USER_NAME: str = "開発ユーザー"
    LOCAL_DEV_USER_EMAIL: str = "dev@example.com"
    LOCAL_DEV_USER_ROLES: str = "member"
    LOCAL_DEV_USER_GROUPS: str = ""
    # Ratio (0 to 1) of auth.request_checked lines emitted. Set to 1 when measuring
    AUTH_TIMING_SAMPLE_RATE: float = 0.1
    # Whether running inside ECS (presence of ECS_CONTAINER_METADATA_URI_V4).
    # Used to keep AUTH_MODE=local from starting on AWS
    RUNNING_IN_ECS: bool = False
    # Closed-network login
    # Password rules of the User Pool. Read only when AUTH_MODE=admin_api. Values are kept
    # as the received strings; checking them as numbers/booleans is done by PasswordPolicy in the start-up check
    PASSWORD_MIN_LENGTH: Optional[str] = None
    PASSWORD_REQUIRE_UPPERCASE: Optional[str] = None
    PASSWORD_REQUIRE_LOWERCASE: Optional[str] = None
    PASSWORD_REQUIRE_DIGITS: Optional[str] = None
    PASSWORD_REQUIRE_SYMBOLS: Optional[str] = None

    # Advanced generation (beta)
    # Whether the environment allows it. Default true; CDK passes false for the closed-network Stage
    ENABLE_ADVANCED_GENERATION: bool = True
    # Where analysis runs (AgentCore Code Interpreter ID). Unusable when empty
    CODE_INTERPRETER_ID: str = ""
    # Physical names of the three tables (passed by CDK; built from the prefix when unset)
    SOURCE_FILES_TABLE_NAME: str = ""
    ADVANCED_GENERATIONS_TABLE_NAME: str = ""
    APP_SETTINGS_TABLE_NAME: str = ""
    # Model of the planning, analysis and synthesis agents (default BEDROCK_MODEL_ID)
    ADVANCED_GENERATION_MODEL_ID: str = ""
    # Number of generations run at once in one task
    ADVANCED_GENERATION_MAX_CONCURRENT: int = 3
    # Upload
    ADVANCED_UPLOAD_MAX_BYTES: int = 1_000_000_000
    ADVANCED_UPLOAD_MAX_FILES: int = 10
    ADVANCED_UPLOAD_PART_SIZE_BYTES: int = 16 * 1024 * 1024
    ADVANCED_UPLOAD_URL_EXPIRATION_SECONDS: int = 3600
    ADVANCED_UPLOAD_URL_REFRESH_MAX: int = 24
    ADVANCED_UPLOAD_PENDING_MAX_SECONDS: int = 24 * 3600
    ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS: int = 15 * 60
    # Interval for re-reading a row stuck in inspection (the cut-off is INSPECT_RETRY_SECONDS)
    ADVANCED_UPLOAD_INSPECT_POLL_SECONDS: int = 2
    # Retention period of uploaded files. Keep it the same as SOURCE_RETENTION_DAYS in
    # cdk/lib/constructs/upload-bucket.ts
    ADVANCED_SOURCE_RETENTION_DAYS: int = 7
    # Records fixed per pass when correcting old records, and the time limit for it
    ADVANCED_SOURCE_RECONCILE_MAX_DELETES: int = 10
    ADVANCED_SOURCE_RECONCILE_MAX_SECONDS: int = 5
    # Size of the head read when finalizing
    ADVANCED_INSPECT_BYTES: int = 4 * 1024 * 1024
    # Generation
    ADVANCED_GENERATION_TIMEOUT_SECONDS: int = 60 * 60
    ADVANCED_GENERATION_PREPARE_TIMEOUT_SECONDS: int = 20 * 60
    # Interval for reading the preparation progress file (at most every 5 seconds)
    ADVANCED_GENERATION_PREPARE_POLL_SECONDS: int = 5
    # Interval for writing progress during preparation even when the row count does not change
    ADVANCED_GENERATION_PREPARE_NOTICE_SECONDS: int = 60
    # Stop when excluded rows exceed this share (integer %)
    ADVANCED_SOURCE_MAX_EXCLUDED_PERCENT: int = 1
    # A table with fewer rows than persona count x this value is treated as insufficient data
    ADVANCED_MIN_ROWS_PER_PERSONA: int = 10
    ADVANCED_GENERATION_MODEL_MIN_REMAINING_SECONDS: int = 10 * 60
    ADVANCED_GENERATION_LOCK_SECONDS: int = 75 * 60
    # Generation 60 min + total plan-approval wait 60 min + 10 min margin
    ADVANCED_GENERATION_SESSION_TIMEOUT_SECONDS: int = 7800
    ADVANCED_GENERATION_HEARTBEAT_SECONDS: int = 30
    # Wait allowed per plan card, and the total wait allowed in one generation
    ADVANCED_GENERATION_PLAN_WAIT_SECONDS: int = 15 * 60
    ADVANCED_GENERATION_PLAN_WAIT_TOTAL_SECONDS: int = 60 * 60
    # A generation whose heartbeat is older than this is treated as stopped
    ADVANCED_GENERATION_STALL_SECONDS: int = 5 * 60
    # Interval at which the watcher and the plan-approval wait re-read the generation
    ADVANCED_GENERATION_CONTROL_POLL_SECONDS: int = 5
    ADVANCED_GENERATION_RESULT_READABLE_SECONDS: int = 24 * 3600
    ADVANCED_GENERATION_ITEM_TTL_SECONDS: int = 48 * 3600
    # Expiry of the presigned GET used to bring files into the session
    ADVANCED_GENERATION_DOWNLOAD_URL_SECONDS: int = 900
    # Size of the result
    ADVANCED_GENERATION_ITEM_MAX_BYTES: int = 64 * 1024
    ADVANCED_GENERATION_PART_MAX_BYTES: int = 100 * 1024
    ADVANCED_GENERATION_PARTS_TOTAL_MAX_BYTES: int = 2 * 1024 * 1024
    # Analysis tools
    ADVANCED_TOOL_MAX_CALLS: int = 30
    ADVANCED_TOOL_MAX_TOTAL_SECONDS: int = 20 * 60
    ADVANCED_TOOL_MAX_OUTPUT_CHARS: int = 20_000
    # AI detailed analysis (run_code) and multiple tables
    ADVANCED_RUN_CODE_MAX_FAILURES: int = 5
    ADVANCED_RUN_CODE_TIMEOUT_SECONDS: int = 120
    ADVANCED_RUN_CODE_MAX_CHARS: int = 20_000
    ADVANCED_MAX_TABLES: int = 3
    ADVANCED_JOIN_MIN_MATCH_RATE: float = 0.5
    ADVANCED_JOIN_CHECK_TIMEOUT_SECONDS: int = 120
    # Documents
    ADVANCED_DOCUMENT_MAX_BYTES: int = 100_000_000
    ADVANCED_INSPECT_TAIL_BYTES: int = 1024 * 1024
    # Factor for the character estimate (characters per MB; a guide only, it does not stop anything by itself)
    ADVANCED_DOCUMENT_CHARS_PER_MB_PDF: int = 20_000
    ADVANCED_DOCUMENT_CHARS_PER_MB_DOCX: int = 100_000
    ADVANCED_DOCUMENT_CHUNK_CHARS: int = 15_000
    ADVANCED_DOCUMENT_CHUNK_OVERLAP_CHARS: int = 500
    ADVANCED_DOCUMENT_MAX_TOTAL_CHARS: int = 2_000_000
    # Characters received from the sandbox at once. Code Interpreter's standard output came back
    # intact up to 4 million characters, so use a value smaller than the output truncation (20,000 characters)
    ADVANCED_DOCUMENT_TRANSFER_CHARS: int = 10_000
    # Evidence extraction (the model defaults to SURVEY_MODEL_ID)
    ADVANCED_EVIDENCE_MODEL_ID: str = ""
    ADVANCED_EVIDENCE_MAX_CONCURRENT: int = 4
    ADVANCED_EVIDENCE_PER_CHUNK_MAX: int = 20
    ADVANCED_EVIDENCE_QUOTE_MAX_CHARS: int = 200
    ADVANCED_EVIDENCE_QUOTE_MIN_CHARS: int = 10
    ADVANCED_EVIDENCE_VERIFY_BATCH: int = 50
    # Retry count for transient errors in evidence extraction (dropped connections, throttling) and
    # the first wait in seconds (doubled on each retry)
    ADVANCED_EVIDENCE_MAX_RETRIES: int = 2
    ADVANCED_EVIDENCE_RETRY_BASE_SECONDS: int = 2

    def __post_init__(self) -> None:
        """Post-initialization of the settings"""
        # Override settings from environment variables
        self.AWS_REGION = os.getenv("AWS_REGION", self.AWS_REGION)
        agent_max_tokens = os.getenv("AGENT_MAX_TOKENS")
        if agent_max_tokens:
            self.AGENT_MAX_TOKENS = int(agent_max_tokens)

        enable_additional_persona_models = os.getenv(
            "ENABLE_ADDITIONAL_PERSONA_MODELS", ""
        ).lower()
        if enable_additional_persona_models in ("true", "1", "yes"):
            self.ENABLE_ADDITIONAL_PERSONA_MODELS = True
        elif enable_additional_persona_models in ("false", "0", "no"):
            self.ENABLE_ADDITIONAL_PERSONA_MODELS = False
        # Read the scope first and derive the defaults of the three model IDs from it
        self._load_inference_scope_setting()

        # Override DynamoDB settings from environment variables
        self.DYNAMODB_TABLE_PREFIX = os.getenv(
            "DYNAMODB_TABLE_PREFIX", self.DYNAMODB_TABLE_PREFIX
        )
        self.DYNAMODB_REGION = os.getenv("DYNAMODB_REGION", self.DYNAMODB_REGION)

        # Override AgentCore Memory settings from environment variables
        self.AGENTCORE_MEMORY_ID = os.getenv(
            "AGENTCORE_MEMORY_ID", self.AGENTCORE_MEMORY_ID
        )
        self.AGENTCORE_MEMORY_REGION = os.getenv(
            "AGENTCORE_MEMORY_REGION", self.AGENTCORE_MEMORY_REGION
        )
        enable_memory = os.getenv("ENABLE_LONG_TERM_MEMORY", "").lower()
        if enable_memory in ("true", "1", "yes"):
            self.ENABLE_LONG_TERM_MEMORY = True
        elif enable_memory in ("false", "0", "no"):
            self.ENABLE_LONG_TERM_MEMORY = False
        memory_max_results = os.getenv("MEMORY_MAX_RESULTS")
        if memory_max_results:
            self.MEMORY_MAX_RESULTS = int(memory_max_results)
        self.SUMMARY_MEMORY_STRATEGY_ID = os.getenv(
            "SUMMARY_MEMORY_STRATEGY_ID", self.SUMMARY_MEMORY_STRATEGY_ID
        )
        self.SEMANTIC_MEMORY_STRATEGY_ID = os.getenv(
            "SEMANTIC_MEMORY_STRATEGY_ID", self.SEMANTIC_MEMORY_STRATEGY_ID
        )

        # Override S3 settings from environment variables
        self.S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", self.S3_BUCKET_NAME)

        # Data analysis agent integration settings
        self.DATA_AGENT_RUNTIME_ARN = os.getenv(
            "DATA_AGENT_RUNTIME_ARN", self.DATA_AGENT_RUNTIME_ARN
        )
        self.DATA_AGENT_REGION = os.getenv("DATA_AGENT_REGION", self.DATA_AGENT_REGION)
        enable_val = os.getenv("ENABLE_DATA_AGENT", "").lower()
        if enable_val in ("true", "1", "yes"):
            self.ENABLE_DATA_AGENT = True
        elif enable_val in ("false", "0", "no"):
            self.ENABLE_DATA_AGENT = False

        enable_dataset_analysis = os.getenv("ENABLE_DATASET_ANALYSIS", "").lower()
        if enable_dataset_analysis in ("true", "1", "yes"):
            self.ENABLE_DATASET_ANALYSIS = True
        elif enable_dataset_analysis in ("false", "0", "no"):
            self.ENABLE_DATASET_ANALYSIS = False

        max_concurrent = os.getenv("DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES")
        if max_concurrent:
            # Do not fall back to the default on an invalid value; fail immediately (BoundedSemaphore
            # raises ValueError for 0 or negative values, so a misconfiguration surfaces at start-up).
            value = int(max_concurrent)
            if value < 1:
                raise ValueError(
                    f"DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES must be >= 1, got {value}"
                )
            self.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES = value

        # Override persona generation cache settings from environment variables
        persona_cache_ttl = os.getenv("PERSONA_CACHE_TTL_SECONDS")
        if persona_cache_ttl:
            self.PERSONA_CACHE_TTL_SECONDS = int(persona_cache_ttl)

        self._load_survey_runner_settings()
        self._load_closed_feature_settings()
        self._load_asset_delivery_setting()
        self._load_auth_settings()
        self._load_advanced_generation_settings()

        # Check that the directories exist and create them
        self._ensure_directories()

    def _load_survey_runner_settings(self) -> None:
        """Read the survey runner settings.

        Raises:
            ValueError: ``SURVEY_EXECUTION_TIMEOUT_SECONDS`` is not a positive
                integer.
        """
        self.SURVEY_STATE_MACHINE_ARN = (
            os.getenv("SURVEY_STATE_MACHINE_ARN", "").strip() or None
        )
        self.SURVEY_RUN_LEASE_TABLE_NAME = (
            os.getenv("SURVEY_RUN_LEASE_TABLE_NAME", "").strip() or None
        )
        timeout = os.getenv("SURVEY_EXECUTION_TIMEOUT_SECONDS", "").strip()
        if not timeout:
            return
        try:
            seconds = int(timeout)
        except ValueError as e:
            raise ValueError(
                f"SURVEY_EXECUTION_TIMEOUT_SECONDS must be an integer, got {timeout!r}"
            ) from e
        if seconds < 1:
            raise ValueError(
                f"SURVEY_EXECUTION_TIMEOUT_SECONDS must be >= 1, got {seconds}"
            )
        self.SURVEY_EXECUTION_TIMEOUT_SECONDS = seconds

    @property
    def survey_runner_configured(self) -> bool:
        """Whether surveys can run here: both the state machine and the lease
        table are set. Local runs have neither.
        """
        return bool(self.SURVEY_STATE_MACHINE_ARN and self.SURVEY_RUN_LEASE_TABLE_NAME)

    def _load_closed_feature_settings(self) -> None:
        """Read the three per-feature values of the closed network.

        Empty values mean the defaults (the public behaviour). Any other value
        that is not allowed stops the start with the variable's name; it never
        falls back to a default.

        Raises:
            ValueError: ``NEMOTRON_SOURCE``, ``NEMOTRON_DATASET_KEY`` or
                ``DATA_AGENT_ALLOWED`` holds a value that is not allowed.
        """
        source = os.getenv("NEMOTRON_SOURCE", "").strip().lower()
        if source:
            allowed_sources = [s.value for s in NemotronSource]
            if source not in allowed_sources:
                raise ValueError(
                    f"NEMOTRON_SOURCE must be one of {allowed_sources}, got {source!r}"
                )
            self.NEMOTRON_SOURCE = source

        key = os.getenv("NEMOTRON_DATASET_KEY", "").strip()
        if key:
            problem = nemotron_dataset_key_problem(key)
            if problem:
                raise ValueError(f"NEMOTRON_DATASET_KEY {key!r}: {problem}")
            self.NEMOTRON_DATASET_KEY = key

        allowed = os.getenv("DATA_AGENT_ALLOWED", "").strip().lower()
        if allowed:
            if allowed not in ("true", "false"):
                raise ValueError(
                    f"DATA_AGENT_ALLOWED must be 'true' or 'false', got {allowed!r}"
                )
            self.DATA_AGENT_ALLOWED = allowed == "true"

    def _load_asset_delivery_setting(self) -> None:
        """Read the file delivery mode of the closed network.

        Empty means ``presigned`` (the public behaviour). Values are read
        case-insensitively; anything other than ``presigned`` or ``proxy``
        stops the start with the variable's name and never falls back.

        Raises:
            ValueError: ``ASSET_DELIVERY`` holds a value that is not allowed.
        """
        mode = os.getenv("ASSET_DELIVERY", "").strip().lower()
        if not mode:
            return
        allowed_modes = [m.value for m in AssetDeliveryMode]
        if mode not in allowed_modes:
            raise ValueError(
                f"ASSET_DELIVERY must be one of {allowed_modes}, got {mode!r}"
            )
        self.ASSET_DELIVERY = mode

    def _load_inference_scope_setting(self) -> None:
        """Read the inference scope and the three model IDs.

        ``INFERENCE_SCOPE`` empty means ``global``; values are read
        case-insensitively. Each model ID uses its environment variable when it
        is set and not empty, otherwise the scope's default
        (``SCOPE_DEFAULTS``). Must run after ``ENABLE_ADDITIONAL_PERSONA_MODELS``
        is read.

        Raises:
            ValueError: ``INFERENCE_SCOPE`` holds a value that is not allowed,
                or ``jp`` is combined with ``ENABLE_ADDITIONAL_PERSONA_MODELS``.
        """
        scope_value = os.getenv("INFERENCE_SCOPE", "").strip().lower()
        if scope_value:
            allowed_scopes = [s.value for s in InferenceScope]
            if scope_value not in allowed_scopes:
                raise ValueError(
                    f"INFERENCE_SCOPE must be one of {allowed_scopes}, "
                    f"got {scope_value!r}"
                )
            self.INFERENCE_SCOPE = scope_value
        scope = InferenceScope(self.INFERENCE_SCOPE)
        if scope is InferenceScope.JP and self.ENABLE_ADDITIONAL_PERSONA_MODELS:
            raise ValueError(
                "INFERENCE_SCOPE=jp cannot be combined with "
                "ENABLE_ADDITIONAL_PERSONA_MODELS=true (the additional models "
                "call Bedrock Mantle in US regions)"
            )
        defaults = SCOPE_DEFAULTS[scope]
        self.BEDROCK_MODEL_ID = (
            os.getenv("BEDROCK_MODEL_ID") or defaults.bedrock_model_id
        )
        self.AGENT_MODEL_ID = os.getenv("AGENT_MODEL_ID") or defaults.agent_model_id
        self.SURVEY_MODEL_ID = os.getenv("SURVEY_MODEL_ID") or defaults.survey_model_id

    def _load_auth_settings(self) -> None:
        """Override login-related settings from environment variables."""
        optional_names = (
            "AUTH_MODE",
            "COGNITO_USER_POOL_ID",
            "COGNITO_CLIENT_ID",
            "COGNITO_CLIENT_SECRET",
            "COGNITO_DOMAIN",
            "TOKEN_ENCRYPTION_KEYS",
            "LOGIN_COOKIE_SIGNING_KEY",
            "SESSIONS_TABLE_NAME",
            *TABLE_NAME_SETTINGS.values(),
        )
        for name in optional_names:
            env_value = os.getenv(name)
            if env_value:
                setattr(self, name, env_value)
        for name in (
            "APP_BASE_URL",
            "LOCAL_DEV_USER_NAME",
            "LOCAL_DEV_USER_EMAIL",
            "LOCAL_DEV_USER_ROLES",
            "LOCAL_DEV_USER_GROUPS",
        ):
            setattr(self, name, os.getenv(name, getattr(self, name)))
        self.RUNNING_IN_ECS = bool(os.getenv("ECS_CONTAINER_METADATA_URI_V4"))
        if self.AUTH_MODE == "admin_api":
            for name in PASSWORD_POLICY_SETTINGS:
                setattr(self, name, os.getenv(name))
        self.RESOURCE_SHARES_TABLE_NAME = (
            os.getenv("RESOURCE_SHARES_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_ResourceShares"
        )
        self.PERSONA_CANDIDATES_TABLE_NAME = (
            os.getenv("PERSONA_CANDIDATES_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_PersonaCandidates"
        )
        self.PERSONA_BINDINGS_TABLE_NAME = (
            os.getenv("PERSONA_BINDINGS_TABLE_NAME")
            or f"{self.DYNAMODB_TABLE_PREFIX}_PersonaBindings"
        )
        for setting, kind in (
            ("DISCUSSION_MESSAGES_TABLE_NAME", "DiscussionMessages"),
            ("DISCUSSION_REPORTS_TABLE_NAME", "DiscussionReports"),
            ("INTERVIEW_SESSIONS_TABLE_NAME", "InterviewSessions"),
            ("INTERVIEW_MESSAGES_TABLE_NAME", "InterviewMessages"),
            ("CUSTOM_PERSONA_DATA_TABLE_NAME", "CustomPersonaData"),
        ):
            setattr(
                self,
                setting,
                os.getenv(setting) or f"{self.DYNAMODB_TABLE_PREFIX}_{kind}",
            )
        sample_rate = os.getenv("AUTH_TIMING_SAMPLE_RATE")
        if sample_rate:
            rate = float(sample_rate)
            if not 0.0 <= rate <= 1.0:
                raise ValueError(
                    f"AUTH_TIMING_SAMPLE_RATE must be between 0 and 1, got {rate}"
                )
            self.AUTH_TIMING_SAMPLE_RATE = rate

    def _load_advanced_generation_settings(self) -> None:
        """Read the advanced generation (beta) settings.

        Must run after the inference scope (``BEDROCK_MODEL_ID``) and
        ``DYNAMODB_TABLE_PREFIX`` are read.

        Raises:
            ValueError: ``ENABLE_ADVANCED_GENERATION`` is not a boolean word, or
                ``ADVANCED_GENERATION_MAX_CONCURRENT`` is not an integer >= 1.
        """
        enable = os.getenv("ENABLE_ADVANCED_GENERATION", "").strip().lower()
        if enable:
            if enable in ("true", "1", "yes"):
                self.ENABLE_ADVANCED_GENERATION = True
            elif enable in ("false", "0", "no"):
                self.ENABLE_ADVANCED_GENERATION = False
            else:
                raise ValueError(
                    "ENABLE_ADVANCED_GENERATION must be 'true' or 'false', "
                    f"got {enable!r}"
                )
        self.CODE_INTERPRETER_ID = os.getenv("CODE_INTERPRETER_ID", "").strip()
        for setting, kind in (
            ("SOURCE_FILES_TABLE_NAME", "SourceFiles"),
            ("ADVANCED_GENERATIONS_TABLE_NAME", "AdvancedGenerations"),
            ("APP_SETTINGS_TABLE_NAME", "AppSettings"),
        ):
            setattr(
                self,
                setting,
                os.getenv(setting) or f"{self.DYNAMODB_TABLE_PREFIX}_{kind}",
            )
        self.ADVANCED_GENERATION_MODEL_ID = (
            os.getenv("ADVANCED_GENERATION_MODEL_ID") or self.BEDROCK_MODEL_ID
        )
        # The light model of the evidence extraction
        self.ADVANCED_EVIDENCE_MODEL_ID = (
            os.getenv("ADVANCED_EVIDENCE_MODEL_ID") or self.SURVEY_MODEL_ID
        )
        max_concurrent = os.getenv("ADVANCED_GENERATION_MAX_CONCURRENT", "").strip()
        if max_concurrent:
            try:
                value = int(max_concurrent)
            except ValueError as e:
                raise ValueError(
                    "ADVANCED_GENERATION_MAX_CONCURRENT must be an integer, "
                    f"got {max_concurrent!r}"
                ) from e
            if value < 1:
                raise ValueError(
                    f"ADVANCED_GENERATION_MAX_CONCURRENT must be >= 1, got {value}"
                )
            self.ADVANCED_GENERATION_MAX_CONCURRENT = value

    def table_name(self, kind: str) -> str:
        """Physical DynamoDB name of one table kind.

        Args:
            kind: A key of :data:`TABLE_NAME_SETTINGS` such as ``"Personas"``.

        Returns:
            The name passed by CDK (``PERSONAS_TABLE_NAME`` etc.), or
            ``{DYNAMODB_TABLE_PREFIX}_{kind}`` for local development.

        Raises:
            KeyError: ``kind`` is not one of the eight bulk-granted tables.
        """
        explicit = getattr(self, TABLE_NAME_SETTINGS[kind])
        return explicit or f"{self.DYNAMODB_TABLE_PREFIX}_{kind}"

    def password_policy_settings(self) -> dict[str, Optional[str]]:
        """The five password policy settings as read (``None`` when unset).

        Only ``AUTH_MODE=admin_api`` reads them; under other modes every
        value is ``None``.
        """
        return {name: getattr(self, name) for name in PASSWORD_POLICY_SETTINGS}

    def _ensure_directories(self) -> None:
        """Make sure the required directories exist, creating them if not"""
        directories = [
            Path(self.UPLOAD_DIR),
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def upload_dir(self) -> Path:
        """Return the Path object of the upload directory"""
        return Path(self.UPLOAD_DIR)

    def get_aws_credentials(self) -> dict:
        """Get AWS credentials from environment variables"""
        return {
            "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
            "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
            "aws_session_token": os.getenv("AWS_SESSION_TOKEN"),
            "region_name": self.AWS_REGION,
        }

    def get_default_insight_categories(self) -> List["InsightCategory"]:
        """Get default insight categories for the system."""
        from src.models.insight_category import InsightCategory

        return InsightCategory.get_default_categories()


# Global settings instance
config = Config()
