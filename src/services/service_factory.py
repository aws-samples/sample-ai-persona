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

if TYPE_CHECKING:
    from .data_agent_service import DataAgentService
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
            self.logger.debug(f"STMセッション確認失敗: {e}")
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
