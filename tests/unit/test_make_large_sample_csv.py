"""scripts/make_large_sample_csv.py: a made-up Shift_JIS CSV for the 1GB E2E."""

import csv
import io
from pathlib import Path

import pytest

from scripts import make_large_sample_csv as gen

pytestmark = pytest.mark.unit


def _read_back(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("cp932")
    return list(csv.reader(io.StringIO(text)))


class TestWriteCsv:
    def test_reads_back_as_shift_jis_with_the_rows_and_columns_asked_for(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "big.csv"

        written = gen.write_csv(str(out), rows=250, columns=12)

        table = _read_back(out)
        assert written.rows == 250
        assert len(table) == 251  # header + rows
        assert all(len(row) == 12 for row in table)
        assert out.stat().st_size == written.bytes

    def test_is_not_utf8(self, tmp_path: Path) -> None:
        out = tmp_path / "big.csv"

        gen.write_csv(str(out), rows=50, columns=8)

        with pytest.raises(UnicodeDecodeError):
            out.read_bytes().decode("utf-8")

    def test_same_seed_gives_the_same_file(self, tmp_path: Path) -> None:
        gen.write_csv(str(tmp_path / "a.csv"), rows=100, columns=9, seed=7)
        gen.write_csv(str(tmp_path / "b.csv"), rows=100, columns=9, seed=7)

        assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()

    def test_it_writes_a_little_at_a_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sizes: list[int] = []
        real_open = open

        class _Spy:
            def __init__(self, handle):
                self._handle = handle

            def write(self, data: bytes) -> int:
                sizes.append(len(data))
                return self._handle.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        monkeypatch.setattr(
            gen, "_open_binary", lambda path: _Spy(real_open(path, "wb")), raising=True
        )

        gen.write_csv(str(tmp_path / "c.csv"), rows=1000, columns=8, chunk_rows=100)

        assert len(sizes) >= 10
        assert max(sizes) < sum(sizes)

    def test_size_mb_writes_until_the_size_is_reached(self, tmp_path: Path) -> None:
        out = tmp_path / "s.csv"

        written = gen.write_csv(str(out), size_mb=1, columns=10, chunk_rows=2000)

        assert out.stat().st_size >= 1024 * 1024
        assert out.stat().st_size < 1024 * 1024 + 2000 * 400
        assert written.rows > 1000

    def test_needs_rows_or_size(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            gen.write_csv(str(tmp_path / "x.csv"), columns=8)

    def test_too_few_columns_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            gen.write_csv(str(tmp_path / "x.csv"), rows=10, columns=2)

    def test_main_prints_a_summary_and_writes_the_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "m.csv"

        code = gen.main(["--rows", "30", "--columns", "7", "--out", str(out)])

        assert code == 0
        assert out.is_file()
        assert "30" in capsys.readouterr().out
