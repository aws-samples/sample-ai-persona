"""Infra tests - automatically marked with @pytest.mark.infra.

Deploy / teardown scripts, the container and local start-up settings, the WAF
paths and the table-creation script. They read or run files outside ``src/`` and
``web/`` and are not run in CI: run them by hand when those files change
(``uv run pytest tests/infra/ -q``).
"""

from pathlib import Path

import pytest

_THIS_DIR = str(Path(__file__).parent)


def pytest_collection_modifyitems(items):
    for item in items:
        if str(item.fspath).startswith(_THIS_DIR):
            item.add_marker(pytest.mark.infra)
