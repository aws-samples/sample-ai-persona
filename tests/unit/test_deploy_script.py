"""deploy.sh（v1.0.0）の検査（FR9.2、NFR9.9、cicd-pipeline.md、Infrastructure Design R-09・R-10）。

AWS・Docker・CDK には触れない。PATH の先頭に偽の aws・docker・npx・npm を置き、
呼ばれたら記録して失敗するようにして、止まるべき所で止まることを確かめる。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
SCRIPT = ROOT / "deploy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash and git are required",
)


def _fake_tools(bin_dir: Path, calls: Path) -> None:
    bin_dir.mkdir()
    for tool in ("aws", "docker", "npx", "npm"):
        fake = bin_dir / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 97\n')
        fake.chmod(0o755)


def _run(args: list[str], cwd: Path, path_prefix: Path) -> subprocess.CompletedProcess:
    env = {"PATH": f"{path_prefix}:/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8"}
    return subprocess.run(
        ["bash", str(cwd / "deploy.sh"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """deploy.sh と cdk/bin/app.ts だけを持つ Git の作業ツリー。"""
    repo = tmp_path / "repo"
    (repo / "cdk" / "bin").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "deploy.sh")
    (repo / "cdk" / "bin" / "app.ts").write_text("// stub\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    calls = tmp_path / "calls.log"
    calls.touch()
    bin_dir = tmp_path / "bin"
    _fake_tools(bin_dir, calls)
    return repo, bin_dir, calls


def test_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_help_lists_the_three_options_without_touching_aws(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--help"], repo, bin_dir)

    assert result.returncode == 0
    for option in ("--restart-app", "--image-tag", "--region"):
        assert option in result.stdout
    # v0 のオプションは消した
    for removed in ("--skip-cognito", "--skip-memory", "--self-signup"):
        assert removed not in result.stdout
    assert calls.read_text() == ""


def test_dirty_working_tree_stops_before_any_aws_call(workspace):
    repo, bin_dir, calls = workspace
    (repo / "uncommitted.txt").write_text("local change\n")

    result = _run([], repo, bin_dir)

    assert result.returncode != 0
    assert "作業ツリー" in result.stdout + result.stderr
    assert calls.read_text() == ""


def test_unknown_option_is_rejected(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--skip-cognito"], repo, bin_dir)
    assert result.returncode != 0
    assert calls.read_text() == ""


def test_unset_image_tag_is_refused(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--image-tag", "unset"], repo, bin_dir)
    assert result.returncode != 0
    assert "unset" in result.stdout + result.stderr
    assert calls.read_text() == ""


def test_region_option_reaches_every_cdk_deploy(workspace, tmp_path):
    """--region is passed to CDK as -c region=..., not only to bootstrap and the CLI."""
    repo, _, _ = workspace
    calls = tmp_path / "ok-calls.log"
    calls.touch()
    ok_bin = tmp_path / "ok-bin"
    ok_bin.mkdir()
    for tool in ("aws", "docker", "npx", "npm"):
        fake = ok_bin / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\necho 123\nexit 0\n')
        fake.chmod(0o755)

    result = _run(["--region", "us-west-2", "--image-tag", "abc1234"], repo, ok_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    deploys = [
        line for line in calls.read_text().splitlines() if " cdk deploy " in line
    ]
    assert len(deploys) == 2  # ① Data・Edge と ③ App
    assert all("-c region=us-west-2" in line for line in deploys)
    bootstraps = [
        line for line in calls.read_text().splitlines() if "cdk bootstrap" in line
    ]
    assert any("aws://123/us-west-2" in line for line in bootstraps)


# 閉域対応 U3（W6）: --nemotron-dataset-key は --closed のときだけ -c closedNemotronDatasetKey


def _ok_tools(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "ok-calls.log"
    calls.touch()
    ok_bin = tmp_path / "ok-bin"
    ok_bin.mkdir()
    for tool in ("aws", "docker", "npx", "npm"):
        fake = ok_bin / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\necho 123\nexit 0\n')
        fake.chmod(0o755)
    return ok_bin, calls


_CLOSED = [
    "--closed",
    "--certificate-arn",
    "arn:aws:acm:ap-northeast-1:123456789012:certificate/0",
    "--domain-name",
    "ai-persona.closed.example",
    "--create-test-environment",
    "--image-tag",
    "abc1234",
]


def test_nemotron_dataset_key_needs_closed(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--nemotron-dataset-key", "closed/n.parquet"], repo, bin_dir)
    assert result.returncode != 0
    assert "--nemotron-dataset-key" in result.stdout + result.stderr
    assert calls.read_text() == ""


def test_nemotron_dataset_key_reaches_every_closed_cdk_deploy(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run(
        [*_CLOSED, "--nemotron-dataset-key", "closed/n.parquet"], repo, ok_bin
    )
    assert result.returncode == 0, result.stdout + result.stderr
    deploys = [
        line for line in calls.read_text().splitlines() if " cdk deploy " in line
    ]
    assert len(deploys) == 2
    assert all("-c closedNemotronDatasetKey=closed/n.parquet" in d for d in deploys)


def test_closed_deploy_without_the_key_passes_no_key_context(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run(_CLOSED, repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "closedNemotronDatasetKey" not in calls.read_text()


# 閉域対応 U5（BR1.9）: --inference-scope は対象の Stage のコンテキストだけに渡す。
# 公開は -c inferenceScope=<値>、--closed は -c closedInferenceScope=<値>（閉域の synth は
# 公開の Stage も組み立てるため、共通の名前だと公開にも効いてしまう）


def _deploys(calls: Path) -> list[str]:
    return [line for line in calls.read_text().splitlines() if " cdk deploy " in line]


@pytest.mark.parametrize(
    ("base", "context", "other"),
    [
        (["--image-tag", "abc1234"], "-c inferenceScope=jp", "closedInferenceScope"),
        (_CLOSED, "-c closedInferenceScope=jp", "-c inferenceScope="),
    ],
    ids=["public", "closed"],
)
def test_inference_scope_reaches_only_the_target_stage(
    workspace, tmp_path, base, context, other
):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run([*base, "--inference-scope", "jp"], repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    deploys = _deploys(calls)
    assert len(deploys) == 2
    assert all(context in d for d in deploys)
    assert not any(other in d for d in deploys)
    assert "推論の範囲: jp" in result.stdout


def test_without_inference_scope_no_context_and_the_default_is_shown(
    workspace, tmp_path
):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run(["--image-tag", "abc1234"], repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inferenceScope" not in calls.read_text()
    assert "推論の範囲: 指定なし（parameters.ts の値か global）" in result.stdout


def test_scope_is_shown_before_the_first_cdk_deploy(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, _ = _ok_tools(tmp_path)
    result = _run(
        ["--image-tag", "abc1234", "--inference-scope", "global"], repo, ok_bin
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "推論の範囲: global" in result.stdout.split("Step 2")[0]


@pytest.mark.parametrize("value", ["us", "JP-East", ""])
def test_unknown_inference_scope_stops_before_any_aws_call(workspace, value):
    repo, bin_dir, calls = workspace
    result = _run(["--inference-scope", value], repo, bin_dir)
    assert result.returncode == 1
    assert "--inference-scope" in result.stdout + result.stderr
    assert calls.read_text() == ""


def test_help_lists_inference_scope(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--help"], repo, bin_dir)
    assert result.returncode == 0
    assert "--inference-scope" in result.stdout
    assert calls.read_text() == ""


# 閉域対応 U2（BR5.1）: --endpoint-az-count・--create-resolver-endpoint・--resolver-allowed-cidrs は
# --closed のときだけ、2 回の cdk deploy にコンテキストとして渡す

_U2_OPTIONS = [
    "--endpoint-az-count",
    "2",
    "--create-resolver-endpoint",
    "--resolver-allowed-cidrs",
    "10.1.0.0/16,10.2.0.0/16",
]
_U2_CONTEXT = [
    "-c closedEndpointAzCount=2",
    "-c closedCreateResolverEndpoint=true",
    "-c closedResolverAllowedCidrs=10.1.0.0/16,10.2.0.0/16",
]


def test_closed_network_options_reach_every_closed_cdk_deploy(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run([*_CLOSED, *_U2_OPTIONS], repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    deploys = _deploys(calls)
    assert len(deploys) == 2
    for context in _U2_CONTEXT:
        assert all(context in d for d in deploys), context


def test_closed_deploy_without_the_options_passes_no_u2_context(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, calls = _ok_tools(tmp_path)
    result = _run(_CLOSED, repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    text = calls.read_text()
    for key in (
        "closedEndpointAzCount",
        "closedCreateResolverEndpoint",
        "closedResolverAllowedCidrs",
    ):
        assert key not in text


@pytest.mark.parametrize(
    "option",
    [
        ["--endpoint-az-count", "1"],
        ["--create-resolver-endpoint"],
        ["--resolver-allowed-cidrs", "10.1.0.0/16"],
    ],
    ids=["az-count", "create-resolver", "resolver-cidrs"],
)
def test_closed_network_options_need_closed(workspace, option):
    repo, bin_dir, calls = workspace
    result = _run(option, repo, bin_dir)
    assert result.returncode != 0
    assert option[0] in result.stdout + result.stderr
    assert calls.read_text() == ""


@pytest.mark.parametrize("value", ["0", "3", "two", ""])
def test_endpoint_az_count_other_than_1_or_2_stops_before_any_aws_call(
    workspace, value
):
    repo, bin_dir, calls = workspace
    result = _run([*_CLOSED, "--endpoint-az-count", value], repo, bin_dir)
    assert result.returncode == 1
    assert "--endpoint-az-count" in result.stdout + result.stderr
    assert calls.read_text() == ""


def test_help_lists_the_closed_network_options(workspace):
    repo, bin_dir, calls = workspace
    result = _run(["--help"], repo, bin_dir)
    assert result.returncode == 0
    for option in (
        "--endpoint-az-count",
        "--create-resolver-endpoint",
        "--resolver-allowed-cidrs",
    ):
        assert option in result.stdout
    assert calls.read_text() == ""


def test_redeploy_command_includes_the_closed_network_options(workspace, tmp_path):
    repo, _, _ = workspace
    ok_bin, _ = _ok_tools(tmp_path)
    result = _run([*_CLOSED, *_U2_OPTIONS], repo, ok_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    redeploy = next(
        line for line in result.stdout.splitlines() if line.startswith("再デプロイ")
    )
    assert "--endpoint-az-count 2" in redeploy
    assert "--create-resolver-endpoint" in redeploy
    assert "--resolver-allowed-cidrs 10.1.0.0/16,10.2.0.0/16" in redeploy
