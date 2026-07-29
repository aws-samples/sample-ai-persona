"""エラーコード + 文言カタログ方式の契約テスト（Issue #112）"""

import pytest

from src.models.errors import CodedError, ErrorCode
from web import error_messages
from web.error_messages import FALLBACK_MESSAGE, user_message_for


class TestCatalogCoverage:
    """カタログの網羅性。コード追加時の登録漏れを検知する。"""

    def test_every_code_has_wording(self):
        missing = [
            code.name
            for code in ErrorCode
            if code is not ErrorCode.UNKNOWN and code not in error_messages._CATALOG
        ]
        assert not missing, f"カタログ未登録のErrorCode: {missing}"

    def test_unknown_is_not_catalogued(self):
        """UNKNOWN は「コード未設定」を表す番兵であり文言を持たない。"""
        assert ErrorCode.UNKNOWN not in error_messages._CATALOG

    def test_no_wording_duplication(self):
        """同一文言が複数コードに割り当てられていないこと（コード分割の妥当性）。"""
        wordings = list(error_messages._CATALOG.values())
        assert len(wordings) == len(set(wordings))


class TestResolution:
    """例外 → 文言の解決。"""

    def test_coded_exception_resolves_to_catalog_wording(self):
        class _Err(CodedError):
            code = ErrorCode.GENERATION_CAPACITY_EXCEEDED

        message = user_message_for(_Err("output tokens hit max_tokens=32000"))
        assert (
            message == error_messages._CATALOG[ErrorCode.GENERATION_CAPACITY_EXCEEDED]
        )

    def test_code_passed_to_constructor_wins(self):
        """1つの例外型が複数コードを出すケース（FileUploadError等）。"""
        exc = CodedError("boom", code=ErrorCode.REPORT_CAPACITY_EXCEEDED)
        assert (
            user_message_for(exc)
            == error_messages._CATALOG[ErrorCode.REPORT_CAPACITY_EXCEEDED]
        )

    @pytest.mark.parametrize(
        "exc",
        [
            None,
            Exception("内部詳細"),
            CodedError("コード未指定"),
            ValueError("不正な値"),
        ],
    )
    def test_uncoded_exception_falls_back(self, exc):
        assert user_message_for(exc) == FALLBACK_MESSAGE

    def test_default_overrides_fallback(self):
        assert (
            user_message_for(
                Exception("内部詳細"), default="レポートの生成に失敗しました"
            )
            == "レポートの生成に失敗しました"
        )

    def test_default_ignored_when_code_is_catalogued(self):
        exc = CodedError("boom", code=ErrorCode.GENERATION_CAPACITY_EXCEEDED)
        assert user_message_for(exc, default="別の文言") != "別の文言"

    def test_non_errorcode_code_attribute_falls_back(self):
        """code 属性に ErrorCode 以外が入っていても文言を返さない。"""

        class _Weird(Exception):
            code = "generation_capacity_exceeded"

        assert user_message_for(_Weird("boom")) == FALLBACK_MESSAGE


class TestInterpolation:
    """context によるテンプレート補間（i18n拡張の前提）。"""

    @pytest.fixture
    def dummy_code(self, monkeypatch):
        """カタログにテンプレート付きのダミーコードを注入する。"""
        code = ErrorCode.REPORT_CAPACITY_EXCEEDED
        monkeypatch.setitem(
            error_messages._CATALOG,
            code,
            "上限は {max_size_mb:.1f}MB です（対応形式: {formats}）",
        )
        return code

    def test_context_values_are_interpolated(self, dummy_code):
        exc = CodedError(
            "file too large",
            code=dummy_code,
            context={"max_size_mb": 5.0, "formats": ".txt, .pdf"},
        )
        assert user_message_for(exc) == "上限は 5.0MB です（対応形式: .txt, .pdf）"

    def test_missing_context_key_falls_back_without_raising(self, dummy_code):
        exc = CodedError("file too large", code=dummy_code, context={"formats": ".txt"})
        assert user_message_for(exc) == FALLBACK_MESSAGE

    def test_wrong_context_type_falls_back_without_raising(self, dummy_code):
        exc = CodedError(
            "file too large",
            code=dummy_code,
            context={"max_size_mb": "五", "formats": ".txt"},
        )
        assert user_message_for(exc) == FALLBACK_MESSAGE

    def test_missing_context_key_respects_default(self, dummy_code):
        exc = CodedError("file too large", code=dummy_code)
        assert (
            user_message_for(exc, default="保存に失敗しました") == "保存に失敗しました"
        )

    def test_context_defaults_to_empty_dict(self):
        """context 未指定でも属性は存在し、変更しても他インスタンスに影響しない。"""
        first, second = CodedError("a"), CodedError("b")
        first.context["x"] = 1
        assert second.context == {}


class TestNoMessageLeakage:
    """例外メッセージがレスポンスに到達しないことの回帰テスト（#111の再発防止）。"""

    def test_exception_message_never_appears_in_wording(self):
        secret = "Traceback: bedrock-runtime.ap-northeast-1.amazonaws.com timed out"
        exc = CodedError(secret, code=ErrorCode.GENERATION_CAPACITY_EXCEEDED)
        assert secret not in user_message_for(exc)

    def test_exception_message_never_appears_on_fallback(self):
        secret = "AccessDeniedException: arn:aws:iam::123456789012:role/internal"
        assert secret not in user_message_for(Exception(secret))

    def test_context_is_not_dumped_wholesale(self):
        """補間で使われないcontext値（ID等）が文言に混ざらないこと。"""
        exc = CodedError(
            "not found",
            code=ErrorCode.GENERATION_CAPACITY_EXCEEDED,
            context={"survey_id": "srv-secret-0001"},
        )
        assert "srv-secret-0001" not in user_message_for(exc)
