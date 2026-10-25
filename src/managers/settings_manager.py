"""
SettingsManager
システム設定（ナレッジベース管理、データエージェント接続設定・接続テスト）を管理する。

v1.0.0 U7 (``functional-design/rules.md``):

- BR3.1 / BR3.3: every method of the settings screen first checks
  ``settings:manage``; others get ``SETTINGS_NOT_FOUND`` (the same 404 as a
  missing page) and nothing is read or changed. ``is_data_agent_available``
  only tells whether the agent is configured.
- BR5.2: the knowledge bases' usage scope changes with ``ADD`` / ``DELETE``;
  only added departments must exist. ``asset.scope_changed`` is logged.

Members see the knowledge bases they may use through ``PersonaManager`` (U5).
"""

import logging
from typing import Any, Dict, Optional

from ..config import config
from ..models.authz import Permission, Principal, ResourceType
from ..models.errors import CodedError, ErrorCode
from ..models.knowledge_base import KnowledgeBase
from ..models.persona_binding import KB_TARGET_KEY
from ..models.sharing import AssetScopeChange, AssetScopeDialog, ScopedAssets
from ..services.stores.knowledge_base_store import KnowledgeBaseStore
from ..services.stores.persona_binding_store import (
    PersonaBindingStore,
    PersonaBindingStoreError,
)
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.department_directory import DepartmentDirectory
from .components.sharing_runtime import sharing_parts_from
from .shared.asset_scope import scope_dialog, scope_labels
from .shared.structured_log import log_event

logger = logging.getLogger(__name__)

_KB = ResourceType.KNOWLEDGE_BASE


class SettingsManagerError(CodedError):
    pass


def _missing_kb() -> SettingsManagerError:
    return SettingsManagerError(
        "no such knowledge base", code=ErrorCode.SETTINGS_NOT_FOUND
    )


class SettingsManager:
    def __init__(
        self,
        knowledge_base_store: Optional[KnowledgeBaseStore] = None,
        departments: Optional[DepartmentDirectory] = None,
        binding_store: Optional[PersonaBindingStore] = None,
    ) -> None:
        """Create the manager.

        Args:
            knowledge_base_store: Knowledge base records (the singleton when omitted).
            departments: The per-task department copy (scope choices, C4).
            binding_store: Persona bindings, removed with a deleted knowledge
                base (the singleton on first use when omitted).
        """
        self._kbs = knowledge_base_store or service_factory.get_knowledge_base_store()
        self._departments = departments
        self._binding_store = binding_store

    @property
    def _bindings(self) -> PersonaBindingStore:
        if self._binding_store is None:
            self._binding_store = service_factory.get_persona_binding_store()
        return self._binding_store

    @property
    def _department_directory(self) -> DepartmentDirectory:
        if self._departments is None:
            self._departments = sharing_parts_from(service_factory).departments
        return self._departments

    @staticmethod
    def _require_manage(principal: Principal) -> None:
        AccessPolicy.require(
            principal,
            Permission.SETTINGS_MANAGE,
            hidden_as=ErrorCode.SETTINGS_NOT_FOUND,
            resource_type=_KB,
        )

    # --- ナレッジベース管理 ---

    def get_all_knowledge_bases(
        self, principal: Principal
    ) -> ScopedAssets[KnowledgeBase]:
        """登録済みの KB と利用範囲（admin だけ）。"""
        self._require_manage(principal)
        kbs = self._kbs.list_all()
        departments = self._department_directory.list_all() if kbs else []
        return ScopedAssets(
            items=kbs,
            scope_labels={
                kb.id: scope_labels(sorted(kb.shared_group_ids), departments)
                for kb in kbs
            },
        )

    def create_knowledge_base(
        self,
        principal: Principal,
        knowledge_base_id: str,
        name: str,
        description: str = "",
    ) -> KnowledgeBase:
        """KB を登録する（利用範囲は admin だけで始まる、BR5.1）。"""
        self._require_manage(principal)
        kb = KnowledgeBase.create_new(
            knowledge_base_id=knowledge_base_id.strip(),
            name=name.strip(),
            description=description.strip(),
        )
        self._kbs.put_new(kb)
        logger.info("Knowledge base registered: %s", kb.id)
        return kb

    def delete_knowledge_base(self, principal: Principal, kb_id: str) -> None:
        """Delete a knowledge base registration and its persona bindings.

        A failed binding delete is logged and the delete still succeeds: the
        bindings left behind have no target and are skipped when read.
        """
        self._require_manage(principal)
        self._kbs.delete(kb_id)
        logger.info("Knowledge base deleted: %s", kb_id)
        try:
            self._bindings.delete_for_target(kb_id, KB_TARGET_KEY)
        except PersonaBindingStoreError as e:
            logger.warning(
                "Failed to delete bindings of knowledge base %s: %s",
                kb_id,
                type(e).__name__,
            )

    def scope_dialog(self, principal: Principal, kb_id: str) -> AssetScopeDialog:
        """What the usage-scope dialog shows (M10).

        Raises:
            CodedError: ``SETTINGS_NOT_FOUND`` (missing knowledge base).
        """
        self._require_manage(principal)
        kb = self._kbs.get(kb_id, consistent=True)
        if kb is None:
            raise _missing_kb()
        return scope_dialog(
            _KB,
            kb.id,
            kb.name,
            kb.shared_group_ids,
            self._department_directory.list_all(),
        )

    def update_scope(
        self, principal: Principal, kb_id: str, change: AssetScopeChange
    ) -> KnowledgeBase:
        """Change the usage scope with ``ADD`` / ``DELETE`` (BR5.2).

        Raises:
            CodedError: ``SETTINGS_NOT_FOUND`` (missing knowledge base) or
                ``SHARE_SCOPE_INVALID`` (an added department does not exist).
        """
        self._require_manage(principal)
        existing = {d.group_id for d in self._department_directory.list_all()}
        if not change.add <= existing:
            raise SettingsManagerError(
                "a department that does not exist was sent",
                code=ErrorCode.SHARE_SCOPE_INVALID,
            )
        if not self._kbs.update_scope(kb_id, add=change.add, remove=change.remove):
            raise _missing_kb()
        log_event(
            "asset.scope_changed",
            resource_type=_KB.value,
            resource_id=kb_id,
            added=sorted(change.add),
            removed=sorted(change.remove),
            actor_sub=principal.sub,
        )
        kb = self._kbs.get(kb_id, consistent=True)
        if kb is None:
            raise _missing_kb()
        return kb

    # --- データ分析エージェント設定 ---

    def get_data_agent_settings(self, principal: Principal) -> Dict[str, Any]:
        """データ分析エージェントの接続設定（admin だけ、BR3.1）。"""
        self._require_manage(principal)
        return {
            "data_agent_enabled": config.ENABLE_DATA_AGENT,
            "data_agent_runtime_arn": config.DATA_AGENT_RUNTIME_ARN,
            "data_agent_region": config.DATA_AGENT_REGION,
        }

    def save_data_agent_settings(
        self,
        principal: Principal,
        runtime_arn: Optional[str],
        region: str,
        enabled: bool,
    ) -> None:
        """データ分析エージェント接続設定を保存する。"""
        self._require_manage(principal)
        config.DATA_AGENT_RUNTIME_ARN = runtime_arn
        config.DATA_AGENT_REGION = region
        config.ENABLE_DATA_AGENT = enabled
        logger.info(
            "データ分析エージェント設定更新: enabled=%s, region=%s", enabled, region
        )

    @staticmethod
    def is_data_agent_available() -> bool:
        """データ分析エージェントが利用可能かどうかを判定する。"""
        return bool(config.ENABLE_DATA_AGENT and config.DATA_AGENT_RUNTIME_ARN)

    def test_data_agent_connection(self, principal: Principal) -> str:
        """データ分析エージェントへの接続テストを実行する。"""
        self._require_manage(principal)
        if not config.DATA_AGENT_RUNTIME_ARN:
            raise SettingsManagerError(
                "runtime ARN is not configured",
                code=ErrorCode.DATA_AGENT_NOT_CONFIGURED,
            )

        try:
            data_agent_service = service_factory.get_data_agent_service()
            if not data_agent_service:
                raise SettingsManagerError(
                    "data agent service could not be initialized",
                    code=ErrorCode.DATA_AGENT_CONNECTION_FAILED,
                )
            result = data_agent_service.query("利用可能なテーブル一覧を教えてください")
            return result.text
        except SettingsManagerError:
            raise
        except Exception as e:
            logger.error("データ分析エージェント接続テストエラー: %s", type(e).__name__)
            raise SettingsManagerError(
                f"data agent connection test failed ({type(e).__name__})",
                code=ErrorCode.DATA_AGENT_CONNECTION_FAILED,
            ) from e
