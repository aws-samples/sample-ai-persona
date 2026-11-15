"""scripts/purge-retained.sh（v1.0.0）の検査。

スタックを消したあとに、別のセッションで RETAIN のリソースを削除するスクリプト。
AWS には触れない。PATH の先頭に偽の aws を置き、呼ばれた引数を記録して、
削除済みスタックから取り直したリソースを、確認のうえ正しい順で消すことを確かめる。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
SCRIPT = ROOT / "scripts" / "purge-retained.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

MEMORY_ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:memory/AIPersona_v1_dev-abc123"
STATE_MACHINE_ARN = "arn:aws:states:ap-northeast-1:123456789012:stateMachine:SurveyRun"
KMS_KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
SECRET_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:ClientSecret-xyz"
)


def _stack_id(name: str, suffix: str, region: str = "ap-northeast-1") -> str:
    return f"arn:aws:cloudformation:{region}:123456789012:stack/{name}/{suffix}"


# list-stack-resources の --output text の形（種類<TAB>物理 ID）
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

# RETAIN のリソースを変える・消す呼び出し
DATA_MUTATIONS = (
    "update-table",
    "delete-table",
    "delete-objects",
    "delete-bucket",
    "update-user-pool",
    "delete-user-pool",
    "delete-repository",
    "delete-memory",
    "delete-secret",
    "delete-log-group",
    "delete-state-machine",
    "schedule-key-deletion",
)

# 存在確認の呼び出し（gone に挙げた物理 ID は「見つからない」を返す）
PROBES = (
    "describe-table",
    "head-bucket",
    "describe-user-pool --user-pool-id",
    "describe-repositories",
    "get-memory",
    "describe-secret",
    "describe-state-machine",
    "describe-key",
)


def _fake_aws(
    bin_dir: Path,
    calls: Path,
    *,
    stacks_exist: bool = False,
    deleted_stacks: bool = True,
    gone: tuple[str, ...] = (),
    fail_on: str = "",
) -> None:
    """Install a fake aws CLI that records its arguments.

    describe-stacks succeeds only when ``stacks_exist``. list-stacks returns two deleted
    Data stacks (an old and a new one, with the same resources) and one App stack when
    ``deleted_stacks``. A probe naming an ID in ``gone`` fails with a not-found error.
    A call containing ``fail_on`` exits 1; every other call succeeds.
    """
    bin_dir.mkdir()
    listed = bin_dir / "versions-listed"
    fail_case = f'  *"{fail_on}"*) exit 1 ;;\n' if fail_on else ""
    gone_cases = "".join(
        f'  *"{probe}"*"{rid}"*) echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;\n'
        for rid in gone
        for probe in PROBES
    )
    log_count = "0" if "app-log-group" in gone else "1"
    key_state = "PendingDeletion" if KMS_KEY_ID in gone else "Enabled"
    secret_deleted = "2026-11-01T00:00:00Z" if SECRET_ARN in gone else "None"
    if deleted_stacks:
        data_ids = f"{_stack_id('STAGE-Data', 'old')}\t{_stack_id('STAGE-Data', 'new')}"
        app_ids = _stack_id("STAGE-App", "one")
    else:
        data_ids = app_ids = ""
    script = f"""#!/bin/sh
echo "aws $*" >> "{calls}"
case "$*" in
{fail_case}{gone_cases}  *"sts get-caller-identity"*) echo 123456789012 ;;
  *"cloudformation describe-stacks"*) exit {0 if stacks_exist else 254} ;;
  *"list-stacks"*"-Data'"*) echo "{data_ids}" | sed "s/STAGE/$(echo "$*" | sed -E "s/.*StackName=='([A-Za-z]+)-Data'.*/\\1/")/g" ;;
  *"list-stacks"*"-App'"*) echo "{app_ids}" | sed "s/STAGE/$(echo "$*" | sed -E "s/.*StackName=='([A-Za-z]+)-App'.*/\\1/")/g" ;;
  *"list-stacks"*) echo "" ;;
  *"list-stack-resources --stack-name "*"-Data/"*) printf '%s\\n' "{DATA_RESOURCES}" ;;
  *"list-stack-resources --stack-name "*"-App/"*) printf '%s\\n' "{APP_RESOURCES}" ;;
  *"s3api list-object-versions"*)
    if [ -f "{listed}" ]; then
      echo '{{"Objects": [], "Quiet": true}}'
    else
      touch "{listed}"
      echo '{{"Objects": [{{"Key": "a.png", "VersionId": "v1"}}], "Quiet": true}}'
    fi ;;
  *"s3api delete-objects"*) echo None ;;
  *"cognito-idp describe-user-pool"*) echo None ;;
  *"logs describe-log-groups"*) echo {log_count} ;;
  *"kms describe-key"*) echo {key_state} ;;
  *"secretsmanager describe-secret"*) echo {secret_deleted} ;;
esac
exit 0
"""
    fake = bin_dir / "aws"
    fake.write_text(script)
    fake.chmod(0o755)


def _run(
    args: list[str], tmp_path: Path, bin_dir: Path, stdin: str = ""
) -> subprocess.CompletedProcess:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / "purge-retained.sh")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    return subprocess.run(
        ["bash", str(root / "scripts" / "purge-retained.sh"), *args],
        cwd=root,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _aws(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    calls = tmp_path / "calls.log"
    calls.touch()
    bin_dir = tmp_path / "bin"
    _fake_aws(bin_dir, calls, **kwargs)
    return bin_dir, calls


def _mutations(log: str) -> list[str]:
    return [line for line in log.splitlines() if any(m in line for m in DATA_MUTATIONS)]


def _index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


def test_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_help_runs_without_touching_aws(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(["--help"], tmp_path, bin_dir)

    assert result.returncode == 0
    for option in ("--closed", "--region"):
        assert option in result.stdout
    assert calls.read_text() == ""


def test_unknown_option_is_rejected_before_aws(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(["--force"], tmp_path, bin_dir)

    assert result.returncode != 0
    assert calls.read_text() == ""


def test_refuses_while_a_stack_of_the_stage_still_exists(tmp_path):
    bin_dir, calls = _aws(tmp_path, stacks_exist=True)
    result = _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    assert result.returncode != 0
    assert "destroy.sh" in result.stdout + result.stderr
    assert _mutations(calls.read_text()) == []


@pytest.mark.parametrize("typed", ["", "yes", "AIPersonaDevClosed"])
def test_nothing_is_deleted_unless_the_stage_name_is_typed(tmp_path, typed):
    bin_dir, calls = _aws(tmp_path)
    result = _run([], tmp_path, bin_dir, stdin=f"{typed}\n")

    assert result.returncode == 0
    assert "persona-table" in result.stdout  # the list is shown before asking
    assert _mutations(calls.read_text()) == []


def test_retained_resources_are_found_from_the_deleted_stacks(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text()
    # 削除済みのスタックだけを、Edge は us-east-1 で探す
    assert "--stack-status-filter DELETE_COMPLETE" in log
    assert "AIPersonaDev-Edge'" in log
    edge = _index(log.splitlines(), "AIPersonaDev-Edge'")
    assert "--region us-east-1" in log.splitlines()[edge]
    # 名前ではなく削除済みスタックの ID で、RETAIN で残ったものだけを引く
    resources = [line for line in log.splitlines() if "list-stack-resources" in line]
    assert any(_stack_id("AIPersonaDev-Data", "new") in line for line in resources)
    assert all("DELETE_SKIPPED" in line for line in resources)


def test_purge_deletes_in_a_safe_order(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.read_text().splitlines()
    # 削除保護を外してから消す
    assert _index(lines, "update-table --table-name persona-table") < _index(
        lines, "delete-table --table-name persona-table"
    )
    assert "--no-deletion-protection-enabled" in lines[_index(lines, "update-table")]
    assert "--deletion-protection INACTIVE" in lines[_index(lines, "update-user-pool")]
    assert _index(lines, "update-user-pool") < _index(lines, "delete-user-pool ")
    # バージョニング有効のバケットは全バージョンを消してからバケットを消す
    assert _index(lines, "delete-objects --bucket upload-bucket") < _index(
        lines, "delete-bucket --bucket upload-bucket"
    )
    assert (
        "--force"
        in lines[_index(lines, "delete-repository --repository-name app-repo")]
    )
    assert (
        "delete-memory --memory-id AIPersona_v1_dev-abc123 "
        in lines[_index(lines, "delete-memory")]
    )
    secret = lines[_index(lines, "delete-secret")]
    assert SECRET_ARN in secret and "--recovery-window-in-days 7" in secret
    assert "--force-delete-without-recovery" not in secret
    assert (
        "delete-log-group --log-group-name app-log-group"
        in lines[_index(lines, "delete-log-group")]
    )
    assert STATE_MACHINE_ARN in lines[_index(lines, "delete-state-machine")]
    # KMS キーは最後に削除を予約する（即時には消さない）
    mutations = _mutations("\n".join(lines))
    assert f"--key-id {KMS_KEY_ID} " in mutations[-1]
    assert "schedule-key-deletion" in mutations[-1]
    assert "--pending-window-in-days 7" in mutations[-1]


def test_a_resource_found_in_two_deleted_stacks_is_deleted_once(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    deletes = [
        line
        for line in calls.read_text().splitlines()
        if "delete-table --table-name persona-table" in line
    ]
    assert len(deletes) == 1


def test_resources_already_gone_are_skipped(tmp_path):
    bin_dir, calls = _aws(tmp_path, gone=("persona-table", "upload-bucket", KMS_KEY_ID))
    result = _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text()
    for skipped in (
        "delete-table",
        "delete-bucket",
        "delete-objects",
        "schedule-key-deletion",
    ):
        assert skipped not in log
    assert "delete-repository --repository-name app-repo" in log


def test_nothing_left_ends_without_asking(tmp_path):
    every = (
        KMS_KEY_ID,
        "persona-table",
        "upload-bucket",
        "ap-northeast-1_pool",
        "app-repo",
        "AIPersona_v1_dev-abc123",
        "app-log-group",
        STATE_MACHINE_ARN,
        SECRET_ARN,
    )
    bin_dir, calls = _aws(tmp_path, gone=every)
    result = _run([], tmp_path, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _mutations(calls.read_text()) == []
    assert "警告" not in result.stdout  # nothing to confirm, so no list and no prompt


def test_no_deleted_stacks_ends_without_deleting(tmp_path):
    bin_dir, calls = _aws(tmp_path, deleted_stacks=False)
    result = _run([], tmp_path, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _mutations(calls.read_text()) == []


def test_a_failure_continues_and_asks_to_run_again(tmp_path):
    bin_dir, calls = _aws(tmp_path, fail_on="delete-table")
    result = _run([], tmp_path, bin_dir, stdin="AIPersonaDev\n")

    assert result.returncode != 0
    log = calls.read_text()
    # 1 件の失敗で止めず、残りは消す
    assert "delete-bucket --bucket upload-bucket" in log
    assert "delete-state-machine" in log
    output = result.stdout + result.stderr
    assert "persona-table" in output
    assert "purge-retained.sh" in output  # 再実行で続きから消せる


def test_closed_stage_uses_its_names_and_region_only(tmp_path):
    bin_dir, calls = _aws(tmp_path)
    result = _run(
        ["--closed", "--region", "us-west-2"],
        tmp_path,
        bin_dir,
        stdin="AIPersonaDevClosed\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text()
    assert "AIPersonaDevClosed-Data'" in log
    assert "-Edge'" not in log
    assert "us-east-1" not in log
    assert "delete-table --table-name persona-table --region us-west-2" in log
