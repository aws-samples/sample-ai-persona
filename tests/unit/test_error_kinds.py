"""ErrorCode の分類（ErrorKind）の契約テスト（Issue #117 ステップ1）。

`ErrorKind` は「ユーザーが次に何をすればよいか」を表し、表示方法を決める。
表示層はHTTPステータスや個別コードではなく kind で分岐する。

本テストは分類の一貫性を検査する。文言と違い分類は機械的な検証が効くため、
「新しいコードを追加したが分類を考えていない」状態を検出できる。
"""

import pytest

from src.models.errors import CodedError, ErrorCode, ErrorKind


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
