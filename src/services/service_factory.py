"""
Service Factory
アプリケーション全体でサービスインスタンスを管理するファクトリークラス
"""

import logging
from typing import Any, Optional, TYPE_CHECKING
from threading import Lock

from .ai_service import AIService
from .agent_service import AgentService
from .database_service import DatabaseService
from ..config import config
from ..models.errors import CodedError, ErrorCode

if TYPE_CHECKING:
    from ..models.login_session import SessionStore
    from .authz_lookup_service import AuthzLookupService
    from .cognito_groups_service import CognitoGroupsService
    from .cognito.cognito_auth_service import CognitoAuthService
    from .cognito.oidc_service import OidcService
    from .custom_persona_data_store import CustomPersonaDataStore
    from .dataset_store import DatasetStore
    from .job_store import JobStore
    from .knowledge_base_store import KnowledgeBaseStore
    from .persona_binding_store import PersonaBindingStore
    from .persona_candidate_store import PersonaCandidateStore
    from .persona_store import PersonaStore
    from .resource_share_store import ResourceShareStore
    from .survey_store import SurveyStore
    from .survey_template_store import SurveyTemplateStore
    from .uploaded_file_store import UploadedFileStore
    from .discussion_store import DiscussionStore
    from .interview_session_store import InterviewSessionStore
    from .user_directory_service import UserDirectoryService
    from .data_agent_service import DataAgentService
    from .dataset_analysis.service import DatasetAnalysisService
    from .memory.memory_service import MemoryService
    from .s3_service import S3Service
    from .survey_batch_service import SurveyBatchService


class ServiceFactory:
    """
    サービスインスタンスを管理するシングルトンファクトリー
    アプリケーション全体でサービスの再利用を実現
    """

    _instance: Optional["ServiceFactory"] = None
    _lock = Lock()

    def __new__(cls) -> "ServiceFactory":
        """シングルトンパターンの実装"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初期化（一度だけ実行される）"""
        if hasattr(self, "_initialized"):
            return

        self.logger = logging.getLogger(__name__)
        self._ai_service: Optional[AIService] = None
        self._agent_service: Optional[AgentService] = None
        self._database_service: Optional[DatabaseService] = None
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
        self._persona_store: Optional["PersonaStore"] = None
        self._knowledge_base_store: Optional["KnowledgeBaseStore"] = None
        self._uploaded_file_store: Optional["UploadedFileStore"] = None
        self._discussion_store: Optional["DiscussionStore"] = None
        self._interview_session_store: Optional["InterviewSessionStore"] = None
        self._custom_persona_data_store: Optional["CustomPersonaDataStore"] = None
        self._initialized = True

        self.logger.info("ServiceFactory initialized")

    def get_ai_service(self) -> AIService:
        """
        AIServiceのシングルトンインスタンスを取得

        Returns:
            AIService: AIサービスインスタンス
        """
        if self._ai_service is None:
            with self._lock:
                if self._ai_service is None:
                    self.logger.info("Creating new AIService instance")
                    self._ai_service = AIService()
        return self._ai_service

    def get_agent_service(self) -> AgentService:
        """
        AgentServiceのシングルトンインスタンスを取得

        Returns:
            AgentService: エージェントサービスインスタンス
        """
        if self._agent_service is None:
            with self._lock:
                if self._agent_service is None:
                    self.logger.info("Creating new AgentService instance")
                    self._agent_service = AgentService()
        return self._agent_service

    def get_database_service(self) -> DatabaseService:
        """
        DatabaseServiceのシングルトンインスタンスを取得

        Returns:
            DatabaseService: データベースサービスインスタンス
        """
        if self._database_service is None:
            with self._lock:
                if self._database_service is None:
                    self.logger.info(
                        f"Creating new DatabaseService instance "
                        f"(region: {config.DYNAMODB_REGION}, prefix: {config.DYNAMODB_TABLE_PREFIX})"
                    )
                    self._database_service = DatabaseService(
                        table_prefix=config.DYNAMODB_TABLE_PREFIX,
                        region=config.DYNAMODB_REGION,
                        # CDK が渡したテーブルごとの物理名（なければ接頭辞から組み立てる）
                        table_names=config.table_names(),
                    )
        return self._database_service

    def is_memory_enabled(self) -> bool:
        """
        長期記憶機能が有効かどうかを確認

        Returns:
            bool: 長期記憶が有効な場合True
        """
        from .memory.session_manager_factory import is_memory_enabled

        return is_memory_enabled()

    def get_memory_service(self) -> Optional["MemoryService"]:
        """
        MemoryServiceのシングルトンインスタンスを取得

        UIからの記憶管理（一覧表示、削除など）に使用。
        エージェントのメモリ統合にはAgentCoreMemorySessionManagerを使用。

        ENABLE_LONG_TERM_MEMORYがFalseの場合、またはAGENTCORE_MEMORY_IDが
        設定されていない場合はNoneを返す。

        Returns:
            MemoryService or None: メモリサービスインスタンス、
                                   または機能が無効の場合はNone
        """
        # 機能が無効の場合はNoneを返す
        if not config.ENABLE_LONG_TERM_MEMORY:
            self.logger.debug("Long-term memory is disabled by configuration")
            return None

        # AGENTCORE_MEMORY_IDが設定されていない場合はNoneを返す
        if not config.AGENTCORE_MEMORY_ID:
            self.logger.warning(
                "ENABLE_LONG_TERM_MEMORY is True but AGENTCORE_MEMORY_ID is not set"
            )
            return None

        # 既に作成を試みて失敗している場合はNoneを返す
        if self._memory_service_attempted and self._memory_service is None:
            return None

        if self._memory_service is None:
            with self._lock:
                if self._memory_service is None and not self._memory_service_attempted:
                    self._memory_service_attempted = True
                    try:
                        # 遅延インポートで循環参照を回避
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
                        # 失敗してもアプリケーションは継続可能
                        # Noneを返すことで呼び出し側が適切に処理できる
                        self._memory_service = None

        return self._memory_service

    def resolve_memory_strategies(self) -> None:
        """起動時に AgentCore Memory の Strategy ID を GetMemory で引き、config に入れる。

        環境変数で渡されていればそれを使う。引けなければ長期記憶を無効にする。
        """
        from .memory.strategy_lookup import resolve_memory_strategies

        resolve_memory_strategies(config)

    def check_stm_session_exists(self, session_id: str) -> bool:
        """AgentCore Memory STMにセッション履歴が存在するか確認する。

        SDK呼び出しをService層に閉じ込める。
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
        """S3Serviceのシングルトンインスタンスを取得。S3_BUCKET_NAME未設定時はRuntimeError。"""
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
        """The S3 service, or ``None`` when ``S3_BUCKET_NAME`` is not set (U5)."""
        if not config.S3_BUCKET_NAME:
            return None
        return self.get_s3_service()

    def get_survey_batch_service(self) -> "SurveyBatchService":
        """SurveyBatchServiceのシングルトンインスタンスを取得"""
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
        """DatasetAnalysisServiceのシングルトンインスタンスを取得。

        analyze_dataset ツール（DuckDB backend）の組み立てを担う。
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

        ``local`` keeps sessions in the process (BR1.15); ``oidc`` uses the
        DynamoDB Sessions table so every task reads the same session (BR1.18).

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
            from .memory_session_store import MemorySessionStore

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
        """Return the Cognito admin API client (AUTH_MODE=admin_api only, U8).

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

        Its DynamoDB client has the short limits of ``reliability-design.md``
        and is separate from :meth:`get_database_service`'s client.
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
        """Return the store of department shares (U4, ADR-003).

        The body tables it also writes are passed per call from
        ``ResourceSharing``'s registration, so only the ResourceShares table
        name is configured here.
        """
        if self._resource_share_store is None:
            with self._lock:
                if self._resource_share_store is None:
                    from .resource_share_store import ResourceShareStore

                    self.logger.info("Creating new ResourceShareStore instance")
                    self._resource_share_store = ResourceShareStore(
                        region=config.DYNAMODB_REGION,
                        shares_table=config.RESOURCE_SHARES_TABLE_NAME,
                    )
        return self._resource_share_store

    def get_persona_candidate_store(self) -> "PersonaCandidateStore":
        """Return the store of generated personas waiting to be saved (U5)."""
        if self._persona_candidate_store is None:
            with self._lock:
                if self._persona_candidate_store is None:
                    from .persona_candidate_store import PersonaCandidateStore

                    self.logger.info("Creating new PersonaCandidateStore instance")
                    self._persona_candidate_store = PersonaCandidateStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.PERSONA_CANDIDATES_TABLE_NAME,
                    )
        return self._persona_candidate_store

    def get_persona_binding_store(self) -> "PersonaBindingStore":
        """Return the store of persona bindings (U5, ADR-007)."""
        if self._persona_binding_store is None:
            with self._lock:
                if self._persona_binding_store is None:
                    from .persona_binding_store import PersonaBindingStore

                    self.logger.info("Creating new PersonaBindingStore instance")
                    self._persona_binding_store = PersonaBindingStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.PERSONA_BINDINGS_TABLE_NAME,
                    )
        return self._persona_binding_store

    def get_job_store(self) -> "JobStore":
        """Return the writer of job records (contract C14)."""
        if self._job_store is None:
            with self._lock:
                if self._job_store is None:
                    from .job_store import JobStore

                    self.logger.info("Creating new JobStore instance")
                    self._job_store = JobStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Jobs"),
                    )
        return self._job_store

    def get_dataset_store(self) -> "DatasetStore":
        """Return the store of registered datasets (U5, U7)."""
        if self._dataset_store is None:
            with self._lock:
                if self._dataset_store is None:
                    from .dataset_store import DatasetStore

                    self.logger.info("Creating new DatasetStore instance")
                    self._dataset_store = DatasetStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Datasets"),
                    )
        return self._dataset_store

    def get_persona_store(self) -> "PersonaStore":
        """Return the store of persona bodies (U4, U5)."""
        if self._persona_store is None:
            with self._lock:
                if self._persona_store is None:
                    from .persona_store import PersonaStore

                    self.logger.info("Creating new PersonaStore instance")
                    self._persona_store = PersonaStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Personas"),
                    )
        return self._persona_store

    def get_survey_store(self) -> "SurveyStore":
        """Return the store of survey bodies (U7)."""
        if self._survey_store is None:
            with self._lock:
                if self._survey_store is None:
                    from .survey_store import SurveyStore

                    self.logger.info("Creating new SurveyStore instance")
                    self._survey_store = SurveyStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("Surveys"),
                    )
        return self._survey_store

    def get_survey_template_store(self) -> "SurveyTemplateStore":
        """Return the store of survey template bodies (U7)."""
        if self._survey_template_store is None:
            with self._lock:
                if self._survey_template_store is None:
                    from .survey_template_store import SurveyTemplateStore

                    self.logger.info("Creating new SurveyTemplateStore instance")
                    self._survey_template_store = SurveyTemplateStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("SurveyTemplates"),
                    )
        return self._survey_template_store

    def get_knowledge_base_store(self) -> "KnowledgeBaseStore":
        """Return the store of registered knowledge bases (U7)."""
        if self._knowledge_base_store is None:
            with self._lock:
                if self._knowledge_base_store is None:
                    from .knowledge_base_store import KnowledgeBaseStore

                    self.logger.info("Creating new KnowledgeBaseStore instance")
                    self._knowledge_base_store = KnowledgeBaseStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("KnowledgeBases"),
                    )
        return self._knowledge_base_store

    def get_custom_persona_data_store(self) -> "CustomPersonaDataStore":
        """Return the store of custom persona data records (U7)."""
        if self._custom_persona_data_store is None:
            with self._lock:
                if self._custom_persona_data_store is None:
                    from .custom_persona_data_store import CustomPersonaDataStore

                    self.logger.info("Creating new CustomPersonaDataStore instance")
                    self._custom_persona_data_store = CustomPersonaDataStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.CUSTOM_PERSONA_DATA_TABLE_NAME,
                    )
        return self._custom_persona_data_store

    def get_uploaded_file_store(self) -> "UploadedFileStore":
        """Return the store of upload metadata (U5)."""
        if self._uploaded_file_store is None:
            with self._lock:
                if self._uploaded_file_store is None:
                    from .uploaded_file_store import UploadedFileStore

                    self.logger.info("Creating new UploadedFileStore instance")
                    self._uploaded_file_store = UploadedFileStore(
                        region=config.DYNAMODB_REGION,
                        table_name=config.table_name("UploadedFiles"),
                    )
        return self._uploaded_file_store

    def get_discussion_store(self) -> "DiscussionStore":
        """Return the store of discussion bodies, messages and reports (U6)."""
        if self._discussion_store is None:
            with self._lock:
                if self._discussion_store is None:
                    from .discussion_store import DiscussionStore

                    self.logger.info("Creating new DiscussionStore instance")
                    self._discussion_store = DiscussionStore(
                        region=config.DYNAMODB_REGION,
                        discussions_table=config.table_name("Discussions"),
                        messages_table=config.DISCUSSION_MESSAGES_TABLE_NAME,
                        reports_table=config.DISCUSSION_REPORTS_TABLE_NAME,
                    )
        return self._discussion_store

    def get_interview_session_store(self) -> "InterviewSessionStore":
        """Return the store of running interviews (U6, ADR-004)."""
        if self._interview_session_store is None:
            with self._lock:
                if self._interview_session_store is None:
                    from .interview_session_store import InterviewSessionStore

                    self.logger.info("Creating new InterviewSessionStore instance")
                    self._interview_session_store = InterviewSessionStore(
                        region=config.DYNAMODB_REGION,
                        sessions_table=config.INTERVIEW_SESSIONS_TABLE_NAME,
                        messages_table=config.INTERVIEW_MESSAGES_TABLE_NAME,
                    )
        return self._interview_session_store

    def get_user_directory_service(self) -> "UserDirectoryService":
        """Return the user-list reader for owner names (U4, BR5.1).

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
        """Return where the departments come from (U4).

        The Cognito ``group:`` groups when a user pool is configured; under
        ``AUTH_MODE=local`` without one, the development user's groups.

        Raises:
            CodedError: ``AUTH_CONFIG_INVALID`` outside local mode without a pool.
        """
        if config.COGNITO_USER_POOL_ID or config.AUTH_MODE != "local":
            return self.get_cognito_groups_service()
        return self._local_directory()

    def get_user_source(self) -> Any:
        """Return where the owner names come from (U4).

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
        DataAgentServiceのインスタンスを取得

        Returns:
            DataAgentService: データ分析エージェントサービス（設定なしの場合はNone）
        """
        if not config.DATA_AGENT_RUNTIME_ARN:
            return None
        from .data_agent_service import DataAgentService

        return DataAgentService(config.DATA_AGENT_RUNTIME_ARN, config.DATA_AGENT_REGION)


# グローバルファクトリーインスタンス
service_factory = ServiceFactory()
