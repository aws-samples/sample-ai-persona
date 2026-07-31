"""ErrorCode の分類（ErrorKind）の契約テスト（Issue #117 ステップ1）。

`ErrorKind` は「ユーザーが次に何をすればよいか」を表し、表示方法を決める。
表示層はHTTPステータスや個別コードではなく kind で分岐する。

本テストは分類の一貫性を検査する。文言と違い分類は機械的な検証が効くため、
「新しいコードを追加したが分類を考えていない」状態を検出できる。
"""

import json

import pytest

from src.models.errors import CodedError, ErrorCode, ErrorKind
from web.error_messages import is_transient, toast_response, user_message_for


class TestKindAssignment:
    """全コードが分類を持つこと。"""

    def test_every_code_has_a_kind(self):
        for code in ErrorCode:
            assert isinstance(code.kind, ErrorKind), f"{code.name} に kind がない"

    def test_unknown_is_transient(self):
        """未分類の失敗が「入力を直せば解決する」ように見えてはならない。"""
        assert ErrorCode.UNKNOWN.kind is ErrorKind.TRANSIENT

    def test_every_kind_is_used(self):
        """使われていない分類が定義に残っていないこと。"""
        used = {code.kind for code in ErrorCode}
        unused = [kind.name for kind in ErrorKind if kind not in used]
        assert not unused, f"未使用の ErrorKind: {unused}"


class TestStrEnumSemanticsPreserved:
    """kind の追加で StrEnum としての振る舞いが壊れていないこと。

    `__new__` を定義しているため、値・逆引き・辞書キーとしての同一性が
    保たれることを明示的に確認する（カタログは ErrorCode をキーに持つ）。
    """

    def test_value_is_the_string(self):
        assert ErrorCode.FILE_TOO_LARGE.value == "file_too_large"

    def test_compares_equal_to_its_string(self):
        assert ErrorCode.FILE_TOO_LARGE == "file_too_large"

    def test_lookup_by_value(self):
        assert ErrorCode("file_too_large") is ErrorCode.FILE_TOO_LARGE

    def test_usable_as_dict_key(self):
        catalog = {ErrorCode.FILE_EMPTY: "wording"}
        assert catalog[ErrorCode.FILE_EMPTY] == "wording"

    def test_name_is_preserved(self):
        assert ErrorCode.FILE_TOO_LARGE.name == "FILE_TOO_LARGE"


class TestKindConsistency:
    """命名と分類が矛盾していないこと。

    命名規則だけでは分類できないケース（例: SEGMENT_CSV_URL_MISSING は
    "MISSING" だが内部要因なので TRANSIENT）があるため、
    「この語を含むなら必ずこの分類」と言える範囲だけを検査する。
    """

    @pytest.mark.parametrize(
        "code",
        [c for c in ErrorCode if c.name.endswith("_OPERATION_FAILED")],
    )
    def test_operation_failed_is_transient(self, code):
        """`*_OPERATION_FAILED` は内部要因の総称コードなので TRANSIENT。"""
        assert code.kind is ErrorKind.TRANSIENT

    @pytest.mark.parametrize(
        "code", [c for c in ErrorCode if "CAPACITY_EXCEEDED" in c.name]
    )
    def test_capacity_exceeded_is_capacity(self, code):
        assert code.kind is ErrorKind.CAPACITY

    @pytest.mark.parametrize(
        "code", [c for c in ErrorCode if c.name.endswith("_NOT_CONFIGURED")]
    )
    def test_not_configured_is_config(self, code):
        """設定不備は運用者の対応が必要なので CONFIG。"""
        assert code.kind is ErrorKind.CONFIG

    def test_validation_is_the_majority(self):
        """入力バリデーションが最多であること（分類の妥当性の目安）。

        大半が TRANSIENT に倒れているなら分類を見直すべきサイン。
        """
        counts: dict[ErrorKind, int] = {}
        for code in ErrorCode:
            counts[code.kind] = counts.get(code.kind, 0) + 1
        assert counts[ErrorKind.VALIDATION] == max(counts.values())


class TestCodedErrorExposesKind:
    """例外インスタンスから分類を辿れること（表示層が使う経路）。"""

    def test_kind_via_exception_instance(self):
        exc = CodedError("diag", code=ErrorCode.FILE_EMPTY)
        assert exc.code.kind is ErrorKind.VALIDATION

    def test_default_code_is_transient(self):
        """コード未指定の例外は TRANSIENT として扱われる。"""
        assert CodedError("diag").code.kind is ErrorKind.TRANSIENT

    def test_subclass_default_kind(self):
        class _Err(CodedError):
            code = ErrorCode.PERSONA_NOT_FOUND

        assert _Err("diag").code.kind is ErrorKind.NOT_FOUND


class TestToastResponse:
    """TRANSIENT エラーをトーストで通知する経路の契約テスト（#117 ステップ2）。

    再試行で解決しうるエラーは画面を書き換えずトーストで通知する。これにより
    ユーザーの入力が保持され、かつ htmx 1.9.10 が4xx本文をスワップしない制約も
    回避できる（HX-Trigger はスワップ判定より前に処理されるため）。
    """

    def test_body_is_empty(self):
        """本文を返さない（スワップされても画面を壊さない）。"""
        response = toast_response(
            CodedError("diag", code=ErrorCode.PERSONA_OPERATION_FAILED)
        )
        assert response.body == b""

    def test_hx_trigger_carries_the_catalog_wording(self):
        response = toast_response(
            CodedError("diag", code=ErrorCode.PERSONA_OPERATION_FAILED)
        )
        payload = json.loads(response.headers["HX-Trigger"])
        assert payload["showToast"]["type"] == "error"
        assert payload["showToast"]["message"] == user_message_for(
            CodedError("diag", code=ErrorCode.PERSONA_OPERATION_FAILED)
        )

    def test_header_is_latin1_encodable(self):
        """HTTPヘッダーは latin-1 のみ。日本語文言は \\uXXXX にエスケープする。

        ensure_ascii=False にすると Starlette が UnicodeEncodeError を投げる。
        """
        response = toast_response(
            CodedError("diag", code=ErrorCode.MEMORY_OPERATION_FAILED)
        )
        header = response.headers["HX-Trigger"]
        header.encode("latin-1")  # 例外が出なければ送信可能
        assert "\\u" in json.dumps(json.loads(header), ensure_ascii=True)

    def test_does_not_leak_the_exception_message(self):
        """例外メッセージがヘッダーに転写されないこと。

        `toast_response` は AST検査（tests/api/test_error_exposure.py）の
        許可リストに入っているため、漏出しないことを直接検証する。
        """
        secret = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret/key"
        response = toast_response(
            CodedError(secret, code=ErrorCode.PERSONA_OPERATION_FAILED)
        )
        assert secret not in response.headers["HX-Trigger"]

    def test_status_code_stays_4xx_by_default(self):
        """htmx:responseError の意味論とログの正確さを保つため既定は4xx。"""
        response = toast_response(CodedError("diag", code=ErrorCode.UNKNOWN))
        assert 400 <= response.status_code < 500

    def test_uncoded_exception_uses_default(self):
        response = toast_response(RuntimeError("boom"), default="保存に失敗しました")
        payload = json.loads(response.headers["HX-Trigger"])
        assert payload["showToast"]["message"] == "保存に失敗しました"


class TestIsTransient:
    """Router が表示方法を判断するためのヘルパー。"""

    def test_true_for_transient_codes(self):
        assert is_transient(CodedError("d", code=ErrorCode.PERSONA_OPERATION_FAILED))

    def test_false_for_validation_codes(self):
        assert not is_transient(CodedError("d", code=ErrorCode.PERSONA_FIELD_REQUIRED))

    def test_false_for_not_found_codes(self):
        assert not is_transient(CodedError("d", code=ErrorCode.PERSONA_NOT_FOUND))

    def test_true_for_uncoded_exception(self):
        """分類できない失敗は入力修正で直るように見せない。"""
        assert is_transient(RuntimeError("boom"))

    def test_true_for_connection_errors(self):
        assert is_transient(ConnectionError("unreachable"))
