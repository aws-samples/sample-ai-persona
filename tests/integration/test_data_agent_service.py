"""DataAgentService の統合テスト"""

import json
import queue

import pytest
from unittest.mock import Mock, patch, MagicMock

from src.services.data_agent_service import (
    DataAgentResult,
    DataAgentService,
    DataAgentServiceError,
    create_data_agent_tool,
)


@pytest.mark.integration
class TestDataAgentResult:
    """DataAgentResult データクラスのテスト"""

    def test_create_with_text_only(self):
        result = DataAgentResult(text="分析結果テキスト")
        assert result.text == "分析結果テキスト"
        assert result.csv_urls == []

    def test_create_with_csv_urls(self):
        result = DataAgentResult(
            text="分析結果", csv_urls=["https://bucket.s3.amazonaws.com/export.csv"]
        )
        assert result.text == "分析結果"
        assert len(result.csv_urls) == 1


@pytest.mark.integration
class TestDataAgentServiceQuery:
    """DataAgentService.query() のテスト"""

    def _make_stream_lines(self, events: list[dict]) -> list[bytes]:
        lines = []
        for evt in events:
            lines.append(f"data: {json.dumps(evt, ensure_ascii=False)}".encode())
        return lines

    @patch("src.services.data_agent_service.boto3")
    def test_query_returns_data_agent_result(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        events = [
            {"type": "token", "content": "結果"},
            {"type": "token", "content": "テキスト"},
            {"type": "done"},
        ]
        mock_response = MagicMock()
        mock_response.__getitem__ = lambda self, key: (
            MagicMock(iter_lines=lambda: iter(self._make_lines(events)))
            if key == "response"
            else None
        )

        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(self._make_stream_lines(events))
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        service = DataAgentService(runtime_arn="arn:aws:test", region="us-east-1")
        result = service.query("テスト質問")

        assert isinstance(result, DataAgentResult)
        assert result.text == "結果テキスト"
        assert result.csv_urls == []

    @patch("src.services.data_agent_service.boto3")
    def test_query_captures_csv_urls(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        csv_url = "https://bucket.s3.amazonaws.com/exports/data.csv?X-Amz-Signature=abc"
        events = [
            {"type": "token", "content": "分析完了。"},
            {"type": "csv_url", "url": csv_url},
            {"type": "done"},
        ]
        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(self._make_stream_lines(events))
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        service = DataAgentService(runtime_arn="arn:aws:test", region="us-east-1")
        result = service.query("CSVエクスポートして")

        assert result.text == "分析完了。"
        assert result.csv_urls == [csv_url]

    @patch("src.services.data_agent_service.boto3")
    def test_query_multiple_csv_urls(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        events = [
            {"type": "token", "content": "結果"},
            {"type": "csv_url", "url": "https://bucket.s3.amazonaws.com/a.csv"},
            {"type": "csv_url", "url": "https://bucket.s3.amazonaws.com/b.csv"},
            {"type": "done"},
        ]
        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(self._make_stream_lines(events))
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        service = DataAgentService(runtime_arn="arn:aws:test", region="us-east-1")
        result = service.query("2つのCSVをエクスポート")

        assert len(result.csv_urls) == 2

    @patch("src.services.data_agent_service.boto3")
    def test_query_ignores_empty_csv_url(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        events = [
            {"type": "token", "content": "結果"},
            {"type": "csv_url", "url": ""},
            {"type": "done"},
        ]
        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(self._make_stream_lines(events))
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        service = DataAgentService(runtime_arn="arn:aws:test", region="us-east-1")
        result = service.query("テスト")

        assert result.csv_urls == []

    @patch("src.services.data_agent_service.boto3")
    def test_query_raises_on_error_event(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        events = [
            {"type": "error", "content": "SQL実行エラー"},
        ]
        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(self._make_stream_lines(events))
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        service = DataAgentService(runtime_arn="arn:aws:test", region="us-east-1")
        with pytest.raises(DataAgentServiceError, match="SQL実行エラー"):
            service.query("エラーになる質問")


@pytest.mark.integration
class TestCreateDataAgentTool:
    """create_data_agent_tool のテスト"""

    @patch("src.services.data_agent_service.boto3")
    def test_csv_url_sent_to_event_queue(self, mock_boto3):
        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        csv_url = "https://bucket.s3.amazonaws.com/export.csv"
        events = [
            {"type": "token", "content": "完了"},
            {"type": "csv_url", "url": csv_url},
            {"type": "done"},
        ]
        lines = [f"data: {json.dumps(e, ensure_ascii=False)}".encode() for e in events]
        resp_mock = MagicMock()
        resp_mock.iter_lines.return_value = iter(lines)
        mock_client.invoke_agent_runtime.return_value = {"response": resp_mock}

        eq: queue.Queue = queue.Queue()
        tool_fn = create_data_agent_tool("arn:aws:test", "us-east-1", event_queue=eq)

        result = tool_fn(question="CSVエクスポート")

        assert "完了" in result
        assert csv_url in result

        events_received = []
        while not eq.empty():
            events_received.append(eq.get_nowait())

        types = [e["type"] for e in events_received]
        assert "tool_call" in types
        assert "tool_result" in types
        assert "csv_url" in types

        csv_evt = next(e for e in events_received if e["type"] == "csv_url")
        assert csv_evt["url"] == csv_url
