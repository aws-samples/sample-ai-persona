"""scripts/export_nemotron_parquet.py のテスト（閉域対応 U3 BR6.1）。

Hugging Face の取得（SurveyBatchService.download_nemotron_dataset）はモックにし、
tmp_path に書く。S3 と AWS には触れない。
"""

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from scripts import export_nemotron_parquet as script

pytestmark = pytest.mark.unit


def _parquet(rows: int = 3) -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame({"persona": [f"p{i}" for i in range(rows)]}).write_parquet(buffer)
    return buffer.getvalue()


@pytest.fixture
def no_aws():
    """Any boto3 client creation fails the test (the script must not touch AWS)."""
    with patch("boto3.client", side_effect=AssertionError("AWS was called")) as client:
        yield client


def test_writes_the_file_and_reports_rows_size_and_sha256(tmp_path, no_aws, capsys):
    data = _parquet(rows=3)
    output = tmp_path / "nemotron.parquet"
    with patch.object(
        script.SurveyBatchService, "download_nemotron_dataset", return_value=data
    ) as download:
        code = script.main(["--output", str(output)])
    assert code == 0
    download.assert_called_once()
    assert output.read_bytes() == data
    out = capsys.readouterr().out
    assert "rows: 3" in out
    assert f"bytes: {len(data)}" in out
    assert f"sha256: {hashlib.sha256(data).hexdigest()}" in out
    no_aws.assert_not_called()


def test_export_returns_the_numbers(tmp_path):
    data = _parquet(rows=5)
    result = script.export(tmp_path / "n.parquet", download=lambda: data)
    assert result.row_count == 5
    assert result.size_bytes == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()


def test_missing_parent_directory_writes_nothing(tmp_path, no_aws):
    output = tmp_path / "missing" / "n.parquet"
    with patch.object(
        script.SurveyBatchService, "download_nemotron_dataset"
    ) as download:
        code = script.main(["--output", str(output)])
    assert code != 0
    assert not output.parent.exists()
    download.assert_not_called()


def test_existing_file_is_kept_without_force(tmp_path, no_aws):
    output = tmp_path / "n.parquet"
    output.write_bytes(b"old")
    with patch.object(
        script.SurveyBatchService, "download_nemotron_dataset"
    ) as download:
        code = script.main(["--output", str(output)])
    assert code != 0
    assert output.read_bytes() == b"old"
    download.assert_not_called()


def test_force_overwrites(tmp_path, no_aws):
    output = tmp_path / "n.parquet"
    output.write_bytes(b"old")
    data = _parquet()
    with patch.object(
        script.SurveyBatchService, "download_nemotron_dataset", return_value=data
    ):
        code = script.main(["--output", str(output), "--force"])
    assert code == 0
    assert output.read_bytes() == data


def test_download_failure_leaves_no_partial_file(tmp_path, no_aws):
    output = tmp_path / "n.parquet"
    with patch.object(
        script.SurveyBatchService,
        "download_nemotron_dataset",
        side_effect=RuntimeError("network down"),
    ):
        code = script.main(["--output", str(output)])
    assert code != 0
    assert list(tmp_path.iterdir()) == []


def test_unreadable_parquet_leaves_no_partial_file(tmp_path):
    output = tmp_path / "n.parquet"
    with pytest.raises(script.ExportError):
        script.export(output, download=lambda: b"not a parquet file")
    assert list(Path(tmp_path).iterdir()) == []
