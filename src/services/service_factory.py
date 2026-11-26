"""
Service Factory
Factory class that manages service instances across the application
"""

import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, TYPE_CHECKING
from threading import Lock

from .ai_service import AIService
from .agent_service import AgentService
from ..config import config
from ..models.errors import CodedError, ErrorCode

if TYPE_CHECKING:
    from ..models.login_session import SessionStore
    from .authz_lookup_service import AuthzLookupService
    from .cognito_groups_service import CognitoGroupsService
    from .cognito.cognito_auth_service import CognitoAuthService
    from .cognito.oidc_service import OidcService
    from .stores.advanced_generation_store import AdvancedGenerationStore
    from .stores.app_settings_store import AppSettingsStore
    from .stores.source_file_store import SourceFileStore
    from .stores.custom_persona_data_store import CustomPersonaDataStore
    from .stores.dataset_store import DatasetStore
    from .stores.job_store import JobStore
    from .stores.knowledge_base_store import KnowledgeBaseStore
    from .stores.persona_binding_store import PersonaBindingStore
    from .stores.persona_candidate_store import PersonaCandidateStore
    from .stores.persona_store import PersonaStore
    from .stores.resource_share_store import ResourceShareStore
    from .stores.survey_run_lease_store import SurveyRunLeaseStore
    from .stores.survey_store import SurveyStore
    from .stores.survey_template_store import SurveyTemplateStore
    from .stores.uploaded_file_store import UploadedFileStore
    from .stores.discussion_store import DiscussionStore
    from .stores.interview_session_store import InterviewSessionStore
    from .user_directory_service import UserDirectoryService
    from .data_agent_service import DataAgentService
    from .dataset_analysis.service import DatasetAnalysisService
    from .memory.memory_service import MemoryService
    from .s3_service import S3Service
    from .survey_batch_service import SurveyBatchService
    from .survey_runner_service import SurveyRunnerService
    from .sandbox_service import SandboxService
    from .dataset_analysis.analysis_tool_service import (
        AnalysisTable,
        AnalysisToolService,
    )
    from ..models.analysis import AnalysisToolPolicy, RunCodeGate

#: The fixed library written into each analysis session (read as a file, never
#: imported).
_SANDBOX_LIBRARY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "sandbox" / "persona_analysis.py"
)


class ServiceFactory:
    """
    Singleton factory that manages service instances
    Reuses services across the whole application
    """

    _instance: Optional["ServiceFactory"] = None
    _lock = Lock()

    def __new__(cls) -> "ServiceFactory":
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize (runs only once)."""
        if hasattr(self, "_initialized"):
            return

        self.logger = logging.getLogger(__name__)
        self._ai_service: Optional[AIService] = None
        self._agent_service: Optional[AgentService] = None
        self._memory_service: Optional["MemoryService"] = None
        self._memory_service_attempted: bool = False
        self._s3_service: Optional["S3Service"] = None
        self._survey_batch_service: Optional["SurveyBatchService"] = None
        self._dataset_analysis_service: Optional["DatasetAnalysisService"] = None
        self._session_store: Optional["SessionStore"] = None
        self._oidc_service: Optional["OidcService"] = None
        self._cognito_auth_service: Optional["CognitoAuthService"] = None
        self._authz_lookup_service: Optional["AuthzLookupService"] = None
        self._cognito_groups_service: Optional["CognitoGroupsService"] = None
        self._resource_share_store: Optional["ResourceShareStore"] = None
        self._user_directory_service: Optional["UserDirectoryService"] = None
        self._persona_candidate_store: Optional["PersonaCandidateStore"] = None
        self._persona_binding_store: Optional["PersonaBindingStore"] = None
        self._job_store: Optional["JobStore"] = None
        self._dataset_store: Optional["DatasetStore"] = None
        self._survey_template_store: Optional["SurveyTemplateStore"] = None
        self._survey_store: Optional["SurveyStore"] = None
        self._survey_run_lease_store: Optional["SurveyRunLeaseStore"] = None
        self._survey_runner_service: Optional["SurveyRunnerService"] = None
        self._persona_store: Optional["PersonaStore"] = None
        self._knowledge_base_store: Optional["KnowledgeBaseStore"] = None
        self._uploaded_file_store: Optional["UploadedFileStore"] = None
        self._discussion_store: Optional["DiscussionStore"] = None
        self._interview_session_store: Optional["InterviewSessionStore"] = None
        self._custom_persona_data_store: Optional["CustomPersonaDataStore"] = None
        self._source_file_store: Optional["SourceFileStore"] = None
        self._advanced_generation_store: Optional["AdvancedGenerationStore"] = None
        self._app_settings_store: Optional["AppSettingsStore"] = None
        self._sandbox_service: Optional["SandboxService"] = None
        self._initialized = True

        self.logger.info("ServiceFactory initialized")

    def get_ai_service(self) -> AIService:
        """
        Get the singleton instance of AIService

        Returns:
            AIService: AI service instance
        """
        if self._ai_service is None:
            with self._lock:
                if self._ai_service is None:
                    self.logger.info("Creating new AIService instance")
                    self._ai_service = AIService()
        return self._ai_service

    def get_agent_service(self) -> AgentService:
        """
        Get the singleton instance of AgentService

        Returns:
            AgentService: Agent service instance
        """
        if self._agent_service is None:
            with self._lock:
                if self._agent_service is None:
                    self.logger.info("Creating new AgentService instance")
                    self._agent_service = AgentService()
        return self._agent_service

    def is_memory_enabled(self) -> bool:
        """
        Check whether the long-term memory feature is enabled

        Returns:
            bool: True if long-term memory is enabled
        """
        from .memory.session_manager_factory import is_memory_enabled

        return is_memory_enabled()

    def get_memory_service(self) -> Optional["MemoryService"]:
        """
        Get the singleton instance of MemoryService

        Used for memory management from the UI (listing, deletion, etc.).
        Agent memory integration uses AgentCoreMemorySessionManager.

        Returns None if ENABLE_LONG_TERM_MEMORY is False or AGENTCORE_MEMORY_ID is
        not set.

        Returns:
            MemoryService or None: Memory service instance,
                                   or None if the feature is disabled
        """
        # Return None if the feature is disabled
        if not config.ENABLE_LONG_TERM_MEMORY:
            self.logger.debug("Long-term memory is disabled by configuration")
            return None

        # Return None if AGENTCORE_MEMORY_ID is not set
        if not config.AGENTCORE_MEMORY_ID:
            self.logger.warning(
                "ENABLE_LONG_TERM_MEMORY is True but AGENTCORE_MEMORY_ID is not set"
            )
            return None

        # Return None if creation was already attempted and failed
        if self._memory_service_attempted and self._memory_service is None:
            return None

        if self._memory_service is None:
            with self._lock:
                if self._memory_service is None and not self._memory_service_attempted:
                    self._memory_service_attempted = True
                    try:
                        # Lazy import to avoid a circular reference
                        from .memory.memory_service import MemoryService

                        self.logger.info(
                            "Creating new MemoryService instance "
                            f"(memory_id={config.AGENTCORE_MEMORY_ID}, "
                            f"region={config.AGENTCORE_MEMORY_REGION})"
                        )
                        self._memory_service = MemoryService(
                            memory_id=config.AGENTCORE_MEMORY_ID,
                            region=config.AGENTCORE_MEMORY_REGION,
                            validate_connection=True,
                        )
                    except Exception as e:
                        self.logger.error(f"Failed to create MemoryService: {e}")
                        # The application can continue even on failure
                        # Returning None lets callers handle it appropriately
                        self._memory_service = None

        return self._memory_service

    def resolve_memory_strategies(self) -> None:
        """Look up the AgentCore Memory strategy IDs with GetMemory at startup and put them in config.

        IDs passed as environment variables are used as is. If the lookup fails, long-term memory is disabled.
        """
        from .memory.strategy_lookup import resolve_memory_strategies

        resolve_memory_strategies(config)

    def check_stm_session_exists(self, session_id: str) -> bool:
        """Check whether session history exists in AgentCore Memory STM.

        Keeps SDK calls inside the Service layer.
        """
        try:
            if not self.is_memory_enabled():
                return False

            if not config.AGENTCORE_MEMORY_ID:
                return False

            from bedrock_agentcore.memory.integrations.strands.config import (
                AgentCoreMemoryConfig,
            )
            from bedrock_agentcore.memory.integrations.strands.session_manager import (
                AgentCoreMemorySessionManager,
            )

            memory_config = AgentCoreMemoryConfig(
                memory_id=config.AGENTCORE_MEMORY_ID,
                session_id=session_id,
                actor_id="report-agent",
                retrieval_config={},
            )
            sm = AgentCoreMemorySessionManager(
                agentcore_memory_config=memory_config,
                region_name=config.AGENTCORE_MEMORY_REGION,
            )
            session_data = sm.read_session(session_id=session_id)
            return session_data is not None
        except Exception as e:
            self.logger.debug(f"Failed to check STM session: {e}")
            return False

    def get_s3_service(self) -> "S3Service":
        """Get the singleton instance of S3Service. Raises RuntimeError if S3_BUCKET_NAME is not set."""
        if self._s3_service is None:
            with self._lock:
                if self._s3_service is None:
                    if not config.S3_BUCKET_NAME:
                        raise RuntimeError(
                            "S3_BUCKET_NAME が設定されていません。環境変数を確認してください。"
                        )
                    from .s3_service import S3Service

                    self.logger.info(
                        f"Creating new S3Service instance "
                        f"(bucket={config.S3_BUCKET_NAME}, region={config.AWS_REGION})"
                    )
                    self._s3_service = S3Service(
                        bucket_name=config.S3_BUCKET_NAME,
                        region_name=config.AWS_REGION,
                    )
        return self._s3_service

    def get_optional_s3_service(self) -> Optional["S3Service"]:
        """The S3 service, or ``None`` when ``S3_BUCKET_NAME`` is not set."""
        if not config.S3_BUCKET_NAME:
            return None
        return self.get_s3_service()

    def get_survey_batch_service(self) -> "SurveyBatchService":
        """Get the singleton instance of SurveyBatchService."""
        if self._survey_batch_service is None:
            with self._lock:
                if self._survey_batch_service is None:
                    from .survey_batch_service import SurveyBatchService

                    self.logger.info("Creating new SurveyBatchService instance")
                    self._survey_batch_service = SurveyBatchService(
                        bucket_name=config.S3_BUCKET_NAME or "",
                        region_name=config.AWS_REGION,
                    )
        return self._survey_batch_service

    def get_dataset_analysis_service(self) -> "DatasetAnalysisService":
        """Get the singleton instance of DatasetAnalysisService.

        Assembles the analyze_dataset tool (DuckDB backend).
        """
        if self._dataset_analysis_service is None:
            with self._lock:
                if self._dataset_analysis_service is None:
                    from .dataset_analysis.service import DatasetAnalysisService

                    self.logger.info("Creating new DatasetAnalysisService instance")
                    self._dataset_analysis_service = DatasetAnalysisService(
                        bucket_name=config.S3_BUCKET_NAME or "",
                        region_name=config.AWS_REGION,
                        max_concurrent_queries=(
                            config.DATASET_ANALYSIS_MAX_CONCURRENT_QUERIES
                        ),
                    )
        return self._dataset_analysis_service

    def get_session_store(self) -> "SessionStore":
        """Return the LoginSession store for the running AUTH_MODE.

        ``local`` keeps sessions in the process; ``oidc`` uses the
        DynamoDB Sessions table so every task reads the same session.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` when ``oidc`` has no table name.
        """
        if self._session_store is None:
            with self._lock:
                if self._session_store is None:
                    self._session_store = self._create_session_store()
        return self._session_store

    def _create_session_store(self) -> "SessionStore":
        if config.AUTH_MODE == "local":
            from .stores.memory_session_store import MemorySessionStore

            self.logger.info("Using the in-process session store (AUTH_MODE=local)")
            return MemorySessionStore()
        if not config.SESSIONS_TABLE_NAME:
            raise CodedError(
                "SESSIONS_TABLE_NAME is required unless AUTH_MODE=local",
                code=ErrorCode.AUTH_CONFIG_INVALID,
            )
        from .session_store_service import SessionStoreService

        self.logger.info("Using the DynamoDB session store")
        return SessionStoreService(
            table_name=config.SESSIONS_TABLE_NAME, region=config.DYNAMODB_REGION
        )

    def get_oidc_service(self) -> "OidcService":
        """Return the Cognito OIDC client (AUTH_MODE=oidc only).

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` when a Cognito setting is missing.
        """
        if self._oidc_service is None:
            with self._lock:
                if self._oidc_service is None:
                    self._oidc_service = self._create_oidc_service()
        return self._oidc_service

    def _create_oidc_service(self) -> "OidcService":
        required = {
            "COGNITO_USER_POOL_ID": config.COGNITO_USER_POOL_ID,
            "COGNITO_CLIENT_ID": config.COGNITO_CLIENT_ID,
            "COGNITO_CLIENT_SECRET": config.COGNITO_CLIENT_SECRET,
            "COGNITO_DOMAIN": config.COGNITO_DOMAIN,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise CodedError(
                f"Cognito settings are missing: {', '.join(missing)}",
                code=ErrorCode.AUTH_CONFIG_INVALID,
            )
        from .cognito.oidc_service import OidcService

        self.logger.info("Creating new OidcService instance")
        return OidcService(
            user_pool_id=config.COGNITO_USER_POOL_ID or "",
            client_id=config.COGNITO_CLIENT_ID or "",
            client_secret=config.COGNITO_CLIENT_SECRET or "",
            domain=config.COGNITO_DOMAIN or "",
            redirect_uri=config.APP_BASE_URL.rstrip("/") + "/auth/callback",
        )

    def get_cognito_auth_service(self) -> "CognitoAuthService":
        """Return the Cognito admin API client (AUTH_MODE=admin_api only).

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` when a Cognito setting is missing.
        """
        if self._cognito_auth_service is None:
            with self._lock:
                if self._cognito_auth_service is None:
                    self._cognito_auth_service = self._create_cognito_auth_service()
        return self._cognito_auth_service

    def _create_cognito_auth_service(self) -> "CognitoAuthService":
        required = {
            "COGNITO_USER_POOL_ID": config.COGNITO_USER_POOL_ID,
            "COGNITO_CLIENT_ID": config.COGNITO_CLIENT_ID,
            "COGNITO_CLIENT_SECRET": config.COGNITO_CLIENT_SECRET,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise CodedError(
                f"Cognito settings are missing: {', '.join(missing)}",
                code=ErrorCode.AUTH_CONFIG_INVALID,
            )
        from .cognito.cognito_auth_service import CognitoAuthService

        self.logger.info("Creating new CognitoAuthService instance")
        return CognitoAuthService(
            user_pool_id=config.COGNITO_USER_POOL_ID or "",
            client_id=config.COGNITO_CLIENT_ID or "",
            client_secret=config.COGNITO_CLIENT_SECRET or "",
        )

    def get_authz_lookup_service(self) -> "AuthzLookupService":
        """Return the reader of authorization columns and of the Jobs table.

        Its DynamoDB client has the short timeouts and limited retries
        and is separate from the clients of the table stores.
        """
        if self._authz_lookup_service is None:
            with self._lock:
                if self._authz_lookup_service is None:
                    from .authz_lookup_service import AuthzLookupService

                    self.logger.info("Creating new AuthzLookupService instance")
                    self._authz_lookup_service = AuthzLookupService(
                        region=config.DYNAMODB_REGION,
                        jobs_table_name=config.table_name("Jobs"),
                    )
        return self._authz_lookup_service

    def get_cognito_groups_service(self) -> "CognitoGroupsService":
        """Return the department-group reader of the Cognito user pool.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` when ``COGNITO_USER_POOL_ID``
                is not set.
        """
        if self._cognito_groups_service is None:
            with self._lock:
                if self._cognito_groups_service is None:
                    if not config.COGNITO_USER_POOL_ID:
                        raise CodedError(
                            "COGNITO_USER_POOL_ID is required for the department list",
                            code=ErrorCode.AUTH_CONFIG_INVALID,
                        )
                    from .cognito_groups_service import CognitoGroupsService

                    self.logger.info("Creating new CognitoGroupsService instance")
                    self._cognito_groups_service = CognitoGroupsService(
                        user_pool_id=config.COGNITO_USER_POOL_ID
                    )
        return self._cognito_groups_service

    def get_resource_share_store(self) -> "ResourceShareStore":
        """Return the store of department shares.

        The body tables it also writes are passed per call from
        ``ResourceSharing``'s registration, so only the ResourceShares table
        name is configured here.
        """
        if self._resource_share_store is None:
            with self._lock:
                if self._resource_share_store is None:
                    from .stores.resource_share_store import ResourceShareStore

                    self.logger.info("Creating new ResourceShareStore instance")
                    self._resource_share_store = ResourceShareStore(
                        region=config.DYNAMODB_REGION,
                        shares_table=config.RESOURCE_SHARES_TABLE_NAME,
                    )
        return self._resource_share_store

    def get_persona_candidate_store(self) -> "PersonaCandidateStore":
        """Return the store of generated personas waiting to be saved."""
        if self._persona_candidate_store is None:
            with self._lock:
                if self._persona_candidate_store is None:
                    from .stores.persona_candidate_store import PersonaCandidateStore

                    self.logger.info("Creating new PersonaCandidateStore instance")
                    self._persona_candidate_store = PersonaCandidateStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.PERSONA_CANDIDATES_TABLE_NAME,
                    )
        return self._persona_candidate_store

    def get_source_file_store(self) -> "SourceFileStore":
        """Return the store of uploaded source files."""
        if self._source_file_store is None:
            with self._lock:
                if self._source_file_store is None:
                    from .stores.source_file_store import SourceFileStore

                    self.logger.info("Creating new SourceFileStore instance")
                    self._source_file_store = SourceFileStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.SOURCE_FILES_TABLE_NAME,
                    )
        return self._source_file_store

    def get_advanced_generation_store(self) -> "AdvancedGenerationStore":
        """Return the store of advanced generations."""
        if self._advanced_generation_store is None:
            with self._lock:
                if self._advanced_generation_store is None:
                    from .stores.advanced_generation_store import (
                        AdvancedGenerationStore,
                    )

                    self.logger.info("Creating new AdvancedGenerationStore instance")
                    self._advanced_generation_store = AdvancedGenerationStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.ADVANCED_GENERATIONS_TABLE_NAME,
                        source_files_table_name=config.SOURCE_FILES_TABLE_NAME,
                    )
        return self._advanced_generation_store

    def get_app_settings_store(self) -> "AppSettingsStore":
        """Return the reader of application settings."""
        if self._app_settings_store is None:
            with self._lock:
                if self._app_settings_store is None:
                    from .stores.app_settings_store import AppSettingsStore

                    self.logger.info("Creating new AppSettingsStore instance")
                    self._app_settings_store = AppSettingsStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.APP_SETTINGS_TABLE_NAME,
                    )
        return self._app_settings_store

    def get_sandbox_service(self) -> "SandboxService":
        """Return the analysis sandbox on Code Interpreter.

        Raises:
            CodedError: ``ADVANCED_GENERATION_UNAVAILABLE`` when no Code
                Interpreter is configured.
        """
        if not config.CODE_INTERPRETER_ID:
            raise CodedError(
                "CODE_INTERPRETER_ID is not set",
                code=ErrorCode.ADVANCED_GENERATION_UNAVAILABLE,
            )
        if self._sandbox_service is None:
            with self._lock:
                if self._sandbox_service is None:
                    from .sandbox_service import CodeInterpreterSandbox

                    self.logger.info("Creating new CodeInterpreterSandbox instance")
                    self._sandbox_service = CodeInterpreterSandbox(
                        code_interpreter_id=config.CODE_INTERPRETER_ID,
                        region_name=config.AWS_REGION,
                        library_path=str(_SANDBOX_LIBRARY_PATH),
                        max_output_chars=config.ADVANCED_TOOL_MAX_OUTPUT_CHARS,
                    )
        return self._sandbox_service

    def create_analysis_tools(
        self,
        session: Any,
        policy: "AnalysisToolPolicy",
        *,
        source_id: str = "",
        columns: Sequence[str] = (),
        persona_count: int,
        sandbox: Optional["SandboxService"] = None,
        tables: Optional[Sequence["AnalysisTable"]] = None,
        gate: Optional["RunCodeGate"] = None,
        expected_library_sha256: Optional[str] = None,
        expected_parquet_sha256: Optional[Mapping[str, str]] = None,
    ) -> "AnalysisToolService":
        """Build the tools of one generation's analysis (never cached).

        ``sandbox`` defaults to :meth:`get_sandbox_service`; pass the one the
        session was opened on when it is another (the in-process version).
        ``tables`` are the generation's tables (``source_1``...), ``gate``
        decides on ``run_code`` (the Manager's budget) and the expected hashes
        are the trust anchor of the check after each ``run_code``.
        """
        from .dataset_analysis.analysis_tool_service import AnalysisToolService

        return AnalysisToolService(
            sandbox if sandbox is not None else self.get_sandbox_service(),
            session,
            policy,
            source_id=source_id,
            columns=columns,
            persona_count=persona_count,
            tables=tables,
            gate=gate,
            expected_library_sha256=expected_library_sha256,
            expected_parquet_sha256=expected_parquet_sha256,
        )

    def get_persona_binding_store(self) -> "PersonaBindingStore":
        """Return the store of persona bindings."""
        if self._persona_binding_store is None:
            with self._lock:
                if self._persona_binding_store is None:
                    from .stores.persona_binding_store import PersonaBindingStore

                    self.logger.info("Creating new PersonaBindingStore instance")
                    self._persona_binding_store = PersonaBindingStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.PERSONA_BINDINGS_TABLE_NAME,
                    )
        return self._persona_binding_store

    def get_job_store(self) -> "JobStore":
        """Return the writer of job records."""
        if self._job_store is None:
            with self._lock:
                if self._job_store is None:
                    from .stores.job_store import JobStore

                    self.logger.info("Creating new JobStore instance")
                    self._job_store = JobStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Jobs"),
                    )
        return self._job_store

    def get_dataset_store(self) -> "DatasetStore":
        """Return the store of registered datasets."""
        if self._dataset_store is None:
            with self._lock:
                if self._dataset_store is None:
                    from .stores.dataset_store import DatasetStore

                    self.logger.info("Creating new DatasetStore instance")
                    self._dataset_store = DatasetStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Datasets"),
                    )
        return self._dataset_store

    def get_persona_store(self) -> "PersonaStore":
        """Return the store of persona bodies."""
        if self._persona_store is None:
            with self._lock:
                if self._persona_store is None:
                    from .stores.persona_store import PersonaStore

                    self.logger.info("Creating new PersonaStore instance")
                    self._persona_store = PersonaStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Personas"),
                    )
        return self._persona_store

    def get_survey_store(self) -> "SurveyStore":
        """Return the store of survey bodies."""
        if self._survey_store is None:
            with self._lock:
                if self._survey_store is None:
                    from .stores.survey_store import SurveyStore

                    self.logger.info("Creating new SurveyStore instance")
                    self._survey_store = SurveyStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Surveys"),
                    )
        return self._survey_store

    def get_survey_run_lease_store(self) -> "SurveyRunLeaseStore":
        """Return the store of the survey runner's lease."""
        if self._survey_run_lease_store is None:
            with self._lock:
                if self._survey_run_lease_store is None:
                    from .stores.survey_run_lease_store import SurveyRunLeaseStore

                    self.logger.info("Creating new SurveyRunLeaseStore instance")
                    self._survey_run_lease_store = SurveyRunLeaseStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.SURVEY_RUN_LEASE_TABLE_NAME or "",
                    )
        return self._survey_run_lease_store

    def get_survey_runner_service(self) -> "SurveyRunnerService":
        """Return the Step Functions runner of surveys.

        Raises:
            RuntimeError: ``SURVEY_STATE_MACHINE_ARN`` is not set (callers check
                ``config.survey_runner_configured`` first).
        """
        if self._survey_runner_service is None:
            with self._lock:
                if self._survey_runner_service is None:
                    if not config.SURVEY_STATE_MACHINE_ARN:
                        raise RuntimeError("SURVEY_STATE_MACHINE_ARN is not set")
                    from .survey_runner_service import SurveyRunnerService

                    self.logger.info("Creating new SurveyRunnerService instance")
                    self._survey_runner_service = SurveyRunnerService(
                        state_machine_arn=config.SURVEY_STATE_MACHINE_ARN,
                        bucket_name=config.S3_BUCKET_NAME or "",
                        region=config.AWS_REGION,
                    )
        return self._survey_runner_service

    def get_survey_template_store(self) -> "SurveyTemplateStore":
        """Return the store of survey template bodies."""
        if self._survey_template_store is None:
            with self._lock:
                if self._survey_template_store is None:
                    from .stores.survey_template_store import SurveyTemplateStore

                    self.logger.info("Creating new SurveyTemplateStore instance")
                    self._survey_template_store = SurveyTemplateStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("SurveyTemplates"),
                    )
        return self._survey_template_store

    def get_knowledge_base_store(self) -> "KnowledgeBaseStore":
        """Return the store of registered knowledge bases."""
        if self._knowledge_base_store is None:
            with self._lock:
                if self._knowledge_base_store is None:
                    from .stores.knowledge_base_store import KnowledgeBaseStore

                    self.logger.info("Creating new KnowledgeBaseStore instance")
                    self._knowledge_base_store = KnowledgeBaseStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("KnowledgeBases"),
                    )
        return self._knowledge_base_store

    def get_custom_persona_data_store(self) -> "CustomPersonaDataStore":
        """Return the store of custom persona data records."""
        if self._custom_persona_data_store is None:
            with self._lock:
                if self._custom_persona_data_store is None:
                    from .stores.custom_persona_data_store import CustomPersonaDataStore

                    self.logger.info("Creating new CustomPersonaDataStore instance")
                    self._custom_persona_data_store = CustomPersonaDataStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.CUSTOM_PERSONA_DATA_TABLE_NAME,
                    )
        return self._custom_persona_data_store

    def get_uploaded_file_store(self) -> "UploadedFileStore":
        """Return the store of upload metadata."""
        if self._uploaded_file_store is None:
            with self._lock:
                if self._uploaded_file_store is None:
                    from .stores.uploaded_file_store import UploadedFileStore

                    self.logger.info("Creating new UploadedFileStore instance")
                    self._uploaded_file_store = UploadedFileStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("UploadedFiles"),
                    )
        return self._uploaded_file_store

    def get_discussion_store(self) -> "DiscussionStore":
        """Return the store of discussion bodies, messages and reports."""
        if self._discussion_store is None:
            with self._lock:
                if self._discussion_store is None:
                    from .stores.discussion_store import DiscussionStore

                    self.logger.info("Creating new DiscussionStore instance")
                    self._discussion_store = DiscussionStore(
                        region=config.DYNAMODB_REGION,
                        discussions_table=config.table_name("Discussions"),
                        messages_table=config.DISCUSSION_MESSAGES_TABLE_NAME,
                        reports_table=config.DISCUSSION_REPORTS_TABLE_NAME,
                    )
        return self._discussion_store

    def get_interview_session_store(self) -> "InterviewSessionStore":
        """Return the store of running interviews."""
        if self._interview_session_store is None:
            with self._lock:
                if self._interview_session_store is None:
                    from .stores.interview_session_store import InterviewSessionStore

                    self.logger.info("Creating new InterviewSessionStore instance")
                    self._interview_session_store = InterviewSessionStore(
                        region=config.DYNAMODB_REGION,
                        sessions_table=config.INTERVIEW_SESSIONS_TABLE_NAME,
                        messages_table=config.INTERVIEW_MESSAGES_TABLE_NAME,
                    )
        return self._interview_session_store

    def get_user_directory_service(self) -> "UserDirectoryService":
        """Return the user-list reader for owner names.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` when ``COGNITO_USER_POOL_ID``
                is not set.
        """
        if self._user_directory_service is None:
            with self._lock:
                if self._user_directory_service is None:
                    if not config.COGNITO_USER_POOL_ID:
                        raise CodedError(
                            "COGNITO_USER_POOL_ID is required for the owner names",
                            code=ErrorCode.AUTH_CONFIG_INVALID,
                        )
                    from .user_directory_service import UserDirectoryService

                    self.logger.info("Creating new UserDirectoryService instance")
                    self._user_directory_service = UserDirectoryService(
                        user_pool_id=config.COGNITO_USER_POOL_ID
                    )
        return self._user_directory_service

    def get_department_source(self) -> Any:
        """Return where the departments come from.

        The Cognito ``group:`` groups when a user pool is configured; under
        ``AUTH_MODE=local`` without one, the development user's groups.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` outside local mode without a pool.
        """
        if config.COGNITO_USER_POOL_ID or config.AUTH_MODE != "local":
            return self.get_cognito_groups_service()
        return self._local_directory()

    def get_user_source(self) -> Any:
        """Return where the owner names come from.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` outside local mode without a pool.
        """
        if config.COGNITO_USER_POOL_ID or config.AUTH_MODE != "local":
            return self.get_user_directory_service()
        return self._local_directory()

    def _local_directory(self) -> Any:
        from .local_directory_service import LocalDirectoryService

        self.logger.info("Using the local department and user list (AUTH_MODE=local)")
        return LocalDirectoryService(config)

    def get_data_agent_service(self) -> Optional["DataAgentService"]:
        """
        Get the DataAgentService instance

        Returns:
            DataAgentService: Data analysis agent service (None if not configured)
        """
        if not config.DATA_AGENT_RUNTIME_ARN:
            return None
        from .data_agent_service import DataAgentService

        return DataAgentService(config.DATA_AGENT_RUNTIME_ARN, config.DATA_AGENT_REGION)


# Global factory instance
service_factory = ServiceFactory()
