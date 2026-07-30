"""エラーコードを検証するテストヘルパー（Issue #112）。

例外メッセージは診断情報（技術的事実）に限られ、ユーザー向け文言は
`web/error_messages.py` のカタログが持つ。したがってテストは
`pytest.raises(..., match="日本語")` ではなくエラーコードを検証する。
"""

from contextlib import contextmanager
from typing import Any, Generator

import pytest

from src.models.errors import CodedError, ErrorCode


@contextmanager
def raises_code(
    exc_type: type[CodedError], code: ErrorCode, **context: Any
) -> Generator[Any]:
    """`exc_type` が送出され、そのコード（と任意のcontext値）が一致することを検証する。

    Args:
        exc_type: 期待する例外型。
        code: 期待する ErrorCode。
        **context: 検証したい `CodedError.context` のキーと値。
    """
    with pytest.raises(exc_type) as exc_info:
        yield exc_info

    actual = exc_info.value.code
    assert actual is code, f"expected {code.name}, got {actual.name}"
    for key, expected in context.items():
        assert exc_info.value.context[key] == expected, (
            f"context[{key!r}]: expected {expected!r}, "
            f"got {exc_info.value.context.get(key)!r}"
        )
