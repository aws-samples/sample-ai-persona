"""SourceInspector: the inspection of the head of an uploaded table (BR3.1〜BR3.4).

The inspector is a pure function over the first bytes of the file. It never
reads the whole file (NFR1.4).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.managers.shared.source_inspector import (
    SourceInspectionError,
    inspect_document,
    inspect_table_head,
)
from src.models.errors import ErrorCode
from src.models.source_file import (
    SAMPLE_CELL_MAX_CHARS,
    DocumentMetadata,
    SourceEncoding,
    SourceFormat,
)
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
    """U2 BR12.1: one code per way of fixing the file."""

    @pytest.mark.parametrize(
        "head",
        [
            b"",
            b"\n\n",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00",
        ],
    )
    def test_binary_or_empty_is_not_a_table(self, head: bytes) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_TABLE):
            _inspect(head)

    def test_a_line_longer_than_the_head_is_not_a_table(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_TABLE):
            _inspect(b"id,age,1,2,3", total=10_000)

    def test_an_empty_column_name(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_EMPTY_COLUMN_NAME):
            _inspect(b"a,,c\n1,2,3\n")

    def test_a_duplicated_column_name(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME):
            _inspect(b"a,a\n1,2\n")

    def test_undecodable_bytes_are_an_unknown_encoding(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_ENCODING_UNKNOWN):
            _inspect(b"a,b\n\xff\xfe\xfd\xfc,\x80\x80\n")

    def test_a_broken_bom_file_is_an_unknown_encoding(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_ENCODING_UNKNOWN):
            _inspect(b"\xef\xbb\xbfa,b\n\xff\xfe,1\n")

    def test_the_context_holds_no_file_content(self) -> None:
        with pytest.raises(SourceInspectionError) as exc:
            _inspect(b"secret,secret\n1,2\n")

        assert "secret" not in str(dict(exc.value.context))


# --- U4: documents (BR22.1〜BR22.3, AC3.3.1〜AC3.3.3) -------------------------


def _doc(data: bytes, fmt: SourceFormat, *, head_bytes=None, tail_bytes=None, **kw):
    head = data[:head_bytes] if head_bytes else data
    tail = data[-tail_bytes:] if tail_bytes else data
    return inspect_document(
        head,
        tail,
        source_format=fmt,
        total_size=kw.pop("total", len(data)),
        now=NOW,
        chars_per_mb=kw.pop("chars_per_mb", 20_000),
    )


class TestInspectPdf:
    def test_a_pdf_is_ready_with_an_estimate(self) -> None:
        from tests.document_fixtures import pdf_bytes

        data = pdf_bytes(["hello"])

        meta = _doc(data, SourceFormat.PDF, total=5_000_000, chars_per_mb=20_000)

        assert isinstance(meta, DocumentMetadata)
        assert meta.format is SourceFormat.PDF
        assert meta.estimated_chars == 100_000
        assert meta.encoding is None

    def test_the_signature_may_follow_some_bytes(self) -> None:
        from tests.document_fixtures import pdf_bytes

        data = b"\x00" * 500 + pdf_bytes(["hello"])

        assert _doc(data, SourceFormat.PDF).format is SourceFormat.PDF

    @pytest.mark.parametrize(
        "data", [b"just text, not a pdf", b"\x00" * 1100 + b"%PDF-1.4\n"]
    )
    def test_without_the_signature_near_the_start_it_is_not_a_pdf(self, data) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_DOCUMENT):
            _doc(data, SourceFormat.PDF)

    def test_an_encrypted_trailer_is_refused(self) -> None:
        from tests.document_fixtures import pdf_bytes

        data = pdf_bytes(["x" * 2000], encrypted=True)

        with raises_code(SourceInspectionError, ErrorCode.DOCUMENT_ENCRYPTED):
            _doc(data, SourceFormat.PDF, head_bytes=100, tail_bytes=300)

    def test_an_encrypt_word_in_the_body_is_not_a_password(self) -> None:
        from tests.document_fixtures import pdf_bytes

        data = pdf_bytes(["/Encrypt is only a word here"])

        assert _doc(data, SourceFormat.PDF).format is SourceFormat.PDF

    def test_an_encrypted_xref_stream_is_refused(self) -> None:
        data = (
            b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"9 0 obj\n<< /Type /XRef /Size 10 /Root 1 0 R /Encrypt 8 0 R "
            b"/Length 0 >>\nstream\n\nendstream\nendobj\nstartxref\n40\n%%EOF\n"
        )

        with raises_code(SourceInspectionError, ErrorCode.DOCUMENT_ENCRYPTED):
            _doc(data, SourceFormat.PDF)


class TestInspectDocx:
    def test_a_docx_with_its_document_part_is_ready(self) -> None:
        from tests.document_fixtures import docx_bytes

        data = docx_bytes([(1, "見出し"), "本文"])

        meta = _doc(data, SourceFormat.DOCX, total=2_000_000, chars_per_mb=100_000)

        assert meta.format is SourceFormat.DOCX
        assert meta.estimated_chars == 200_000

    def test_a_zip_without_the_document_part_is_not_a_docx(self) -> None:
        import io
        import zipfile

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("xl/workbook.xml", "<x/>")

        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_DOCUMENT):
            _doc(out.getvalue(), SourceFormat.DOCX)

    def test_an_ole_file_is_encrypted_or_old_word(self) -> None:
        from tests.document_fixtures import ole_bytes

        with raises_code(SourceInspectionError, ErrorCode.DOCUMENT_ENCRYPTED):
            _doc(ole_bytes(), SourceFormat.DOCX)

    def test_something_else_is_not_a_docx(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_DOCUMENT):
            _doc(b"%PDF-1.4 not a zip", SourceFormat.DOCX)

    def test_a_directory_outside_the_tail_is_left_to_the_preparation(self) -> None:
        from tests.document_fixtures import docx_bytes

        data = docx_bytes(["本文"])
        # pretend the file is far larger: the directory offset is then before
        # the tail that was read
        tail = data[-22:]
        meta = inspect_document(
            data[:100],
            tail,
            source_format=SourceFormat.DOCX,
            total_size=len(data),
            now=NOW,
            chars_per_mb=100_000,
        )

        assert meta.format is SourceFormat.DOCX


class TestInspectText:
    @pytest.mark.parametrize(
        ("encoding", "expected"),
        [
            ("utf-8", SourceEncoding.UTF8),
            ("cp932", SourceEncoding.CP932),
        ],
    )
    def test_text_gets_its_encoding_and_a_ratio_estimate(
        self, encoding, expected
    ) -> None:
        data = ("面談の記録です。\n" * 100).encode(encoding)

        meta = _doc(data, SourceFormat.TXT)

        assert meta.encoding is expected
        assert meta.estimated_chars == len("面談の記録です。\n" * 100)

    def test_a_cut_multibyte_tail_is_dropped(self) -> None:
        data = ("あ" * 10).encode("utf-8")  # no line break
        head = data[:16]  # 5 characters and one byte of the sixth

        meta = inspect_document(
            head,
            data[-16:],
            source_format=SourceFormat.MD,
            total_size=len(data) * 10,
            now=NOW,
            chars_per_mb=0,
        )

        # 5 characters in the 15 bytes kept: 300 bytes are about 100 characters
        assert meta.encoding is SourceEncoding.UTF8
        assert meta.estimated_chars == 100

    def test_nul_bytes_are_not_text(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_NOT_A_DOCUMENT):
            _doc(b"abc\x00def", SourceFormat.TXT)

    def test_undecodable_text_is_an_unknown_encoding(self) -> None:
        with raises_code(SourceInspectionError, ErrorCode.SOURCE_ENCODING_UNKNOWN):
            _doc(b"\xff\xfe\xfd\xfc\x80\x80\n", SourceFormat.TXT)

    def test_a_table_format_is_not_inspected_as_a_document(self) -> None:
        with pytest.raises(ValueError):
            _doc(b"a,b\n", SourceFormat.CSV)
