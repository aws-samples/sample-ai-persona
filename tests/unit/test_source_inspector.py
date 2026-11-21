"""SourceInspector: the inspection of the head of an uploaded table (BR3.1〜BR3.4).

The inspector is a pure function over the first bytes of the file. It never
reads the whole file (NFR1.4).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.managers.shared.source_inspector import (
    SourceInspectionError,
    inspect_table_head,
)
from src.models.errors import ErrorCode
from src.models.source_file import SAMPLE_CELL_MAX_CHARS, SourceEncoding, SourceFormat
from tests.error_helpers import raises_code

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
JAPANESE_CSV = (
    "顧客ID,年齢,地域,購入額\n1,34,東京,1200.5\n2,28,大阪,800\n3,45,名古屋,99\n"
)


def _inspect(head: bytes, *, fmt=SourceFormat.CSV, total=None):
    return inspect_table_head(
        head,
        source_format=fmt,
        total_size=total if total is not None else len(head),
        now=NOW,
    )


class TestFormatAndColumns:
    def test_csv_columns_types_and_rows(self) -> None:
        meta = _inspect(JAPANESE_CSV.encode("utf-8"))

        assert meta.format is SourceFormat.CSV
        assert meta.delimiter == ","
        assert [c.name for c in meta.columns] == ["顧客ID", "年齢", "地域", "購入額"]
        assert [c.inferred_type for c in meta.columns] == [
            "integer",
            "integer",
            "string",
            "number",
        ]
        assert meta.estimated_rows == 3
        assert meta.sample_rows[0] == ("1", "34", "東京", "1200.5")
        assert meta.inspected_bytes == len(JAPANESE_CSV.encode("utf-8"))
        assert meta.inspected_at == NOW

    def test_tsv_uses_tab(self) -> None:
        text = "id\tname\n1\ta\n2\tb\n"

        meta = _inspect(text.encode("utf-8"), fmt=SourceFormat.TSV)

        assert meta.delimiter == "\t"
        assert [c.name for c in meta.columns] == ["id", "name"]

    def test_sample_is_cut_to_five_rows_and_cell_length(self) -> None:
        long_cell = "x" * (SAMPLE_CELL_MAX_CHARS + 50)
        rows = "".join(f"{i},{long_cell}\n" for i in range(20))

        meta = _inspect(("n,text\n" + rows).encode("utf-8"))

        assert len(meta.sample_rows) == 5
        assert len(meta.sample_rows[0][1]) == SAMPLE_CELL_MAX_CHARS
        assert meta.estimated_rows == 20

    def test_estimated_rows_scale_with_the_whole_size(self) -> None:
        body = "".join(f"{i},a\n" for i in range(100))
        data = ("n,v\n" + body).encode("utf-8")
        head = data[:200]  # cut in the middle of a line

        meta = _inspect(head, total=len(data) * 10)

        # Only whole lines of the head are used; the estimate is scaled up.
        assert meta.estimated_rows > 100
        assert all(len(row) == 2 for row in meta.sample_rows)


class TestEncoding:
    def test_bom_is_utf8_sig(self) -> None:
        meta = _inspect(b"\xef\xbb\xbf" + JAPANESE_CSV.encode("utf-8"))

        assert meta.encoding is SourceEncoding.UTF8_SIG
        assert meta.columns[0].name == "顧客ID"

    def test_utf8(self) -> None:
        assert _inspect(JAPANESE_CSV.encode("utf-8")).encoding is SourceEncoding.UTF8

    def test_cp932(self) -> None:
        meta = _inspect(JAPANESE_CSV.encode("cp932"))

        assert meta.encoding is SourceEncoding.CP932
        assert meta.columns[2].name == "地域"

    def test_euc_jp_when_not_cp932(self) -> None:
        # "髙" style text decodes as EUC-JP but its bytes are not valid CP932.
        text = "名前,地域\nあいう,東京\n"
        data = text.encode("euc-jp")
        try:
            data.decode("cp932")
            pytest.skip("this sample also decodes as CP932")
        except UnicodeDecodeError:
            pass

        assert _inspect(data).encoding is SourceEncoding.EUC_JP

    def test_ascii_only_is_undetermined(self) -> None:
        meta = _inspect(b"id,age\n1,30\n2,40\n")

        assert meta.encoding is SourceEncoding.UNDETERMINED


class TestNotATable:
    @pytest.mark.parametrize(
        "head",
        [
            b"",
            b"\n\n",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00",
            b"a,,c\n1,2,3\n",  # an empty header name
            b"a,a\n1,2\n",  # a duplicated header name
        ],
    )
    def test_is_upload_source_invalid(self, head: bytes) -> None:
        with raises_code(SourceInspectionError, ErrorCode.UPLOAD_SOURCE_INVALID):
            _inspect(head)

    def test_undecodable_bytes_are_invalid(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.UPLOAD_SOURCE_INVALID):
            _inspect(b"a,b\n\xff\xfe\xfd\xfc,\x80\x80\n")
