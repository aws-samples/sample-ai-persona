"""Shared world for the U7 Manager tests (surveys, templates, organization assets).

DynamoDB and S3 are moto; ``DatabaseService``, ``ResourceShareStore``,
``CustomPersonaDataStore``, ``AuthzLookupService`` and ``S3Service`` are the real
services on it. ``AccessPolicy``, ``ResourceSharing``, ``ResourceLookup`` and
``DepartmentDirectory`` are the real components (decisions are never mocked).
``FileAttachments`` and ``OwnerDirectory`` are fakes; AI is mocked by each test.

People (unit-test-instructions.md「テストのデータ」): member A and B in
``sales``, member C in ``plan``, an admin in no department, no role.
"""

from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator
from unittest.mock import Mock

import boto3
import pytest

from src.managers.components.department_directory import DepartmentDirectory
from src.managers.components.resource_lookup import build_resource_lookup
from src.managers.components.resource_sharing import (
    ResourceSharing,
    default_sharing_tables,
)
from src.models.authz import Department, Principal, ResourceType, Role
from src.models.custom_persona_data import CustomPersonaData, ExtraColumn
from src.models.dataset import Dataset, DatasetColumn
from src.models.knowledge_base import KnowledgeBase
from src.models.survey import Survey, TemplateSnapshot
from src.models.survey_template import Question, SurveyTemplate, TemplateImage
from src.models.uploaded_file import AttachResult
from src.services.authz_lookup_service import AuthzLookupService, LookupTable
from src.services.custom_persona_data_store import CustomPersonaDataStore
from src.services.database_service import DatabaseService
from src.services.dataset_store import DatasetStore
from src.services.knowledge_base_store import KnowledgeBaseStore
from src.services.resource_share_store import ResourceShareStore
from src.services.s3_service import S3Service

REGION = "us-east-1"
BUCKET = "u7-test-bucket"
NOW = datetime(2026, 10, 15, 9, 0)

MEMBER_A = Principal(
    sub="a", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
MEMBER_B = Principal(
    sub="b", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"sales"})
)
MEMBER_C = Principal(
    sub="c", roles=frozenset({Role.MEMBER}), group_ids=frozenset({"plan"})
)
ADMIN = Principal(sub="admin", roles=frozenset({Role.ADMIN}))
NO_ROLE = Principal(sub="x", group_ids=frozenset({"sales"}))
NO_DEPARTMENT = Principal(sub="d", roles=frozenset({Role.MEMBER}))

DEPARTMENTS = [Department("sales", "営業部"), Department("plan", "企画部")]

_OWNER_INDEX = {
    "IndexName": "OwnerIndex",
    "KeySchema": [
        {"AttributeName": "owner_sub", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "KEYS_ONLY"},
}


class Departments:
    """``CognitoGroupsService`` stand-in (tests change ``departments``)."""

    def __init__(self) -> None:
        self.departments = list(DEPARTMENTS)

    def list_departments(self) -> list[Department]:
        return list(self.departments)


def make_files() -> Mock:
    files = Mock()
    files.attach_all.side_effect = lambda p, ids, pt, pid: AttachResult(
        parent_type=pt, parent_id=pid, original_expires_at={i: 1 for i in ids}
    )
    files.delete_attached.return_value = []
    return files


class SurveyWorld:
    """Everything a U7 Manager test wires together (inside ``mock_aws``)."""

    def __init__(self) -> None:
        self.db = DatabaseService(table_prefix="U7", region=REGION)
        self.client = self.db.dynamodb_client
        self.kb_table = "U7_KnowledgeBases"
        self.datasets_table = "U7_Datasets"
        self.dataset_store = DatasetStore(
            region=REGION, table_name=self.datasets_table, client=self.client
        )
        self.kb_store = KnowledgeBaseStore(
            region=REGION, table_name=self.kb_table, client=self.client
        )
        self.shares_table = "U7_ResourceShares"
        self.custom_table = "U7_CustomPersonaData"
        self._create_tables()
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        self.s3 = S3Service(bucket_name=BUCKET, region_name=REGION)
        self.share_store = ResourceShareStore(
            region=REGION,
            shares_table=self.shares_table,
            client=self.client,
            sleep=lambda _s: None,
        )
        self.department_source = Departments()
        self.departments = DepartmentDirectory(self.department_source)
        self.sharing = ResourceSharing(
            self.share_store, self.departments, sleep=lambda _s: None
        )
        tables = {
            ResourceType.SURVEY_TEMPLATE: self.db.survey_templates_table,
            ResourceType.SURVEY: self.db.surveys_table,
        }
        for table in default_sharing_tables():
            if table.resource_type in tables:
                self.sharing.register(replace(table, table=tables[table.resource_type]))
        self.lookup_service = AuthzLookupService(
            region=REGION, jobs_table_name="U7_Jobs", client=self.client
        )
        lookup_tables = [
            LookupTable(
                ResourceType.SURVEY_TEMPLATE, self.db.survey_templates_table, "name"
            ),
            LookupTable(ResourceType.SURVEY, self.db.surveys_table, "name"),
            LookupTable(ResourceType.DATASET, self.datasets_table, "name"),
            LookupTable(ResourceType.KNOWLEDGE_BASE, self.kb_table, "name"),
            LookupTable(
                ResourceType.CUSTOM_PERSONA_DATA,
                self.custom_table,
                "name",
                hidden_id_prefix="name#",
            ),
        ]
        self.lookup = build_resource_lookup(
            [(t.resource_type, self.lookup_service.reader(t)) for t in lookup_tables],
            sample_rate=0.0,
        )
        self.custom_store = CustomPersonaDataStore(
            region=REGION, table_name=self.custom_table, client=self.client
        )
        self.files = make_files()
        self.owners = Mock()
        self.owners.labels.side_effect = lambda subs: {s: f"利用者{s}" for s in subs}

    def _create_tables(self) -> None:
        def table(name: str, *, owner_index: bool = False) -> None:
            attributes = [{"AttributeName": "id", "AttributeType": "S"}]
            extra: dict[str, Any] = {}
            if owner_index:
                attributes += [
                    {"AttributeName": "owner_sub", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ]
                extra["GlobalSecondaryIndexes"] = [_OWNER_INDEX]
            self.client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=attributes,
                BillingMode="PAY_PER_REQUEST",
                **extra,
            )

        table(self.db.survey_templates_table, owner_index=True)
        table(self.db.surveys_table, owner_index=True)
        table(self.datasets_table)
        table(self.kb_table)
        table(self.custom_table)
        self.client.create_table(
            TableName=self.shares_table,
            KeySchema=[
                {"AttributeName": "group_id", "KeyType": "HASH"},
                {"AttributeName": "share_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "group_id", "AttributeType": "S"},
                {"AttributeName": "share_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    # --- seeding -----------------------------------------------------------------

    def share(
        self, resource_type: ResourceType, resource_id: str, *groups: str
    ) -> None:
        """Share a resource with departments (as ResourceSharing would)."""
        table = (
            self.db.survey_templates_table
            if resource_type is ResourceType.SURVEY_TEMPLATE
            else self.db.surveys_table
        )
        body = self.share_store.get_item(
            table, resource_id, ["id", "owner_sub", "shared_group_ids", "created_at"]
        )
        assert body is not None
        self.share_store.write_shares(
            table,
            resource_type,
            resource_id,
            expected=frozenset(body.get("shared_group_ids") or ()),
            new=frozenset(groups),
            created_at=str(body["created_at"]),
            owner_sub=str(body.get("owner_sub") or ""),
        )

    def template(
        self,
        owner: str | None,
        name: str = "テンプレート",
        *,
        images: list[TemplateImage] | None = None,
        created_at: datetime | None = None,
    ) -> SurveyTemplate:
        template = SurveyTemplate.create_new(
            name=name,
            questions=[
                Question.create_multiple_choice("好きな色は？", ["赤", "青"]),
                Question.create_scale_rating("満足度は？"),
            ],
            images=images or [],
        )
        if created_at is not None:
            template = replace(template, created_at=created_at, updated_at=created_at)
        if owner:
            template = template.owned_by(owner)
        self.db.put_new_survey_template(template)
        return template

    def survey(
        self,
        owner: str | None,
        template: SurveyTemplate | None = None,
        *,
        status: str = "completed",
        snapshot: bool = True,
        datasource: str | None = "nemotron",
        name: str = "結果",
        csv: bytes | None = None,
        created_at: datetime | None = None,
    ) -> Survey:
        template = template or SurveyTemplate.create_new(
            name="写しのテンプレート", questions=[Question.create_free_text("Q")]
        )
        survey = Survey.create_new(
            name=name,
            description="",
            template_id=template.id,
            persona_count=100,
            datasource=datasource,
            owner_sub=owner,
            template_snapshot=TemplateSnapshot.from_template(template)
            if snapshot
            else None,
        )
        if created_at is not None:
            survey = replace(survey, created_at=created_at, updated_at=created_at)
        survey.status = status
        if status == "completed":
            key = f"survey-results/{survey.id}/results.csv"
            survey.s3_result_path = self.s3.upload_file(csv or results_csv(), key)
        self.db.put_new_survey(survey)
        return survey

    def custom_data(
        self, name: str, *groups: str, rows: int = 150
    ) -> CustomPersonaData:
        data = CustomPersonaData.create_new(
            name=name,
            columns=["sex", "age", "hobby"],
            row_count=rows,
            registered_by="admin",
            extra_columns=[ExtraColumn(csv_column="hobby", description="趣味")],
            shared_group_ids=frozenset(groups),
            now=NOW,
        )
        self.custom_store.put_new(data)
        self.s3.upload_file(b"parquet", data.s3_key)
        return data

    def dataset(self, *groups: str, owner_persona_id: str | None = None) -> Dataset:
        dataset = Dataset.create_new(
            name="売上データ",
            description="説明",
            s3_path=f"s3://{BUCKET}/datasets/x.csv",
            columns=[DatasetColumn(name="a", data_type="string")],
            row_count=3,
            owner_persona_id=owner_persona_id,
        )
        self.dataset_store.put_new(dataset)
        if groups:
            self.dataset_store.update_scope(
                dataset.id, add=frozenset(groups), remove=frozenset()
            )
        return dataset

    def knowledge_base(self, *groups: str) -> KnowledgeBase:
        kb = KnowledgeBase.create_new("KB123", "社内資料", "説明")
        self.kb_store.put_new(kb)
        if groups:
            self.kb_store.update_scope(kb.id, add=frozenset(groups), remove=frozenset())
        return kb

    def raw(self, table: str, item_id: str) -> dict | None:
        return self.client.get_item(TableName=table, Key={"id": {"S": item_id}}).get(
            "Item"
        )

    def spy(self, operation: str) -> Mock:
        """Wrap one DynamoDB client operation (e.g. ``scan``) with a spy."""
        spy = Mock(wraps=getattr(self.client, operation))
        setattr(self.client, operation, spy)
        return spy


def bulk_put_custom_data(
    client: Any, table_name: str, records: list[CustomPersonaData]
) -> None:
    """Write records and their name reservations with batch writes.

    The items are the ones ``CustomPersonaDataStore.put_new`` sends (captured
    from its transaction), so they have the stored shape; one moto transaction
    per record is too slow for hundreds of records.
    """
    recorder = Mock()
    store = CustomPersonaDataStore(
        region=REGION, table_name=table_name, client=recorder
    )
    puts: list[dict[str, Any]] = []
    for record in records:
        store.put_new(record)
        items = recorder.transact_write_items.call_args.kwargs["TransactItems"]
        puts += [{"PutRequest": {"Item": i["Put"]["Item"]}} for i in items]
    for start in range(0, len(puts), 25):
        client.batch_write_item(RequestItems={table_name: puts[start : start + 25]})


def results_csv(rows: int = 3) -> bytes:
    """A result CSV of the template made by :meth:`SurveyWorld.survey`."""
    header = "persona_id,sex,age,occupation,country,region,prefecture,marital_status"
    lines = [header]
    for i in range(rows):
        lines.append(f"p{i},女性,{30 + i},会社員,日本,関東,東京都,未婚")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def survey_world(monkeypatch) -> Iterator[SurveyWorld]:
    moto = pytest.importorskip("moto")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with moto.mock_aws():
        yield SurveyWorld()
