"""
Knowledge Base Tools for Persona Agent
ペルソナエージェントがBedrock Knowledge Baseを検索するためのStrands SDKツール
"""

import logging
from typing import Dict, Callable

import boto3
from botocore.exceptions import ClientError

try:
    from strands import tool
except ImportError:

    def tool(func: Callable) -> Callable:  # type: ignore[no-redef]
        return func


logger = logging.getLogger(__name__)


def _build_metadata_filter(metadata_filters: Dict[str, str]) -> dict:
    """メタデータフィルタをBedrock KB Retrieve API形式に変換"""
    if not metadata_filters:
        return {}

    filter_conditions = [
        {"equals": {"key": k, "value": v}} for k, v in metadata_filters.items()
    ]

    if len(filter_conditions) == 1:
        return {"filter": filter_conditions[0]}

    return {"filter": {"andAll": filter_conditions}}


def create_kb_retrieval_tool(
    knowledge_base_id: str,
    metadata_filters: Dict[str, str] | None = None,
    region: str = "us-east-1",
) -> Callable:
    """
    ペルソナ用のナレッジベース検索ツールを作成

    Args:
        knowledge_base_id: Bedrock Knowledge Base ID
        metadata_filters: メタデータフィルタ（キー=値ペア）
        region: AWSリージョン

    Returns:
        Callable: Strands SDKツールとして使用可能な関数
    """
    client = boto3.client("bedrock-agent-runtime", region_name=region)
    filter_config = _build_metadata_filter(metadata_filters or {})

    @tool
    def search_knowledge_base(query: str, max_results: int = 5) -> str:
        """
        ナレッジベースから関連情報を検索します。
        議論トピックに関連する情報や、自分の知識を補完する情報を検索する際に使用してください。

        Args:
            query: 検索したい内容やキーワード
            max_results: 取得する最大件数（デフォルト: 5）

        Returns:
            検索結果のテキスト
        """
        try:
            if not query or not query.strip():
                return "検索クエリが空です。検索したい内容を指定してください。"

            max_results = max(1, min(10, max_results))

            # Retrieve APIリクエストを構築
            request_params = {
                "knowledgeBaseId": knowledge_base_id,
                "retrievalQuery": {"text": query.strip()},
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": max_results,
                        **filter_config,
                    }
                },
            }

            response = client.retrieve(**request_params)

            results = response.get("retrievalResults", [])
            if not results:
                return "関連する情報は見つかりませんでした。"

            # 結果をフォーマット
            output = "ナレッジベースからの検索結果:\n"
            for i, result in enumerate(results, 1):
                content = result.get("content", {})
                text = content.get("text", "")
                score = result.get("score", 0)

                if text:
                    output += f"\n{i}. {text}\n"
                    if score:
                        output += f"   (関連度: {score:.2f})\n"

            logger.info(
                "KB retrieval returned %d results for kb=%s, query='%s'",
                len(results),
                knowledge_base_id,
                query[:50],
            )
            return output

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(
                "KB retrieval ClientError: kb=%s, code=%s, error=%s",
                knowledge_base_id,
                error_code,
                str(e),
            )
            if error_code == "ResourceNotFoundException":
                return "ナレッジベースが見つかりません。設定を確認してください。"
            if error_code == "AccessDeniedException":
                return "ナレッジベースへのアクセスが拒否されました。"
            return "ナレッジベースの検索中にエラーが発生しました。検索なしで続行します。"

        except Exception as e:
            logger.error(
                "KB retrieval unexpected error: kb=%s, error=%s",
                knowledge_base_id,
                e,
                exc_info=True,
            )
            return "ナレッジベースの検索中にエラーが発生しました。検索なしで続行します。"

    return search_knowledge_base
