"""API tests - automatically marked with @pytest.mark.api"""
import pytest
from pathlib import Path

_THIS_DIR = str(Path(__file__).parent)


def pytest_collection_modifyitems(items):
    for item in items:
        if str(item.fspath).startswith(_THIS_DIR):
            item.add_marker(pytest.mark.api)
