"""
Service Factory
アプリケーション全体でサービスインスタンスを管理するファクトリークラス
"""

import logging
from typing import Optional, TYPE_CHECKING
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
    from .oidc_service import OidcService
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
        self._authz_lookup_service: Optional["AuthzLookupService"] = None
        self._cognito_groups_service: Optional["CognitoGroupsService"] = None
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
        from .oidc_service import OidcService

        self.logger.info("Creating new OidcService instance")
        return OidcService(
            user_pool_id=config.COGNITO_USER_POOL_ID or "",
            client_id=config.COGNITO_CLIENT_ID or "",
            client_secret=config.COGNITO_CLIENT_SECRET or "",
            domain=config.COGNITO_DOMAIN or "",
            redirect_uri=config.APP_BASE_URL.rstrip("/") + "/auth/callback",
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
