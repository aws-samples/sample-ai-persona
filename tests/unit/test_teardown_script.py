"""destroy.sh（v1.0.0）と scripts/destroy-v0.sh の検査。

AWS には触れない。PATH の先頭に偽の aws を置き、呼ばれた引数を記録して、
削除するスタック・順序・リージョンと、v0 やデータに触れないことを確かめる。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
SCRIPT = ROOT / "destroy.sh"
V0_SCRIPT = ROOT / "scripts" / "destroy-v0.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


MEMORY_ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:memory/AIPersona_v1_dev-abc123"
STATE_MACHINE_ARN = "arn:aws:states:ap-northeast-1:123456789012:stateMachine:SurveyRun"
KMS_KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
SECRET_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:ClientSecret-xyz"
)

# describe-stack-resources の --output text の形（種類<TAB>物理 ID）
DATA_RESOURCES = "\n".join(
    [
        # KMS キーを先に並べ、テーブルより後に削除を予約することを確かめる
        f"AWS::KMS::Key\t{KMS_KEY_ID}",
        "AWS::DynamoDB::GlobalTable\tpersona-table",
        "AWS::S3::Bucket\tupload-bucket",
        "AWS::Cognito::UserPool\tap-northeast-1_pool",
        "AWS::ECR::Repository\tapp-repo",
        f"AWS::BedrockAgentCore::Memory\t{MEMORY_ARN}",
    ]
)
APP_RESOURCES = "\n".join(
    [
        "AWS::Logs::LogGroup\tapp-log-group",
        f"AWS::StepFunctions::StateMachine\t{STATE_MACHINE_ARN}",
        f"AWS::SecretsManager::Secret\t{SECRET_ARN}",
    ]
)


def _fake_aws(
    bin_dir: Path, calls: Path, *, stacks_exist: bool, fail_on: str = ""
) -> None:
    """Install a fake aws CLI that records its arguments.

    describe-stacks succeeds only when ``stacks_exist`` is true. describe-stack-resources
    returns retained resources for the Data and App stacks. list-object-versions returns one
    version on the first call and none afterwards. A call containing ``fail_on`` exits 1;
    every other call succeeds.
    """
    bin_dir.mkdir()
    describe_exit = 0 if stacks_exist else 254
    listed = bin_dir / "versions-listed"
    fail_case = f'  *"{fail_on}"*) exit 1 ;;\n' if fail_on else ""
    script = f"""#!/bin/sh
echo "aws $*" >> "{calls}"
case "$*" in
{fail_case}  *"sts get-caller-identity"*) echo 123456789012 ;;
  *"cloudformation describe-stacks"*) exit {describe_exit} ;;
  *"describe-stack-resources --stack-name AIPersonaDev-Data"*|*"describe-stack-resources --stack-name AIPersonaDevClosed-Data"*)
    printf '%s\\n' "{DATA_RESOURCES}" ;;
  *"describe-stack-resources --stack-name AIPersonaDev-App"*|*"describe-stack-resources --stack-name AIPersonaDevClosed-App"*)
    printf '%s\\n' "{APP_RESOURCES}" ;;
  *"s3api list-object-versions"*)
    if [ -f "{listed}" ]; then
      echo '{{"Objects": [], "Quiet": true}}'
    else
      touch "{listed}"
      echo '{{"Objects": [{{"Key": "a.png", "VersionId": "v1"}}], "Quiet": true}}'
    fi ;;
  *"cognito-idp describe-user-pool"*) echo None ;;
esac
exit 0
"""
    fake = bin_dir / "aws"
    fake.write_text(script)
    fake.chmod(0o755)


def _run(
    script: Path, args: list[str], cwd: Path, path_prefix: Path, stdin: str = ""
) -> subprocess.CompletedProcess:
    env = {"PATH": f"{path_prefix}:/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8"}
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tree with both scripts and cdk/bin/app.ts, as at the project root."""
    root = tmp_path / "repo"
    (root / "cdk" / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "cdk" / "bin" / "app.ts").write_text("// stub\n")
    shutil.copy(SCRIPT, root / "destroy.sh")
    shutil.copy(V0_SCRIPT, root / "scripts" / "destroy-v0.sh")
    return root


def _aws(
    tmp_path: Path, *, stacks_exist: bool = True, fail_on: str = ""
) -> tuple[Path, Path]:
    calls = tmp_path / "calls.log"
    calls.touch()
    bin_dir = tmp_path / "bin"
    _fake_aws(bin_dir, calls, stacks_exist=stacks_exist, fail_on=fail_on)
    return bin_dir, calls


def _deletes(calls: Path) -> list[str]:
    return [line for line in calls.read_text().splitlines() if "delete-stack " in line]


# RETAIN のリソースを変える・消す呼び出し（--purge-retained のときだけ出てよい）
DATA_MUTATIONS = (
    "update-table",
    "delete-table",
    "delete-objects",
    "delete-bucket",
    "update-user-pool",
    "delete-user-pool",
    "delete-repository",
    "batch-delete-image",
    "delete-memory",
    "delete-secret",
    "delete-log-group",
    "delete-state-machine",
    "s3 rm",
    "s3 rb",
    "schedule-key-deletion",
)


def _index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


@pytest.mark.parametrize("script", [SCRIPT, V0_SCRIPT])
def test_scripts_have_valid_bash_syntax(script: Path):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_help_lists_options_without_touching_aws(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", ["--help"], repo, bin_dir)

    assert result.returncode == 0
    for option in ("--closed", "--region", "--force"):
        assert option in result.stdout
    assert "scripts/destroy-v0.sh" in result.stdout
    assert calls.read_text() == ""


def test_unknown_option_is_rejected_before_aws(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", ["--env", "dev"], repo, bin_dir)

    assert result.returncode != 0
    assert calls.read_text() == ""


def test_public_stage_is_deleted_app_then_edge_then_data(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", ["--force"], repo, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _deletes(calls) == [
        "aws cloudformation delete-stack --stack-name AIPersonaDev-App --region ap-northeast-1",
        "aws cloudformation delete-stack --stack-name AIPersonaDev-Edge --region us-east-1",
        "aws cloudformation delete-stack --stack-name AIPersonaDev-Data --region ap-northeast-1",
    ]


def test_closed_stage_deletes_only_its_app_and_data_in_the_given_region(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(
        repo / "destroy.sh",
        ["--closed", "--region", "us-west-2", "--force"],
        repo,
        bin_dir,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _deletes(calls) == [
        "aws cloudformation delete-stack --stack-name AIPersonaDevClosed-App --region us-west-2",
        "aws cloudformation delete-stack --stack-name AIPersonaDevClosed-Data --region us-west-2",
    ]
    assert "us-east-1" not in calls.read_text()


def test_never_touches_v0_stacks_or_retained_data(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", ["--force"], repo, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text()
    for v0_stack in (
        "AIPersona-dev",
        "AIPersonaEcr-",
        "AIPersonaCognito-",
        "AIPersonaWaf-",
    ):
        assert v0_stack not in log
    for data_call in DATA_MUTATIONS:
        assert data_call not in log
    # 残るリソースを利用者に示す
    assert "persona-table" in result.stdout


def test_help_lists_purge_option(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", ["--help"], repo, bin_dir)

    assert "--purge-retained" in result.stdout
    assert calls.read_text() == ""


@pytest.mark.parametrize("typed", ["", "yes", "AIPersonaDevClosed"])
def test_purge_requires_the_stage_name_even_with_force(repo, tmp_path, typed):
    bin_dir, calls = _aws(tmp_path)
    result = _run(
        repo / "destroy.sh",
        ["--purge-retained", "--force"],
        repo,
        bin_dir,
        stdin=f"{typed}\n",
    )

    assert result.returncode == 0
    assert _deletes(calls) == []
    log = calls.read_text()
    for data_call in DATA_MUTATIONS:
        assert data_call not in log


def test_purge_deletes_retained_resources_after_every_stack(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(
        repo / "destroy.sh",
        ["--purge-retained", "--force"],
        repo,
        bin_dir,
        stdin="AIPersonaDev\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.read_text().splitlines()
    last_stack_delete = _index(lines, "delete-stack --stack-name AIPersonaDev-Data")
    first_purge = min(
        i for i, line in enumerate(lines) if any(m in line for m in DATA_MUTATIONS)
    )
    assert last_stack_delete < first_purge

    # 削除保護を外してから消す
    assert _index(lines, "update-table --table-name persona-table") < _index(
        lines, "delete-table --table-name persona-table"
    )
    assert "--no-deletion-protection-enabled" in lines[_index(lines, "update-table")]
    pool_update = lines[
        _index(lines, "update-user-pool --user-pool-id ap-northeast-1_pool")
    ]
    assert "--deletion-protection INACTIVE" in pool_update
    assert _index(lines, "update-user-pool") < _index(lines, "delete-user-pool ")

    # バージョニング有効のバケットは全バージョンを消してからバケットを消す
    assert _index(lines, "delete-objects --bucket upload-bucket") < _index(
        lines, "delete-bucket --bucket upload-bucket"
    )
    assert (
        "delete-repository --repository-name app-repo"
        in lines[_index(lines, "delete-repository")]
    )
    assert "--force" in lines[_index(lines, "delete-repository")]
    assert (
        "delete-memory --memory-id AIPersona_v1_dev-abc123 "
        in lines[_index(lines, "delete-memory")]
    )
    secret = lines[_index(lines, "delete-secret")]
    assert SECRET_ARN in secret
    assert "--recovery-window-in-days 7" in secret
    assert "--force-delete-without-recovery" not in secret
    assert (
        "delete-log-group --log-group-name app-log-group"
        in lines[_index(lines, "delete-log-group")]
    )
    assert STATE_MACHINE_ARN in lines[_index(lines, "delete-state-machine")]

    # KMS キーは最後に削除を予約する（即時には消さない）
    kms = _index(lines, "schedule-key-deletion")
    assert f"--key-id {KMS_KEY_ID} " in lines[kms]
    assert "--pending-window-in-days 7" in lines[kms]
    assert kms > _index(lines, "delete-table --table-name persona-table")
    assert kms == max(
        i for i, line in enumerate(lines) if any(m in line for m in DATA_MUTATIONS)
    )


def test_purge_failure_continues_and_exits_non_zero(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path, fail_on="delete-table")
    result = _run(
        repo / "destroy.sh",
        ["--purge-retained", "--force"],
        repo,
        bin_dir,
        stdin="AIPersonaDev\n",
    )

    assert result.returncode != 0
    log = calls.read_text()
    assert "delete-table --table-name persona-table" in log
    # 1 件の失敗で止めず、残りは消す
    assert "delete-bucket --bucket upload-bucket" in log
    assert "delete-state-machine" in log
    assert "persona-table" in result.stdout + result.stderr


def test_purge_stops_before_data_when_a_stack_delete_fails(repo, tmp_path):
    bin_dir, calls = _aws(
        tmp_path, fail_on="wait stack-delete-complete --stack-name AIPersonaDev-App"
    )
    result = _run(
        repo / "destroy.sh",
        ["--purge-retained", "--force"],
        repo,
        bin_dir,
        stdin="AIPersonaDev\n",
    )

    assert result.returncode != 0
    assert _deletes(calls) == [
        "aws cloudformation delete-stack --stack-name AIPersonaDev-App --region ap-northeast-1",
    ]
    log = calls.read_text()
    for data_call in DATA_MUTATIONS:
        assert data_call not in log


def test_declining_the_prompt_deletes_nothing(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "destroy.sh", [], repo, bin_dir, stdin="no\n")

    assert result.returncode == 0
    assert _deletes(calls) == []


def test_no_stacks_means_no_deletes(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path, stacks_exist=False)
    result = _run(repo / "destroy.sh", ["--force"], repo, bin_dir)

    assert result.returncode == 0
    assert _deletes(calls) == []


def test_v0_script_help_runs_from_scripts_dir_without_touching_aws(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(repo / "scripts" / "destroy-v0.sh", ["--help"], repo, bin_dir)

    assert result.returncode == 0
    assert "scripts/destroy-v0.sh" in result.stdout
    assert calls.read_text() == ""


def test_v0_script_finds_project_root_from_scripts_dir(repo, tmp_path):
    bin_dir, calls = _aws(tmp_path, stacks_exist=False)
    result = _run(repo / "scripts" / "destroy-v0.sh", ["--force"], repo, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CDKプロジェクトが見つかりません" not in result.stdout + result.stderr
    assert _deletes(calls) == []
