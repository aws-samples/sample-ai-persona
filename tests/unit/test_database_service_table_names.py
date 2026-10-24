"""DatabaseService のテーブル名の受け渡し（v1.0.0 U1、FR9.2、約束 C10）。"""

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

from src.services.database_service import DatabaseService  # noqa: E402

REGION = "ap-northeast-1"


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        yield


def test_prefix_builds_every_name_when_no_names_are_given():
    service = DatabaseService(table_prefix="Local", region=REGION)
    assert service.personas_table == "Local_Personas"
    assert service.surveys_table == "Local_Surveys"


def test_given_physical_names_are_used():
    service = DatabaseService(
        table_prefix="Local",
        region=REGION,
        table_names={
            "Personas": "AIPersonaDev-Data-Personas1",
            "KnowledgeBases": "AIPersonaDev-Data-Kb2",
        },
    )
    assert service.personas_table == "AIPersonaDev-Data-Personas1"
    assert service.kb_table == "AIPersonaDev-Data-Kb2"
    # 渡されなかったものは接頭辞から組み立てる
    assert service.surveys_table == "Local_Surveys"


def test_factory_passes_the_configured_names(monkeypatch):
    from src.config import config
    from src.services import service_factory as factory_module

    captured = {}

    class _Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    factory = factory_module.ServiceFactory()
    monkeypatch.setattr(factory, "_database_service", None)
    monkeypatch.setattr(factory_module, "DatabaseService", _Spy)
    monkeypatch.setattr(config, "PERSONAS_TABLE_NAME", "AIPersonaDev-Data-Personas1")

    factory.get_database_service()

    assert captured["table_names"]["Personas"] == "AIPersonaDev-Data-Personas1"
    assert len(captured["table_names"]) == 8
