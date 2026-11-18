"""shared/ ユーティリティのテスト。

insight_utils, document_loader (public化メソッド) をテストする。
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from src.managers.shared.document_loader import (
    build_content_block,
    is_supported_mime_type,
    is_image_type,
    SUPPORTED_IMAGE_TYPES,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_MIME_TYPES,
)
from src.managers.shared.file_utils import (
    MODEL_IMAGE_MAX_BYTES,
    MODEL_IMAGE_MAX_SIDE,
    fit_image_for_model,
    detect_encoding,
    extract_text_from_bytes,
    get_csv_preview,
    build_results_csv_bytes,
    parse_results_csv,
    analyze_csv_schema,
    detect_binding_key,
    infer_behavior_data_type,
    parse_csv_first_row_with_mapping,
)
from src.managers.shared.insight_utils import (
    attach_insights_to_discussion,
    normalize_and_deduplicate_insights,
    save_categories_to_config,
    get_default_insight_categories,
)
from src.models.dataset import DatasetColumn
from src.models.discussion import Discussion
from src.models.insight_category import InsightCategory


@pytest.mark.unit
class TestInsightUtils:
    """insight_utils のテスト"""

    def test_attach_insights_success(self):
        """インサイト付与が成功すること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        # U6: 発言は本体の外に置くため、別に渡す
        from src.models.message import Message

        messages = [
            Message.create_new(
                persona_id="p1",
                persona_name="太郎",
                content="テスト",
                message_type="statement",
            ),
            Message.create_new(
                persona_id="p2",
                persona_name="花子",
                content="テスト2",
                message_type="statement",
            ),
        ]

        mock_ai_service = Mock()
        mock_ai_service.extract_insights.return_value = [
            {
                "category": "ニーズ",
                "description": "ユーザーは価格よりも品質を重視する傾向がある",
                "supporting_messages": [],
                "confidence_score": 0.8,
            }
        ]

        result = attach_insights_to_discussion(
            discussion=discussion,
            messages=messages,
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        mock_ai_service.extract_insights.assert_called_once_with(
            messages, categories=None, topic="テスト議論"
        )
        assert len(result.insights) == 1
        assert result.insights[0].category == "ニーズ"

    def test_attach_insights_failure_returns_discussion_unchanged(self):
        """AI失敗時にインサイトなしの議論がそのまま返ること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )

        mock_ai_service = Mock()
        mock_ai_service.extract_insights.side_effect = Exception("AI error")

        result = attach_insights_to_discussion(
            discussion=discussion,
            messages=[],
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        assert result.insights == []

    def test_save_categories_to_config(self):
        """カテゴリーがagent_configに保存されること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )
        categories = [
            InsightCategory(name="ニーズ", description="ユーザーのニーズ"),
            InsightCategory(name="課題", description="課題や不満"),
        ]

        result = save_categories_to_config(discussion, categories)

        assert result.agent_config is not None
        assert "insight_categories" in result.agent_config
        assert len(result.agent_config["insight_categories"]) == 2

    def test_save_categories_empty_returns_unchanged(self):
        """空カテゴリーの場合、議論がそのまま返ること"""
        discussion = Discussion.create_new(
            topic="テスト議論", participants=["p1", "p2"]
        )

        result = save_categories_to_config(discussion, [])

        assert result.agent_config is None or "insight_categories" not in (
            result.agent_config or {}
        )

    def test_get_default_insight_categories(self):
        """デフォルトカテゴリーが取得できること"""
        categories = get_default_insight_categories()

        assert len(categories) > 0
        assert all(isinstance(c, InsightCategory) for c in categories)


@pytest.mark.unit
class TestNormalizeAndDeduplicateInsights:
    """normalize_and_deduplicate_insights のテスト"""

    def test_category_normalization(self):
        """カテゴリ名が部分一致で正規化されること"""
        categories = [
            InsightCategory(name="ユーザーニーズ", description=""),
            InsightCategory(name="課題", description=""),
        ]
        raw = [
            {
                "category": "ニーズ",
                "description": "テスト用のインサイト説明文です",
                "confidence_score": 0.8,
            }
        ]

        result = normalize_and_deduplicate_insights(raw, categories)

        assert len(result) == 1
        assert result[0]["category"] == "ユーザーニーズ"

    def test_confidence_clamping(self):
        """confidence scoreが0.0-1.0にクランプされること"""
        raw = [
            {
                "category": "テスト",
                "description": "十分に長い説明文テストです",
                "confidence_score": 1.5,
            },
            {
                "category": "テスト",
                "description": "もう一つの十分に長い説明文です",
                "confidence_score": -0.3,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert result[0]["confidence_score"] == 1.0
        assert result[1]["confidence_score"] == 0.0

    def test_short_description_filtered(self):
        """短い説明文がフィルタされること"""
        raw = [
            {"category": "テスト", "description": "短い", "confidence_score": 0.8},
            {
                "category": "テスト",
                "description": "これは十分に長い説明文のテストです",
                "confidence_score": 0.8,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert len(result) == 1
        assert "十分に長い" in result[0]["description"]

    def test_deduplication(self):
        """重複するdescriptionが除去されること"""
        raw = [
            {
                "category": "A",
                "description": "同じ説明文のインサイトです",
                "confidence_score": 0.8,
            },
            {
                "category": "B",
                "description": "同じ説明文のインサイトです",
                "confidence_score": 0.9,
            },
            {
                "category": "C",
                "description": "異なる説明文のインサイトです",
                "confidence_score": 0.7,
            },
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert len(result) == 2
        assert result[0]["category"] == "A"
        assert result[1]["category"] == "C"

    def test_invalid_confidence_defaults(self):
        """無効なconfidence値がデフォルト0.5になること"""
        raw = [
            {
                "category": "テスト",
                "description": "十分に長い説明文テストです",
                "confidence_score": "invalid",
            }
        ]

        result = normalize_and_deduplicate_insights(raw, None)

        assert result[0]["confidence_score"] == 0.5

    def test_empty_input(self):
        """空入力で空リストが返ること"""
        result = normalize_and_deduplicate_insights([], None)
        assert result == []


@pytest.mark.unit
class TestDocumentLoaderPublic:
    """document_loader の public 化メソッドのテスト"""

    def test_build_content_block_png(self):
        """PNG画像のContentBlockが正しく構築されること"""
        result = build_content_block(b"\x89PNG", "image/png", "test.png")

        assert result is not None
        assert "image" in result
        assert result["image"]["format"] == "png"
        assert result["image"]["source"]["bytes"] == b"\x89PNG"

    def test_build_content_block_jpeg(self):
        """JPEG画像のContentBlockが正しく構築されること"""
        result = build_content_block(b"\xff\xd8", "image/jpeg", "photo.jpg")

        assert result is not None
        assert result["image"]["format"] == "jpeg"

    def test_build_content_block_pdf(self):
        """PDFのContentBlockが正しく構築されること"""
        result = build_content_block(b"%PDF-1.4", "application/pdf", "doc.pdf")

        assert result is not None
        assert "document" in result
        assert result["document"]["format"] == "pdf"
        assert result["document"]["name"] == "doc"

    def test_build_content_block_text(self):
        """テキストのContentBlockが正しく構築されること"""
        result = build_content_block(b"hello", "text/plain", "note.txt")

        assert result is not None
        assert result["document"]["format"] == "txt"

    def test_build_content_block_csv(self):
        """CSVのContentBlockが正しく構築されること"""
        result = build_content_block(b"a,b,c", "text/csv", "data.csv")

        assert result is not None
        assert result["document"]["format"] == "csv"

    def test_build_content_block_unsupported_mime(self):
        """未サポートMIMEでNoneが返ること"""
        result = build_content_block(b"data", "application/zip", "archive.zip")

        assert result is None

    def test_build_content_block_filename_sanitize(self):
        """特殊文字を含むファイル名がサニタイズされること"""
        result = build_content_block(b"%PDF", "application/pdf", "日本語ファイル名.pdf")

        assert result is not None
        name = result["document"]["name"]
        assert len(name) <= 100

    def test_is_supported_mime_type_valid(self):
        """サポートMIMEでTrueが返ること"""
        assert is_supported_mime_type("image/png") is True
        assert is_supported_mime_type("application/pdf") is True
        assert is_supported_mime_type("text/csv") is True

    def test_is_supported_mime_type_invalid(self):
        """未サポートMIMEでFalseが返ること"""
        assert is_supported_mime_type("application/zip") is False
        assert is_supported_mime_type("video/mp4") is False
        assert is_supported_mime_type("") is False

    def test_is_image_type(self):
        """画像MIMEの判定が正しいこと"""
        assert is_image_type("image/png") is True
        assert is_image_type("image/jpeg") is True
        assert is_image_type("application/pdf") is False
        assert is_image_type("text/plain") is False

    def test_mime_constants(self):
        """MIME定数が正しく定義されていること"""
        assert "image/png" in SUPPORTED_IMAGE_TYPES
        assert "application/pdf" in SUPPORTED_DOCUMENT_TYPES
        assert len(SUPPORTED_MIME_TYPES) == len(SUPPORTED_IMAGE_TYPES) + len(
            SUPPORTED_DOCUMENT_TYPES
        )


# ---------- file_utils テスト ----------


@pytest.mark.unit
class TestDetectEncoding:
    """detect_encoding のテスト"""

    def test_utf8(self):
        """UTF-8が正しく検出されること"""
        content = "こんにちは世界".encode("utf-8")
        assert detect_encoding(content) == "utf-8"

    def test_shift_jis(self):
        """Shift_JISが正しく検出されること"""
        # UTF-8でデコード不可なShift_JIS固有バイト列
        content = b"\x83\x65\x83\x58\x83\x67"  # テスト in Shift_JIS
        assert detect_encoding(content) == "shift_jis"

    def test_euc_jp(self):
        """EUC-JPが正しく検出されること"""
        # EUC-JP固有のバイト列（UTF-8でもShift_JISでも不正）
        content = b"\xa4\xb3\xa4\xf3\xa4\xcb\xa4\xc1\xa4\xcf"  # こんにちは in EUC-JP
        # UTF-8でもShift_JISでもデコードできないEUC-JP固有パターン
        # detect_encodingはutf-8→shift_jis→euc-jpの順で試行する
        # 上記バイト列がutf-8, shift_jisで失敗しeuc-jpで成功することを確認
        result = detect_encoding(content)
        assert result == "euc-jp"

    def test_unknown_encoding_raises(self):
        """検出不能なバイト列でValueErrorが送出されること"""
        # 全エンコーディングでデコード不能なバイト列
        content = b"\xff\xfe\x00\x01\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x8b\x8c\x8d\x8e\x8f\x90\x91\x92\x93\x94\x95\x96\x97\x98\x99\x9a\x9b\x9c\x9d\x9e\x9f"
        with pytest.raises(ValueError, match="エンコーディングを検出できません"):
            detect_encoding(content)

    def test_ascii_detected_as_utf8(self):
        """ASCIIテキストがUTF-8として検出されること"""
        content = b"hello world"
        assert detect_encoding(content) == "utf-8"


@pytest.mark.unit
class TestExtractTextFromBytes:
    """extract_text_from_bytes のテスト"""

    def test_txt_file(self):
        """テキストファイルがデコードされること"""
        content = "テストテキスト".encode("utf-8")
        result = extract_text_from_bytes(content, "test.txt")
        assert result == "テストテキスト"

    def test_md_file(self):
        """Markdownファイルがデコードされること"""
        content = "# 見出し\n本文".encode("utf-8")
        result = extract_text_from_bytes(content, "readme.md")
        assert result == "# 見出し\n本文"

    def test_csv_file(self):
        """CSVファイルがデコードされること"""
        content = "名前,年齢\n太郎,30".encode("utf-8")
        result = extract_text_from_bytes(content, "data.csv")
        assert result == "名前,年齢\n太郎,30"

    @patch("markitdown.MarkItDown")
    def test_pdf_uses_markitdown(self, mock_markitdown_class):
        """PDFファイルでmarkitdownが使用されること"""
        mock_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "PDF content here"
        mock_instance.convert_stream.return_value = mock_result
        mock_markitdown_class.return_value = mock_instance

        content = b"%PDF-1.4 fake content"
        result = extract_text_from_bytes(content, "document.pdf")

        assert result == "PDF content here"
        mock_markitdown_class.assert_called_once()
        mock_instance.convert_stream.assert_called_once()

    @patch("markitdown.MarkItDown")
    def test_docx_uses_markitdown(self, mock_markitdown_class):
        """DOCXファイルでmarkitdownが使用されること"""
        mock_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "Word document text"
        mock_instance.convert_stream.return_value = mock_result
        mock_markitdown_class.return_value = mock_instance

        content = b"PK\x03\x04 fake docx"
        result = extract_text_from_bytes(content, "report.docx")

        assert result == "Word document text"

    def test_txt_with_uppercase_extension(self):
        """大文字拡張子でもテキストとして処理されること"""
        content = "uppercase test".encode("utf-8")
        result = extract_text_from_bytes(content, "FILE.TXT")
        assert result == "uppercase test"


@pytest.mark.unit
class TestGetCsvPreview:
    """get_csv_preview のテスト"""

    def test_short_csv(self):
        """行数がmax_lines以下の場合、全行が返ること"""
        content = "a,b\n1,2\n3,4".encode("utf-8")
        result = get_csv_preview(content, max_lines=20)
        assert result == "a,b\n1,2\n3,4"
        assert "..." not in result

    def test_long_csv_truncated(self):
        """行数がmax_linesを超える場合、切り詰められ行数が表示されること"""
        lines = ["header"] + [f"row{i}" for i in range(30)]
        content = "\n".join(lines).encode("utf-8")
        result = get_csv_preview(content, max_lines=5)
        assert "row0" in result
        assert "row4" not in result  # 6行目以降は含まれない（header + row0~row3 = 5行）
        assert f"... (全{len(lines)}行)" in result

    def test_exact_max_lines(self):
        """行数がちょうどmax_linesの場合、省略表示がないこと"""
        lines = [f"line{i}" for i in range(5)]
        content = "\n".join(lines).encode("utf-8")
        result = get_csv_preview(content, max_lines=5)
        assert "..." not in result
        assert "line4" in result

    def test_default_max_lines(self):
        """デフォルトのmax_linesが20であること"""
        lines = [f"line{i}" for i in range(25)]
        content = "\n".join(lines).encode("utf-8")
        result = get_csv_preview(content)
        assert "line19" in result
        assert "line20" not in result
        assert "... (全25行)" in result


@pytest.mark.unit
class TestBuildResultsCsvBytes:
    """build_results_csv_bytes のテスト"""

    def test_basic_csv(self):
        """基本的なCSVが正しく構築されること"""
        headers = ["名前", "年齢"]
        rows = [["太郎", "30"], ["花子", "25"]]
        result = build_results_csv_bytes(headers, rows)

        # BOM付きUTF-8であること
        assert result.startswith(b"\xef\xbb\xbf")

        # デコードして内容確認
        text = result.decode("utf-8-sig")
        assert '"名前"' in text
        assert '"年齢"' in text
        assert '"太郎"' in text
        assert '"30"' in text

    def test_bom_prefix(self):
        """BOM付きUTF-8であること"""
        result = build_results_csv_bytes(["a"], [["1"]])
        assert result[:3] == b"\xef\xbb\xbf"

    def test_quote_all(self):
        """全フィールドがクォートされていること"""
        result = build_results_csv_bytes(["col"], [["value"]])
        text = result.decode("utf-8-sig")
        assert '"col"' in text
        assert '"value"' in text

    def test_empty_rows(self):
        """行が空でもヘッダーのみのCSVが生成されること"""
        result = build_results_csv_bytes(["h1", "h2"], [])
        text = result.decode("utf-8-sig")
        lines = text.strip().splitlines()
        assert len(lines) == 1
        assert '"h1"' in lines[0]

    def test_special_characters(self):
        """カンマやクォートを含む値が正しくエスケープされること"""
        headers = ["data"]
        rows = [['value, with "quotes"']]
        result = build_results_csv_bytes(headers, rows)
        text = result.decode("utf-8-sig")
        # CSV内でダブルクォートがエスケープされていること
        assert '""quotes""' in text


@pytest.mark.unit
class TestParseResultsCsv:
    """parse_results_csv のテスト"""

    def test_basic_parse(self):
        """基本的なCSVが正しくパースされること"""
        headers = ["名前", "年齢", "職業"]
        rows = [["太郎", "30", "エンジニア"], ["花子", "25", "デザイナー"]]
        csv_bytes = build_results_csv_bytes(headers, rows)

        result = parse_results_csv(csv_bytes)

        assert len(result) == 2
        assert result[0]["名前"] == "太郎"
        assert result[0]["年齢"] == "30"
        assert result[1]["職業"] == "デザイナー"

    def test_roundtrip(self):
        """build_results_csv_bytes → parse_results_csv のラウンドトリップが一致すること"""
        headers = ["a", "b", "c"]
        rows = [["1", "2", "3"], ["x", "y", "z"]]
        csv_bytes = build_results_csv_bytes(headers, rows)
        parsed = parse_results_csv(csv_bytes)

        assert len(parsed) == 2
        assert parsed[0] == {"a": "1", "b": "2", "c": "3"}
        assert parsed[1] == {"a": "x", "b": "y", "c": "z"}

    def test_empty_csv(self):
        """ヘッダーのみのCSVで空リストが返ること"""
        csv_bytes = build_results_csv_bytes(["h1"], [])
        result = parse_results_csv(csv_bytes)
        assert result == []

    def test_without_bom(self):
        """BOMなしCSVもパースできること"""
        csv_text = "col1,col2\nval1,val2\n"
        csv_bytes = csv_text.encode("utf-8")
        result = parse_results_csv(csv_bytes)
        assert len(result) == 1
        assert result[0]["col1"] == "val1"


@pytest.mark.unit
class TestAnalyzeCsvSchema:
    """analyze_csv_schema のテスト"""

    def _make_csv(self, header: str, rows: list[str]) -> bytes:
        """BOM付きCSVバイト列を生成するヘルパー"""
        content = header + "\n" + "\n".join(rows)
        return content.encode("utf-8-sig")

    def test_integer_columns(self):
        """整数カラムがintegerとして推定されること"""
        csv_bytes = self._make_csv("id,count", ["1,100", "2,200", "3,300"])
        columns, row_count = analyze_csv_schema(csv_bytes)

        assert len(columns) == 2
        assert columns[0].name == "id"
        assert columns[0].data_type == "integer"
        assert columns[1].data_type == "integer"
        assert row_count == 3

    def test_float_columns(self):
        """浮動小数カラムがfloatとして推定されること"""
        csv_bytes = self._make_csv("price,rate", ["1.5,0.08", "2.3,0.12"])
        columns, _ = analyze_csv_schema(csv_bytes)

        assert columns[0].data_type == "float"
        assert columns[1].data_type == "float"

    def test_date_columns(self):
        """日付カラムがdateとして推定されること"""
        csv_bytes = self._make_csv(
            "created_at", ["2024-01-15", "2024-02-20", "2024-03-10"]
        )
        columns, _ = analyze_csv_schema(csv_bytes)

        assert columns[0].data_type == "date"

    def test_string_columns(self):
        """文字列カラムがstringとして推定されること"""
        csv_bytes = self._make_csv(
            "name,email", ["太郎,taro@test.com", "花子,hanako@test.com"]
        )
        columns, _ = analyze_csv_schema(csv_bytes)

        assert columns[0].data_type == "string"
        assert columns[1].data_type == "string"

    def test_blank_column_is_string(self):
        """値が空白だけのカラムはstringとして推定されること"""
        csv_bytes = self._make_csv("id,memo", ["1,", "2,  "])
        columns, _ = analyze_csv_schema(csv_bytes)

        assert [c.data_type for c in columns] == ["integer", "string"]

    def test_empty_csv(self):
        """空CSVで空リストと0が返ること"""
        csv_bytes = "".encode("utf-8-sig")
        columns, row_count = analyze_csv_schema(csv_bytes)
        assert columns == []
        assert row_count == 0

    def test_row_count(self):
        """行数が正しくカウントされること（sample_rowsを超える場合含む）"""
        rows = [f"{i},val{i}" for i in range(150)]
        csv_bytes = self._make_csv("id,name", rows)
        columns, row_count = analyze_csv_schema(csv_bytes, sample_rows=100)

        # sample_rows を超えた行も含めて全行を数える
        assert row_count == 150
        assert len(columns) == 2

    def test_blank_lines_are_not_rows(self):
        """Blank lines (which DuckDB skips) are not counted as rows."""
        csv_bytes = "id,name\n1,a\n\n2,b\n\n".encode("utf-8")
        _, row_count = analyze_csv_schema(csv_bytes)

        assert row_count == 2

    def test_returns_dataset_column_instances(self):
        """DatasetColumnインスタンスが返ること"""
        csv_bytes = self._make_csv("col1", ["abc"])
        columns, _ = analyze_csv_schema(csv_bytes)

        assert isinstance(columns[0], DatasetColumn)
        assert columns[0].name == "col1"


@pytest.mark.unit
class TestDetectBindingKey:
    """detect_binding_key のテスト"""

    def _make_csv_bytes(self, header: str, row: str) -> bytes:
        content = f"{header}\n{row}"
        return content.encode("utf-8-sig")

    def test_user_id_detected(self):
        """user_idカラムが検出されること"""
        csv_bytes = self._make_csv_bytes("user_id,name,age", "U001,太郎,30")
        key_col, value = detect_binding_key(["user_id", "name", "age"], csv_bytes)

        assert key_col == "user_id"
        assert value == "U001"

    def test_customer_id_detected(self):
        """customer_idカラムが検出されること"""
        csv_bytes = self._make_csv_bytes("customer_id,product", "C123,商品A")
        key_col, value = detect_binding_key(["customer_id", "product"], csv_bytes)

        assert key_col == "customer_id"
        assert value == "C123"

    def test_no_id_column(self):
        """IDカラムがない場合、空文字列タプルが返ること"""
        csv_bytes = self._make_csv_bytes("name,age", "太郎,30")
        key_col, value = detect_binding_key(["name", "age"], csv_bytes)

        assert key_col == ""
        assert value == ""

    def test_case_insensitive(self):
        """カラム名の大小文字を無視して検出されること"""
        csv_bytes = self._make_csv_bytes("User_ID,name", "U999,花子")
        key_col, value = detect_binding_key(["User_ID", "name"], csv_bytes)

        assert key_col == "User_ID"
        assert value == "U999"

    def test_priority_order(self):
        """候補の優先順位に従って最初にマッチしたものが返ること"""
        csv_bytes = self._make_csv_bytes("uid,customer_id,value", "uid1,cid1,100")
        # id_candidates = ["user_id", "customer_id", "member_id", "uid", "cid"]
        # customer_idが先に見つかる
        key_col, value = detect_binding_key(["uid", "customer_id", "value"], csv_bytes)

        assert key_col == "customer_id"
        assert value == "cid1"


@pytest.mark.unit
class TestInferBehaviorDataType:
    """infer_behavior_data_type のテスト"""

    def test_purchase_history(self):
        """購買系カラムで購買履歴と推定されること"""
        columns = ["user_id", "purchase_date", "order_amount", "product_name"]
        result = infer_behavior_data_type(columns)
        assert result == "購買履歴"

    def test_web_behavior(self):
        """Web行動系カラムでWeb行動ログと推定されること"""
        columns = ["session_id", "page_url", "click_count", "timestamp"]
        result = infer_behavior_data_type(columns)
        assert result == "Web行動ログ"

    def test_inquiry_history(self):
        """問い合わせ系カラムで問い合わせ履歴と推定されること"""
        columns = ["ticket_id", "inquiry_date", "support_category"]
        result = infer_behavior_data_type(columns)
        assert result == "問い合わせ履歴"

    def test_no_match(self):
        """マッチするキーワードがない場合、空文字列が返ること"""
        columns = ["id", "name", "age", "address"]
        result = infer_behavior_data_type(columns)
        assert result == ""

    def test_empty_columns(self):
        """空のカラムリストで空文字列が返ること"""
        result = infer_behavior_data_type([])
        assert result == ""

    def test_majority_wins(self):
        """最多ヒットの種別が選ばれること"""
        # 購買系2つ、Web系1つ → 購買履歴
        columns = ["purchase_amount", "order_date", "page_view"]
        result = infer_behavior_data_type(columns)
        assert result == "購買履歴"


@pytest.mark.unit
class TestParseCsvFirstRowWithMapping:
    """parse_csv_first_row_with_mapping のテスト"""

    def _make_csv_bytes(self, header: str, row: str) -> bytes:
        content = f"{header}\n{row}"
        return content.encode("utf-8-sig")

    def test_basic_mapping(self):
        """マッピングに基づいてカラム名が変換されること"""
        csv_bytes = self._make_csv_bytes("氏名,年齢,住所", "太郎,30,東京")
        mapping = {"name": "氏名", "age": "年齢", "address": "住所"}

        result = parse_csv_first_row_with_mapping(csv_bytes, mapping)

        assert result["name"] == "太郎"
        assert result["age"] == "30"
        assert result["address"] == "東京"

    def test_unmapped_columns_keep_original_name(self):
        """マッピングにないカラムは元のカラム名で返ること"""
        csv_bytes = self._make_csv_bytes("name,extra", "太郎,値")
        mapping = {"standard_name": "name"}

        result = parse_csv_first_row_with_mapping(csv_bytes, mapping)

        assert result["standard_name"] == "太郎"
        assert result["extra"] == "値"

    def test_empty_csv(self):
        """データ行がないCSVで空辞書が返ること"""
        csv_bytes = "col1,col2\n".encode("utf-8-sig")
        result = parse_csv_first_row_with_mapping(csv_bytes, {"a": "col1"})
        assert result == {}

    def test_empty_values(self):
        """空の値が空文字列として返ること"""
        csv_bytes = self._make_csv_bytes("a,b", ",")
        mapping = {"x": "a", "y": "b"}

        result = parse_csv_first_row_with_mapping(csv_bytes, mapping)

        assert result["x"] == ""
        assert result["y"] == ""

    def test_empty_mapping(self):
        """空のマッピングで元のカラム名がそのまま返ること"""
        csv_bytes = self._make_csv_bytes("col1,col2", "v1,v2")
        result = parse_csv_first_row_with_mapping(csv_bytes, {})

        assert result["col1"] == "v1"
        assert result["col2"] == "v2"


def _image_bytes(size, fmt="PNG", mode="RGB", color=(200, 100, 50), noise=False):
    import io
    import os

    from PIL import Image

    if noise:
        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _jpeg_with_orientation(size, orientation):
    import io

    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _decode(data):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(data))


@pytest.mark.unit
class TestFitImageForModel:
    """画像を Bedrock 上の Claude の上限に収め、正しく読めるように渡す（Issue #217）

    上限は 1 枚 3.75 MB（base64 後 5 MB）。履歴に画像が溜まると 1 リクエストの画像が
    20 枚を超えるため、長辺も 2000 px に収める。
    """

    # --- そのまま渡す ---

    def test_image_within_limits_is_passed_unchanged(self):
        raw = _image_bytes((800, 600))
        assert fit_image_for_model(raw, "png") == (raw, "png")

    def test_upright_jpeg_with_exif_is_passed_unchanged(self):
        raw = _jpeg_with_orientation((300, 200), 1)
        assert fit_image_for_model(raw, "jpeg") == (raw, "jpeg")

    def test_format_follows_content_not_extension(self):
        raw = _image_bytes((400, 300), fmt="JPEG")
        assert fit_image_for_model(raw, "png") == (raw, "jpeg")

    def test_camera_mpo_is_passed_as_jpeg(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (300, 200)).save(
            buf,
            format="MPO",
            save_all=True,
            append_images=[Image.new("RGB", (300, 200))],
        )
        assert fit_image_for_model(buf.getvalue(), "jpeg") == (buf.getvalue(), "jpeg")

    # --- 縮める・作り直す ---

    def test_large_image_is_shrunk_under_byte_limit(self):
        raw = _image_bytes((1800, 1800), noise=True)
        assert len(raw) > MODEL_IMAGE_MAX_BYTES

        data, image_format = fit_image_for_model(raw, "png")

        assert len(data) <= MODEL_IMAGE_MAX_BYTES
        assert image_format == "jpeg"

    def test_long_side_is_shrunk_to_the_max_side(self):
        # 縮める先は長辺の上限そのもの（Claude 4.7 以降は長辺 2576 px まで読む）
        data, _ = fit_image_for_model(_image_bytes((6000, 300)), "png")

        assert _decode(data).size == (MODEL_IMAGE_MAX_SIDE, 100)

    def test_unsupported_format_is_reencoded(self):
        data, image_format = fit_image_for_model(
            _image_bytes((400, 300), fmt="BMP"), "png"
        )

        assert image_format == "jpeg"
        assert _decode(data).size == (400, 300)

    def test_exif_orientation_is_applied(self):
        # モデルはメタデータを読まないため、画素を正しい向きに回す
        data, _ = fit_image_for_model(_jpeg_with_orientation((300, 200), 6), "jpeg")

        assert _decode(data).size == (200, 300)

    def test_transparent_image_is_flattened_on_white(self):
        raw = _image_bytes((4000, 100), mode="RGBA", color=(0, 0, 0, 0))

        data, _ = fit_image_for_model(raw, "png")

        assert _decode(data).convert("RGB").getpixel((0, 0)) > (240, 240, 240)

    # --- 外す ---

    def test_undecodable_image_over_limit_is_dropped(self):
        raw = b"\x89PNG\r\n" + b"\0" * MODEL_IMAGE_MAX_BYTES
        assert fit_image_for_model(raw, "png") is None

    @staticmethod
    def _forbid_png_decode(monkeypatch):
        from PIL import PngImagePlugin

        def load(self):
            raise AssertionError("the PNG pixel data was decoded")

        monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", load)

    def test_png_over_pixel_limit_is_dropped_without_decoding(self, monkeypatch):
        """Pillow の PNG は getexif() で画像全体を展開する。上限の確認はその前に行う"""
        from src.managers.shared import file_utils

        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_SOURCE_PIXELS", 1_000_000)
        raw = _image_bytes((2500, 1000))
        self._forbid_png_decode(monkeypatch)

        assert fit_image_for_model(raw, "png") is None

    def test_png_within_limits_is_passed_without_decoding(self, monkeypatch):
        raw = _image_bytes((800, 600))
        self._forbid_png_decode(monkeypatch)

        # 拡張子は jpeg。中身から形式を決められた（展開の失敗で素通りしていない）ことを見る
        assert fit_image_for_model(raw, "jpeg") == (raw, "png")

    def test_png_exif_before_pixel_data_is_applied(self):
        import io

        from PIL import Image

        exif = Image.Exif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        Image.new("RGB", (300, 200)).save(buf, format="PNG", exif=exif)

        data, _ = fit_image_for_model(buf.getvalue(), "png")

        assert _decode(data).size == (200, 300)

    def test_pixel_limit_is_higher_for_jpeg(self, monkeypatch):
        """JPEG は縮小しながら展開するため、画素数の上限が高い"""
        from src.managers.shared import file_utils

        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_SOURCE_PIXELS", 1_000_000)
        monkeypatch.setattr(file_utils, "MODEL_IMAGE_MAX_JPEG_SOURCE_PIXELS", 4_000_000)

        assert fit_image_for_model(_image_bytes((2500, 1000)), "png") is None
        data, _ = fit_image_for_model(_image_bytes((2500, 1000), fmt="JPEG"), "jpeg")
        assert _decode(data).size == (MODEL_IMAGE_MAX_SIDE, 800)

    # --- build_content_block との接続 ---

    def test_content_block_uses_the_fitted_image(self):
        block = build_content_block(
            _image_bytes((400, 300), fmt="BMP"), "image/bmp", "a.bmp"
        )
        assert block["image"]["format"] == "jpeg"

    def test_content_block_is_none_when_the_image_cannot_fit(self):
        raw = b"\x89PNG\r\n" + b"\0" * MODEL_IMAGE_MAX_BYTES
        assert build_content_block(raw, "image/png", "broken.png") is None
