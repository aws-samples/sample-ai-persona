"""Test helper for asserting error codes (Issue #112).

Exception messages carry diagnostic facts only; user-facing text lives in the
`web/error_messages.py` catalog. Tests therefore assert the error code instead
of `pytest.raises(..., match="...")` on Japanese text.
"""

from contextlib import contextmanager
from typing import Any, Generator

import pytest

from src.models.errors import CodedError, ErrorCode


@contextmanager
def raises_code(
    exc_type: type[CodedError], code: ErrorCode, **context: Any
) -> Generator[Any]:
    """Assert that `exc_type` is raised with the given code (and optional context values).

    Args:
        exc_type: Expected exception type.
        code: Expected ErrorCode.
        **context: Keys and values expected in `CodedError.context`.

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
