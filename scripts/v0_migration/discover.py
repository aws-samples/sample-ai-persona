"""Find the tables, bucket and user pool of the v0 and v1 environments.

v0 (v0.19.x ``deploy.sh``) names its tables ``{prefix}_{Table}``; the prefix is
read from the ``PersonasTableName`` output of ``AIPersona-{env}``. v1 names
everything automatically; the names are read from the environment of the
running ECS task definition (``{stage}-App``), the same values the app uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: v0 table suffixes by the key the migration uses.
V0_TABLES: Mapping[str, str] = {
    "personas": "Personas",
    "discussions": "Discussions",
    "uploaded_files": "UploadedFiles",
    "datasets": "Datasets",
    "persona_dataset_bindings": "PersonaDatasetBindings",
    "survey_templates": "SurveyTemplates",
    "surveys": "Surveys",
    "knowledge_bases": "KnowledgeBases",
    "persona_kb_bindings": "PersonaKBBindings",
}

#: v1 task environment variables by the key the migration uses (src/config.py).
V1_TABLES: Mapping[str, str] = {
    "personas": "PERSONAS_TABLE_NAME",
    "discussions": "DISCUSSIONS_TABLE_NAME",
    "discussion_messages": "DISCUSSION_MESSAGES_TABLE_NAME",
    "discussion_reports": "DISCUSSION_REPORTS_TABLE_NAME",
    "uploaded_files": "UPLOADED_FILES_TABLE_NAME",
    "datasets": "DATASETS_TABLE_NAME",
    "survey_templates": "SURVEY_TEMPLATES_TABLE_NAME",
    "surveys": "SURVEYS_TABLE_NAME",
    "knowledge_bases": "KNOWLEDGE_BASES_TABLE_NAME",
    "custom_persona_data": "CUSTOM_PERSONA_DATA_TABLE_NAME",
    "persona_bindings": "PERSONA_BINDINGS_TABLE_NAME",
    "resource_shares": "RESOURCE_SHARES_TABLE_NAME",
}


class DiscoveryError(RuntimeError):
    """A stack, output or setting the migration needs was not found."""


@dataclass(frozen=True)
class Environment:
    """Where one side of the migration keeps its data."""

    region: str
    tables: Mapping[str, str]
    bucket: str
    user_pool_id: str

    def table(self, key: str) -> str:
        return self.tables[key]


def _outputs(cfn: Any, stack: str) -> dict[str, str]:
    try:
        response = cfn.describe_stacks(StackName=stack)
    except Exception as e:  # botocore ClientError: the stack does not exist
        raise DiscoveryError(f"stack {stack} not found: {type(e).__name__}") from e
    stacks = response.get("Stacks") or []
    if not stacks:
        raise DiscoveryError(f"stack {stack} not found")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs") or []}


def _required(values: Mapping[str, str], key: str, where: str) -> str:
    value = values.get(key)
    if not value:
        raise DiscoveryError(f"{key} not found in {where}")
    return value


def discover_v0(cfn: Any, *, env: str, region: str) -> Environment:
    """The v0 environment deployed with ``deploy.sh --env {env}``.

    Raises:
        DiscoveryError: A stack or output is missing.
    """
    main_stack = f"AIPersona-{env}"
    outputs = _outputs(cfn, main_stack)
    personas = _required(outputs, "PersonasTableName", main_stack)
    if not personas.endswith("_Personas"):
        raise DiscoveryError(f"unexpected PersonasTableName {personas!r}")
    prefix = personas[: -len("_Personas")]
    cognito_stack = f"AIPersonaCognito-{env}"
    return Environment(
        region=region,
        tables={key: f"{prefix}_{suffix}" for key, suffix in V0_TABLES.items()},
        bucket=_required(outputs, "UploadBucketName", main_stack),
        user_pool_id=_required(
            _outputs(cfn, cognito_stack), "UserPoolId", cognito_stack
        ),
    )


def discover_v1(cfn: Any, ecs: Any, *, stage: str, region: str) -> Environment:
    """The v1 environment of the CDK stage ``stage`` (``AIPersonaDev`` etc.).

    Raises:
        DiscoveryError: A stack, the service or a setting is missing.
    """
    app_stack = f"{stage}-App"
    outputs = _outputs(cfn, app_stack)
    cluster = _required(outputs, "ClusterName", app_stack)
    service = _required(outputs, "ServiceName", app_stack)
    services = (
        ecs.describe_services(cluster=cluster, services=[service]).get("services") or []
    )
    if not services:
        raise DiscoveryError(f"ECS service {service} not found in {cluster}")
    definition = ecs.describe_task_definition(
        taskDefinition=services[0]["taskDefinition"]
    )
    environment: dict[str, str] = {}
    for container in definition["taskDefinition"].get("containerDefinitions") or []:
        for pair in container.get("environment") or []:
            environment[pair["name"]] = pair["value"]
    where = f"the task definition of {service}"
    return Environment(
        region=region,
        tables={
            key: _required(environment, var, where) for key, var in V1_TABLES.items()
        },
        bucket=_required(environment, "S3_BUCKET_NAME", where),
        user_pool_id=_required(environment, "COGNITO_USER_POOL_ID", where),
    )
