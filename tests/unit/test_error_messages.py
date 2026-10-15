"""エラーコード + 文言カタログ方式の契約テスト（Issue #112）"""

import json

import pytest

from src.models.errors import CodedError, ErrorCode, ErrorKind
from web import error_messages
from web.error_messages import FALLBACK_MESSAGE, user_message_for

# 補間キーの上位集合。どのテンプレートでも補間が成立するようにまとめて渡す。
_SAMPLE_CONTEXT: dict[str, object] = {
    "max_reports": 3,
    "min_length": 5,
    "max_length": 200,
    "min_personas": 2,
    "max_personas": 5,
    "min_rounds": 1,
    "max_rounds": 10,
}


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


class TestAuthWording:
    """ログインのコードが既定文言に落ちず、カタログの文言を返すこと（U1）。"""

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.AUTH_REQUIRED,
            ErrorCode.SESSION_EXPIRED,
            ErrorCode.CSRF_FAILED,
            ErrorCode.LOOKUP_UNAVAILABLE,
            # U2（約束 C5 の残り）
            ErrorCode.ROLE_REQUIRED,
            ErrorCode.ACCESS_DENIED,
            ErrorCode.REQUEST_INVALID,
            ErrorCode.HTTP_ERROR,
        ],
    )
    def test_auth_codes_resolve_to_catalog_wording(self, code):
        sentinel = "SENTINEL_GENERIC_FAILURE"
        assert user_message_for(CodedError("diag", code=code), default=sentinel) != (
            sentinel
        )


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


class TestActionableWordingPreserved:
    """対処方法を含む文言が総称文言に劣化していないことの回帰テスト。

    コード未付与の例外は `default` にフォールバックするため、Manager 側で
    コードを付け忘れると「〜に失敗しました」だけが表示され、ユーザーが
    次に何をすればよいか分からなくなる。実際に一度その劣化を起こしたため、
    ユーザーが行動できる文言については解決後の文字列を直接検証する。
    """

    def test_report_limit_tells_user_to_delete_one(self):
        exc = CodedError(
            "discussion has 3 reports, max is 3",
            code=ErrorCode.REPORT_LIMIT_REACHED,
            context={"max_reports": 3},
        )
        message = user_message_for(exc, default="レポートの保存に失敗しました")
        assert "最大3件" in message
        assert "削除してください" in message

    def test_discussion_topic_length_tells_user_the_bound(self):
        short = CodedError(
            "topic length 2 below minimum 5",
            code=ErrorCode.DISCUSSION_TOPIC_TOO_SHORT,
            context={"min_length": 5},
        )
        long = CodedError(
            "topic length 300 exceeds 200",
            code=ErrorCode.DISCUSSION_TOPIC_TOO_LONG,
            context={"max_length": 200},
        )
        default = "議論の開始中にエラーが発生しました"
        assert "5文字以上" in user_message_for(short, default=default)
        assert "200文字以内" in user_message_for(long, default=default)

    def test_discussion_rounds_tells_user_the_bound(self):
        few = CodedError(
            "rounds 0 below minimum 1",
            code=ErrorCode.DISCUSSION_ROUNDS_TOO_FEW,
            context={"min_rounds": 1},
        )
        many = CodedError(
            "rounds 12 exceeds maximum 10",
            code=ErrorCode.DISCUSSION_ROUNDS_TOO_MANY,
            context={"max_rounds": 10},
        )
        default = "議論の開始中にエラーが発生しました"
        assert "1以上" in user_message_for(few, default=default)
        assert "10以下" in user_message_for(many, default=default)

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.REPORT_LIMIT_REACHED,
            ErrorCode.REPORT_NOT_FOUND,
            ErrorCode.DISCUSSION_TOPIC_REQUIRED,
            ErrorCode.DISCUSSION_TOPIC_TOO_SHORT,
            ErrorCode.DISCUSSION_TOPIC_TOO_LONG,
            ErrorCode.DISCUSSION_TOO_FEW_PERSONAS,
            ErrorCode.DISCUSSION_ROUNDS_TOO_FEW,
            ErrorCode.DISCUSSION_ROUNDS_TOO_MANY,
        ],
    )
    def test_actionable_codes_never_fall_back_to_default(self, code):
        """これらのコードは必ずカタログ文言を返し、default に落ちてはならない。"""
        sentinel = "SENTINEL_GENERIC_FAILURE"
        exc = CodedError("diagnostic detail", code=code, context=_SAMPLE_CONTEXT)
        assert user_message_for(exc, default=sentinel) != sentinel


def _toast(code: ErrorCode) -> dict[str, str]:
    response = error_messages.toast_response(CodedError("diagnostic", code=code))
    return json.loads(response.headers["HX-Trigger"])["showToast"]


#: 「ページが見つかりません」にそろえるコード（U3 functional-spec.md WF8、約束 C5）
_RESOURCE_NOT_FOUND = {
    ErrorCode.PERSONA_NOT_FOUND,
    ErrorCode.DISCUSSION_NOT_FOUND,
    ErrorCode.DISCUSSION_DOCUMENT_NOT_FOUND,
    ErrorCode.REPORT_NOT_FOUND,
    ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
    ErrorCode.SURVEY_NOT_FOUND,
    ErrorCode.SURVEY_TEMPLATE_NOT_FOUND,
    ErrorCode.DATASET_NOT_FOUND,
    ErrorCode.JOB_NOT_FOUND,
    # U5: アップロードファイルと KB の紐付け（security-design.md §5）
    ErrorCode.FILE_NOT_FOUND,
    ErrorCode.KB_BINDING_NOT_FOUND,
}


class TestNotFoundToastWording:
    """NFR1.9: the htmx toast for a missing resource does not reveal its kind."""

    def test_every_not_found_code_is_classified_exactly_once(self):
        unified = error_messages.UNIFIED_NOT_FOUND_TOAST_CODES
        distinct = error_messages.DISTINCT_NOT_FOUND_TOAST_CODES
        not_found = {code for code in ErrorCode if code.kind is ErrorKind.NOT_FOUND}

        assert not unified & distinct
        assert unified | distinct == not_found

    def test_unified_set_is_the_resource_lookups(self):
        assert error_messages.UNIFIED_NOT_FOUND_TOAST_CODES == _RESOURCE_NOT_FOUND

    def test_unified_codes_share_one_error_toast(self):
        toasts = [_toast(code) for code in sorted(_RESOURCE_NOT_FOUND, key=str)]

        assert {t["message"] for t in toasts} == {
            error_messages.NOT_FOUND_TOAST_MESSAGE
        }
        assert {t["type"] for t in toasts} == {"error"}

    @pytest.mark.parametrize("code", sorted(_RESOURCE_NOT_FOUND, key=str))
    def test_catalog_wording_is_unchanged(self, code):
        exc = CodedError("diagnostic", code=code)

        assert user_message_for(exc) == error_messages._CATALOG[code]
        assert user_message_for(exc) != error_messages.NOT_FOUND_TOAST_MESSAGE

    def test_distinct_codes_keep_their_own_toast(self):
        for code in error_messages.DISTINCT_NOT_FOUND_TOAST_CODES:
            assert _toast(code)["message"] == user_message_for(CodedError(code=code))

    def test_other_kinds_are_not_affected(self):
        assert _toast(ErrorCode.ACCESS_DENIED)["message"] == user_message_for(
            CodedError(code=ErrorCode.ACCESS_DENIED)
        )


class TestSharingWording:
    """共有のコード（U4）がカタログの文言を返し、上限を補間すること。"""

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.SHARE_SCOPE_INVALID,
            ErrorCode.SHARE_NO_DEPARTMENT,
            ErrorCode.SHARE_OWNER_MISSING,
            ErrorCode.SHARE_NO_TARGET,
            ErrorCode.SHARE_UPDATE_FAILED,
        ],
    )
    def test_codes_resolve_to_catalog_wording(self, code):
        message = user_message_for(CodedError(code=code), default="既定")
        assert message != "既定"
        assert message == error_messages._CATALOG[code]

    def test_target_limit_is_interpolated(self):
        message = user_message_for(
            CodedError(
                code=ErrorCode.SHARE_TOO_MANY_TARGETS, context={"max_targets": 100}
            )
        )
        assert "100" in message

    def test_group_limit_is_interpolated(self):
        message = user_message_for(
            CodedError(code=ErrorCode.SHARE_TOO_MANY_GROUPS, context={"max_groups": 99})
        )
        assert "99" in message


class TestPersonaU5Wording:
    """U5 のコードがカタログの文言を返し、上限を補間すること。"""

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.GENERATION_RESULT_TOO_LARGE,
            ErrorCode.FILE_NOT_FOUND,
            ErrorCode.KB_BINDING_NOT_FOUND,
        ],
    )
    def test_codes_resolve_to_catalog_wording(self, code):
        message = user_message_for(CodedError(code=code), default="既定")
        assert message == error_messages._CATALOG[code]

    def test_attach_limit_is_interpolated(self):
        message = user_message_for(
            CodedError(
                code=ErrorCode.FILE_ATTACH_LIMIT_EXCEEDED, context={"max_files": 100}
            )
        )
        assert "100" in message

    @pytest.mark.parametrize(
        "code", [ErrorCode.FILE_NOT_FOUND, ErrorCode.KB_BINDING_NOT_FOUND]
    )
    def test_missing_file_and_kb_binding_use_the_unified_toast(self, code):
        assert _toast(code)["message"] == error_messages.NOT_FOUND_TOAST_MESSAGE
