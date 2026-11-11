"""KB 検索ツール（kb_tools）が組み立てる Retrieve リクエストのテスト。

通常の KB は vectorSearchConfiguration、Managed KB は managedSearchConfiguration を使う。
boto3 クライアントはモックに差し替える。
"""

from unittest.mock import Mock, patch

import pytest

from src.models.knowledge_base import KB_TYPE_MANAGED, KB_TYPE_VECTOR
from src.services.knowledge_base.kb_tools import create_kb_retrieval_tool


@pytest.fixture
def client() -> Mock:
    mock = Mock()
    mock.retrieve.return_value = {
        "retrievalResults": [{"content": {"text": "本文"}, "score": 0.5}]
    }
    with patch("src.services.knowledge_base.kb_tools.boto3.client", return_value=mock):
        yield mock


def _retrieval_configuration(client: Mock) -> dict:
    return client.retrieve.call_args.kwargs["retrievalConfiguration"]


class TestRetrievalConfiguration:
    def test_vector_kb_uses_vector_search(self, client):
        tool = create_kb_retrieval_tool("KB1", {"a": "1"}, kb_type=KB_TYPE_VECTOR)
        assert "本文" in tool("質問", max_results=3)
        assert _retrieval_configuration(client) == {
            "vectorSearchConfiguration": {
                "numberOfResults": 3,
                "filter": {"equals": {"key": "a", "value": "1"}},
            }
        }

    def test_kb_type_defaults_to_vector_search(self, client):
        create_kb_retrieval_tool("KB1")("質問")
        assert list(_retrieval_configuration(client)) == ["vectorSearchConfiguration"]

    def test_managed_kb_uses_managed_search_with_the_same_filters(self, client):
        tool = create_kb_retrieval_tool(
            "KB1", {"a": "1", "b": "2"}, kb_type=KB_TYPE_MANAGED
        )
        tool("質問", max_results=4)
        assert _retrieval_configuration(client) == {
            "managedSearchConfiguration": {
                "numberOfResults": 4,
                "filter": {
                    "andAll": [
                        {"equals": {"key": "a", "value": "1"}},
                        {"equals": {"key": "b", "value": "2"}},
                    ]
                },
            }
        }

    def test_managed_kb_without_filters_sends_no_filter(self, client):
        create_kb_retrieval_tool("KB1", kb_type=KB_TYPE_MANAGED)("質問")
        assert _retrieval_configuration(client) == {
            "managedSearchConfiguration": {"numberOfResults": 5}
        }

    def test_empty_query_does_not_call_the_api(self, client):
        tool = create_kb_retrieval_tool("KB1", kb_type=KB_TYPE_MANAGED)
        assert "空" in tool("  ")
        client.retrieve.assert_not_called()
