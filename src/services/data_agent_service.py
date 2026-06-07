"""
データ分析エージェント連携サービス

AgentCore Runtime 上のデータ分析エージェントに問い合わせ、
Strands @tool としてペルソナ生成エージェントに提供する。
"""

import json
import logging
import queue
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)


@dataclass
class DataAgentResult:
    """DataAgent からの応答結果"""

    text: str
    csv_urls: list[str] = field(default_factory=list)


class DataAgentServiceError(Exception):
    """DataAgent サービスのエラー"""


class DataAgentService:
    """AgentCore Runtime 上の DataAgent DWH Agent への問い合わせサービス"""

    def __init__(self, runtime_arn: str, region: str) -> None:
        self._runtime_arn = runtime_arn
        self._client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=BotoConfig(read_timeout=600, connect_timeout=120),
        )

    def query(self, question: str) -> DataAgentResult:
        """DataAgent に自然言語で問い合わせ、回答テキストとCSV URLを返す。

        Args:
            question: データに関する質問

        Returns:
            DataAgentResult（テキスト応答 + CSV URL リスト）

        Raises:
            DataAgentServiceError: 問い合わせ失敗時
        """
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        session_id = f"persona-data_agent-{ts}-{uuid.uuid4().hex[:8]}"

        payload = json.dumps(
            {
                "prompt": question,
                "user_id": "persona-system",
            }
        ).encode()

        logger.info("DataAgent 問い合わせ開始: %s", question[:100])

        try:
            resp = self._client.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=session_id,
                payload=payload,
            )

            chunks: list[str] = []
            csv_urls: list[str] = []
            for line in resp["response"].iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8")
                if not s.startswith("data: "):
                    continue
                evt = json.loads(s[6:])
                if evt.get("type") == "token":
                    chunks.append(evt["content"])
                elif evt.get("type") == "csv_url":
                    url = evt.get("url", "")
                    if url:
                        csv_urls.append(url)
                elif evt.get("type") == "error":
                    raise DataAgentServiceError(
                        f"DataAgent エラー: {evt.get('content', '')}"
                    )
                elif evt.get("type") == "done":
                    break

            result = "".join(chunks)
            if not result:
                raise DataAgentServiceError("DataAgent から回答を取得できませんでした")

            logger.info(
                "DataAgent 問い合わせ完了 (%d chars, %d csv_urls)",
                len(result),
                len(csv_urls),
            )
            return DataAgentResult(text=result, csv_urls=csv_urls)

        except DataAgentServiceError:
            raise
        except Exception as e:
            raise DataAgentServiceError(f"DataAgent 問い合わせ失敗: {e}") from e

    @staticmethod
    def download_csv(url: str) -> bytes:
        """DataAgentが生成した署名付きURLからCSVをダウンロードする。

        URLスキーム・ドメインを検証し、HTTPタイムアウト付きで取得する。

        Raises:
            DataAgentServiceError: URL不正またはダウンロード失敗時
        """
        import urllib.request
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise DataAgentServiceError("不正なURLスキームです")
        if not parsed.hostname or not parsed.hostname.endswith(".amazonaws.com"):
            raise DataAgentServiceError(
                "許可されていないドメインからのダウンロードです"
            )
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return bytes(resp.read())
        except DataAgentServiceError:
            raise
        except Exception as e:
            raise DataAgentServiceError(f"CSVダウンロードに失敗しました: {e}") from e


def create_data_agent_tool(
    runtime_arn: str, region: str, event_queue: "queue.Queue[dict] | None" = None
) -> Any:
    """DataAgent 問い合わせを Strands @tool としてラップして返す。

    Args:
        runtime_arn: AgentCore Runtime ARN
        region: AWS リージョン
        event_queue: リアルタイムイベント用 queue（任意）

    Returns:
        Strands tool 関数
    """
    from strands import tool

    service = DataAgentService(runtime_arn, region)

    @tool
    def ask_data_agent(question: str) -> str:
        """社内データウェアハウス（DWH）に自然言語で問い合わせる。
        利用可能なテーブルやデータの分析、集計、傾向把握ができる。
        まず「利用可能なテーブル一覧」を確認してからデータ構造に合った質問をすると効果的。

        Args:
            question: データに関する質問。例: "利用可能なテーブル一覧を教えて", "顧客の年代別分布を教えて"
        """
        if event_queue is not None:
            event_queue.put(
                {
                    "type": "tool_call",
                    "content": "データ分析エージェントに問い合わせ中...",
                    "detail": question,
                }
            )
        agent_result = service.query(question)
        if event_queue is not None:
            event_queue.put(
                {
                    "type": "tool_result",
                    "tool_name": "ask_data_agent",
                    "content": agent_result.text,
                }
            )
            for url in agent_result.csv_urls:
                event_queue.put({"type": "csv_url", "url": url})
        if agent_result.csv_urls:
            return (
                agent_result.text
                + "\n\nCSVダウンロードURL:\n"
                + "\n".join(agent_result.csv_urls)
            )
        return agent_result.text

    return ask_data_agent
