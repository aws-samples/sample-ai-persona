"""API tests - automatically marked with @pytest.mark.api"""

import pytest
from pathlib import Path

# U7: the moto world behind the survey / settings routers (tests/api/u7_world.py)
from tests.unit.survey_world import survey_world  # noqa: F401

_THIS_DIR = str(Path(__file__).parent)


def pytest_collection_modifyitems(items):
    for item in items:
        if str(item.fspath).startswith(_THIS_DIR):
            item.add_marker(pytest.mark.api)
