"""DatabaseService のアイコン画像パス更新（条件付き書き込み）と削除のテスト

条件式の評価そのものを確かめるため、moto の DynamoDB を使う。
"""

from datetime import datetime

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.persona import Persona  # noqa: E402
from src.services.database_service import DatabaseService  # noqa: E402

REGION = "us-east-1"
OLD_PATH = "s3://bucket/persona_avatars/p/old.webp"
NEW_PATH = "s3://bucket/persona_avatars/p/new.webp"


@pytest.fixture
def service(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        svc = DatabaseService(table_prefix="AvatarTest", region=REGION)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=svc.personas_table,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield svc


def _saved(service: DatabaseService, avatar_path: str | None = None) -> Persona:
    persona = Persona.create_new(
        name="n",
        age=30,
        occupation="o",
        background="b",
        values=["v"],
        pain_points=["p"],
        goals=["g"],
    ).with_avatar(avatar_path)
    service.save_persona(persona)
    return persona


class TestUpdatePersonaAvatar:
    def test_sets_path_when_unchanged(self, service):
        persona = _saved(service)

        assert service.update_persona_avatar(
            persona.id, NEW_PATH, None, datetime(2030, 1, 1)
        )

        stored = service.get_persona(persona.id)
        assert stored.avatar_path == NEW_PATH
        assert stored.updated_at == datetime(2030, 1, 1)

    def test_replaces_expected_path(self, service):
        persona = _saved(service, OLD_PATH)

        assert service.update_persona_avatar(
            persona.id, NEW_PATH, OLD_PATH, datetime.now()
        )
        assert service.get_persona(persona.id).avatar_path == NEW_PATH

    def test_removes_path(self, service):
        persona = _saved(service, OLD_PATH)

        assert service.update_persona_avatar(persona.id, None, OLD_PATH, datetime.now())
        assert service.get_persona(persona.id).avatar_path is None

    def test_does_not_recreate_deleted_persona(self, service):
        persona = _saved(service)
        service.delete_persona_returning_old(persona.id)

        assert not service.update_persona_avatar(
            persona.id, NEW_PATH, None, datetime.now()
        )
        assert service.get_persona(persona.id) is None

    def test_rejects_when_path_changed_concurrently(self, service):
        """同時アップロード: 先に書いた側の画像が上書きされないこと"""
        persona = _saved(service)
        assert service.update_persona_avatar(persona.id, OLD_PATH, None, datetime.now())

        assert not service.update_persona_avatar(
            persona.id, NEW_PATH, None, datetime.now()
        )
        assert service.get_persona(persona.id).avatar_path == OLD_PATH

    def test_keeps_other_fields_edited_concurrently(self, service):
        persona = _saved(service)
        service.update_persona(persona.update(name="編集後の名前"))

        assert service.update_persona_avatar(persona.id, NEW_PATH, None, datetime.now())

        stored = service.get_persona(persona.id)
        assert stored.name == "編集後の名前"
        assert stored.avatar_path == NEW_PATH


class TestDeletePersonaReturningOld:
    def test_returns_deleted_item(self, service):
        persona = _saved(service, OLD_PATH)

        deleted = service.delete_persona_returning_old(persona.id)

        assert deleted is not None
        assert deleted.avatar_path == OLD_PATH
        assert service.get_persona(persona.id) is None

    def test_returns_none_when_absent(self, service):
        assert service.delete_persona_returning_old("missing") is None
