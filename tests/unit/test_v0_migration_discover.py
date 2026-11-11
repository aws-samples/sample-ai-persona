"""v0 / v1 の環境の名前を CloudFormation と ECS から引くテスト（移行スクリプト）。"""

from typing import Any

import pytest

from scripts.v0_migration.discover import (
    V1_TABLES,
    DiscoveryError,
    discover_v0,
    discover_v1,
)


class FakeCfn:
    def __init__(self, outputs: dict[str, dict[str, str]]) -> None:
        self.outputs = outputs

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        if StackName not in self.outputs:
            raise RuntimeError("does not exist")
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v}
                        for k, v in self.outputs[StackName].items()
                    ]
                }
            ]
        }


class FakeEcs:
    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment
        self.calls: list[tuple[str, Any]] = []

    def describe_services(self, cluster: str, services: list[str]) -> dict[str, Any]:
        self.calls.append(("services", (cluster, services)))
        return {"services": [{"taskDefinition": "arn:td:1"}]}

    def describe_task_definition(self, taskDefinition: str) -> dict[str, Any]:
        self.calls.append(("td", taskDefinition))
        env = [{"name": k, "value": v} for k, v in self.environment.items()]
        return {"taskDefinition": {"containerDefinitions": [{"environment": env}]}}


V0_OUTPUTS = {
    "AIPersona-dev": {
        "PersonasTableName": "AIPersonaDev_Personas",
        "UploadBucketName": "aipersonadev-uploads",
    },
    "AIPersonaCognito-dev": {"UserPoolId": "us-east-1_v0"},
}


def test_v0_names_come_from_the_prefix():
    env = discover_v0(FakeCfn(V0_OUTPUTS), env="dev", region="us-east-1")
    assert env.table("personas") == "AIPersonaDev_Personas"
    assert env.table("persona_kb_bindings") == "AIPersonaDev_PersonaKBBindings"
    assert env.bucket == "aipersonadev-uploads"
    assert env.user_pool_id == "us-east-1_v0"


def test_v0_missing_stack():
    with pytest.raises(DiscoveryError, match="AIPersona-prod"):
        discover_v0(FakeCfn(V0_OUTPUTS), env="prod", region="us-east-1")


def test_v0_unexpected_table_name():
    outputs = {
        **V0_OUTPUTS,
        "AIPersona-dev": {"PersonasTableName": "x", "UploadBucketName": "b"},
    }
    with pytest.raises(DiscoveryError, match="PersonasTableName"):
        discover_v0(FakeCfn(outputs), env="dev", region="us-east-1")


def _v1_env() -> dict[str, str]:
    env = {var: f"t-{key}" for key, var in V1_TABLES.items()}
    env.update({"S3_BUCKET_NAME": "v1-bucket", "COGNITO_USER_POOL_ID": "ap_v1"})
    return env


def test_v1_names_come_from_the_task_definition():
    cfn = FakeCfn({"AIPersonaDev-App": {"ClusterName": "c", "ServiceName": "s"}})
    ecs = FakeEcs(_v1_env())
    env = discover_v1(cfn, ecs, stage="AIPersonaDev", region="ap-northeast-1")
    assert env.table("resource_shares") == "t-resource_shares"
    assert env.bucket == "v1-bucket"
    assert env.user_pool_id == "ap_v1"
    assert ecs.calls[0] == ("services", ("c", ["s"]))


def test_v1_missing_setting():
    env = _v1_env()
    env.pop("DISCUSSION_MESSAGES_TABLE_NAME")
    cfn = FakeCfn({"AIPersonaDev-App": {"ClusterName": "c", "ServiceName": "s"}})
    with pytest.raises(DiscoveryError, match="DISCUSSION_MESSAGES_TABLE_NAME"):
        discover_v1(cfn, FakeEcs(env), stage="AIPersonaDev", region="ap-northeast-1")
