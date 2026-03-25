"""
MCP Server Manager - MotherDuck MCPサーバーのライフサイクル管理
"""

import logging
import threading
from typing import Optional, Any, List

logger = logging.getLogger(__name__)

# グローバルシングルトン
_mcp_manager: Optional["MCPServerManager"] = None


def get_mcp_manager() -> "MCPServerManager":
    """MCPServerManagerのシングルトンインスタンスを取得"""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPServerManager()
    return _mcp_manager


class MCPServerManager:
    """MotherDuck MCPサーバー管理"""

    def __init__(self):
        self._enabled = False
        self._mcp_client: Optional[Any] = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> bool:
        """MCPサーバーを起動（MCPClientを初期化してセッションを開始）"""
        with self._lock:
            if self._enabled:
                return True

            try:
                import os
                from mcp import stdio_client, StdioServerParameters
                from strands.tools.mcp import MCPClient

                # AWS認証情報を環境変数から取得してMCPサーバーに渡す
                env = os.environ.copy()

                self._mcp_client = MCPClient(
                    lambda: stdio_client(
                        StdioServerParameters(
                            command="uvx",
                            args=["mcp-server-motherduck", "--read-write"],
                            env=env,
                        )
                    )
                )
                # セッションを開始して維持
                self._mcp_client.start()
                self._enabled = True
                logger.info("MCP Server started successfully")
                return True

            except ImportError as e:
                logger.error(f"MCP dependencies not installed: {e}")
                return False
            except Exception as e:
                logger.error(f"Failed to start MCP server: {e}")
                return False

    def stop(self) -> bool:
        """MCPサーバーを停止"""
        with self._lock:
            if not self._enabled:
                return True

            try:
                if self._mcp_client:
                    # MCPClient.stop()は__exit__として設計されているため引数が必要
                    self._mcp_client.stop(None, None, None)
                    self._mcp_client = None
                self._enabled = False
                logger.info("MCP Server stopped")
                return True
            except Exception as e:
                logger.error(f"Failed to stop MCP server: {e}")
                return False

    def is_running(self) -> bool:
        """MCPサーバーが起動中かどうか"""
        return self._enabled

    def get_mcp_client(self) -> Optional[Any]:
        """MCPClientインスタンスを取得（起動中のみ）"""
        if not self._enabled:
            return None
        return self._mcp_client

    def get_tools(self) -> List[Any]:
        """MCPツールを取得（セッションが開いている状態で）"""
        if not self._enabled or not self._mcp_client:
            return []
        try:
            return list(self._mcp_client.list_tools_sync())
        except Exception as e:
            logger.error(f"Failed to get MCP tools: {e}")
            return []

    def toggle(self, enable: bool) -> bool:
        """有効/無効を切り替え"""
        if enable:
            return self.start()
        else:
            return self.stop()
