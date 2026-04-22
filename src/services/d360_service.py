"""
D360 DWH Agent 連携サービス

AgentCore Runtime 上の D360 データ分析エージェントに問い合わせ、
Strands @tool としてペルソナ生成エージェントに提供する。
"""

import json
import logging
import uuid
from datetime import datetime

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)


class D360ServiceError(Exception):
    """D360 サービスのエラー"""


class D360Service:
    """AgentCore Runtime 上の D360 DWH Agent への問い合わせサービス"""

    def __init__(self, runtime_arn: str, region: str) -> None:
        self._runtime_arn = runtime_arn
        self._client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=BotoConfig(read_timeout=600, connect_timeout=120),
        )

    def query(self, question: str) -> str:
        """D360 に自然言語で問い合わせ、回答テキストを返す。

        Args:
            question: データに関する質問

        Returns:
            分析結果テキスト

        Raises:
            D360ServiceError: 問い合わせ失敗時
        """
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        session_id = f"persona-d360-{ts}-{uuid.uuid4().hex[:8]}"

        payload = json.dumps({
            "prompt": question,
            "user_id": "persona-system",
        }).encode()

        logger.info("D360 問い合わせ開始: %s", question[:100])

        try:
            resp = self._client.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=session_id,
                payload=payload,
            )

            chunks: list[str] = []
            for line in resp["response"].iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8")
                if not s.startswith("data: "):
                    continue
                evt = json.loads(s[6:])
                if evt.get("type") == "token":
                    chunks.append(evt["content"])
                elif evt.get("type") == "error":
                    raise D360ServiceError(f"D360 エラー: {evt.get('content', '')}")
                elif evt.get("type") == "done":
                    break

            result = "".join(chunks)
            if not result:
                raise D360ServiceError("D360 から回答を取得できませんでした")

            logger.info("D360 問い合わせ完了 (%d chars)", len(result))
            return result

        except D360ServiceError:
            raise
        except Exception as e:
            raise D360ServiceError(f"D360 問い合わせ失敗: {e}") from e


def create_d360_tool(runtime_arn: str, region: str, event_queue=None):
    """D360 問い合わせを Strands @tool としてラップして返す。

    Args:
        runtime_arn: AgentCore Runtime ARN
        region: AWS リージョン
        event_queue: リアルタイムイベント用 queue（任意）

    Returns:
        Strands tool 関数
    """
    from strands import tool

    service = D360Service(runtime_arn, region)

    @tool
    def ask_data_agent(question: str) -> str:
        """社内データウェアハウス（DWH）に自然言語で問い合わせる。
        売上・注文・顧客・商品データの分析、集計、傾向把握ができる。
        集計クエリを依頼すると効率的に情報を得られる。

        Args:
            question: データに関する質問。例: "先月の売上トップ10商品は？", "顧客の年代別購買金額の分布を教えて"
        """
        result = service.query(question)
        if event_queue is not None:
            # 結果のプレビュー（先頭300文字）を流す
            preview = result[:300] + ("..." if len(result) > 300 else "")
            event_queue.put({"type": "tool_result", "content": preview})
        return result

    return ask_data_agent
