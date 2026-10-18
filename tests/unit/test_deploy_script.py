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
