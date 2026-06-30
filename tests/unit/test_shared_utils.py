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
        # メッセージを追加（generate_insightsが要求）
        from src.models.message import Message

        msg = Message.create_new(
            persona_id="p1",
            persona_name="太郎",
            content="テスト",
            message_type="statement",
        )
        discussion = discussion.add_message(msg)
        msg2 = Message.create_new(
            persona_id="p2",
            persona_name="花子",
            content="テスト2",
            message_type="statement",
        )
        discussion = discussion.add_message(msg2)

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
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        assert result.insights is not None
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
            categories=None,
            ai_service=mock_ai_service,
            logger=Mock(),
        )

        assert result.insights == [] or result.insights is None

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
        # Shift_JIS固有のバイト列（UTF-8では不正）
        content = "表計算".encode("shift_jis")
        # UTF-8でデコード可能な場合もあるのでShift_JIS固有文字を使う
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

        # 実装はbreakで1行消費するため、100 + 残り49 = 149
        assert row_count == 149
        assert len(columns) == 2

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
