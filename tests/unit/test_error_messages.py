"""エラーコード + 文言カタログ方式の契約テスト（Issue #112）"""

import json
import string

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

    def test_every_code_resolves_to_its_filled_wording(self):
        """Every catalogued code reaches its own wording, never the fallback.

        Each template's placeholders are filled with a sample value, so a
        template that cannot be filled (a typo, a bad format spec) fails here.
        """
        sentinel = "SENTINEL_GENERIC_FAILURE"
        failed = []
        for code, template in error_messages._CATALOG.items():
            names = {f for _, f, _, _ in string.Formatter().parse(template) if f}
            context: dict[str, object] = {name: 1 for name in names}
            if "field" in names:
                context["field"] = "not-a-known-field"
            message = user_message_for(
                CodedError("diag", code=code, context=context), default=sentinel
            )
            expected = template.format(**{**context, "field": "入力値"})
            if message != expected:
                failed.append(code.name)
        assert not failed, f"文言に解決できないコード: {failed}"

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


class TestJsonErrorResponse:
    """JSON を読む画面のスクリプト向けのエラーの本文（高度な生成 U1、契約 C1）。"""

    def test_body_has_the_wording_code_and_kind_only(self):
        secret = "arn:aws:s3:::bucket/sources/owner/file failed"
        exc = CodedError(
            secret,
            code=ErrorCode.UPLOAD_TOO_LARGE,
            context={"max_size_mb": 1000, "max_bytes": 10},
        )

        response = error_messages.json_error_response(exc)

        body = json.loads(response.body)
        assert set(body) == {"detail", "error_code", "kind"}
        assert body["error_code"] == ErrorCode.UPLOAD_TOO_LARGE.value
        assert body["kind"] == ErrorKind.CAPACITY.value
        assert secret not in response.body.decode()
        assert response.status_code == 413
        assert response.headers["Cache-Control"] == "no-store"

    def test_payload_names_code_kind_and_wording(self):
        secret = "generation expired at 2026-11-20 for owner-1"
        exc = CodedError(secret, code=ErrorCode.GENERATION_EXPIRED)

        payload = error_messages.error_payload(exc)

        assert payload == {
            "error_code": ErrorCode.GENERATION_EXPIRED.value,
            "kind": ErrorKind.NOT_FOUND.value,
            "message": user_message_for(exc),
        }
        assert secret not in json.dumps(payload, ensure_ascii=False)

    def test_status_can_be_given(self):
        exc = CodedError(code=ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING)

        assert (
            error_messages.json_error_response(exc, status_code=409).status_code == 409
        )

    def test_uncoded_exception_is_transient_with_the_fallback(self):
        secret = "botocore: connect timeout on https://example"

        response = error_messages.json_error_response(Exception(secret))

        body = json.loads(response.body)
        assert body["error_code"] == ErrorCode.UNKNOWN.value
        assert body["kind"] == ErrorKind.TRANSIENT.value
        assert body["detail"] == FALLBACK_MESSAGE
        assert response.status_code == 500
        assert secret not in response.body.decode()


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
    # U7: 利用範囲の外のカスタムペルソナデータと、admin だけの設定（NFR1.79）
    ErrorCode.CUSTOM_PERSONA_DATA_NOT_FOUND,
    ErrorCode.SETTINGS_NOT_FOUND,
    # 高度な生成 U1: 他人のアップロード・ファイル・生成（functional-spec.md §5）
    ErrorCode.UPLOAD_NOT_FOUND,
    ErrorCode.GENERATION_SOURCE_NOT_FOUND,
    ErrorCode.GENERATION_NOT_FOUND,
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


class TestDiscussionInterviewWording:
    """U6 のコードがカタログの文言を返すこと（BR2.7、NFR1.59、NFR1.60）。"""

    def test_concurrent_update_tells_a_reply_is_in_progress(self):
        """NFR Requirements の決定: 「応答の途中です」の趣旨で、入力を残すトースト。"""
        message = user_message_for(CodedError(code=ErrorCode.CONCURRENT_UPDATE))
        assert "応答の途中" in message
        assert _toast(ErrorCode.CONCURRENT_UPDATE)["type"] == "error"


class TestSurveyOrgAssetWording:
    """U7 のコードがカタログの文言を返すこと（BR3.1、BR4.4、NFR2.25）。"""

    @pytest.mark.parametrize(
        "name", ["SETTINGS_NOT_FOUND", "CUSTOM_PERSONA_DATA_NOT_FOUND"]
    )
    def test_hidden_and_unusable_toast_like_a_missing_page(self, name):
        """存在を知らせない（NFR1.79）: トーストは「ページが見つかりません」。"""
        code = ErrorCode[name]
        assert code in error_messages.UNIFIED_NOT_FOUND_TOAST_CODES
        assert _toast(code)["message"] == error_messages.NOT_FOUND_TOAST_MESSAGE

    def test_name_invalid_shows_the_limit(self):
        message = user_message_for(
            CodedError(
                code=ErrorCode["CUSTOM_PERSONA_DATA_NAME_INVALID"],
                context={"max_length": 100},
            )
        )
        assert "100" in message


class TestClosedNetworkLoginWording:
    """U8 のログインのコード（M2・M3）がカタログの文言を返すこと。"""

    def test_login_failure_wording_does_not_depend_on_the_reason(self):
        """BR1.4: 理由（context）が違っても文言は同じ。"""
        code = ErrorCode["LOGIN_FAILED"]
        plain = user_message_for(CodedError(code=code))
        with_reason = user_message_for(
            CodedError(code=code, context={"reason": "user_not_found"})
        )
        assert plain == with_reason


class TestPasswordRuleLabels:
    """M3 の条件の一覧の文言（条件のキー → 表示）は 1 か所に置く。"""

    def _policy(self, **overrides: object):
        from src.models.password_policy import PasswordPolicy

        params: dict[str, object] = {
            "min_length": 10,
            "require_uppercase": True,
            "require_lowercase": True,
            "require_digits": True,
            "require_symbols": True,
        }
        params.update(overrides)
        return PasswordPolicy(**params)  # type: ignore[arg-type]

    def test_every_rule_has_a_label(self):
        from src.models.password_policy import PasswordRule

        policy = self._policy()
        for rule in PasswordRule:
            assert error_messages.password_rule_label(rule, policy)

    def test_min_length_label_names_the_length(self):
        from src.models.password_policy import PasswordRule

        label = error_messages.password_rule_label(
            PasswordRule.MIN_LENGTH, self._policy(min_length=12)
        )
        assert "12" in label

    def test_max_length_label_names_256(self):
        from src.models.password_policy import PasswordRule

        label = error_messages.password_rule_label(
            PasswordRule.MAX_LENGTH, self._policy()
        )
        assert "256" in label

    def test_missed_rules_are_read_from_the_policy_error_only(self):
        from src.models.password_policy import PasswordRule

        error = CodedError(
            code=ErrorCode["PASSWORD_POLICY_NOT_MET"],
            context={"rules": ["digits", "uppercase", "not-a-rule"]},
        )
        assert error_messages.missed_password_rules(error) == frozenset(
            {PasswordRule.DIGITS, PasswordRule.UPPERCASE}
        )
        other = CodedError(
            code=ErrorCode["LOGIN_FAILED"], context={"rules": ["digits"]}
        )
        assert error_messages.missed_password_rules(other) == frozenset()
        assert error_messages.missed_password_rules(None) == frozenset()

    def test_labels_are_distinct(self):
        from src.models.password_policy import PasswordRule

        policy = self._policy()
        labels = [error_messages.password_rule_label(r, policy) for r in PasswordRule]
        assert len(labels) == len(set(labels))


class TestSurveyOnDemandWording:
    """オンデマンド推論のコードがカタログの文言を返すこと（functional-spec.md 3 章）。"""

    def test_too_many_failed_answers_shows_the_counts(self):
        code = ErrorCode["SURVEY_TOO_MANY_FAILED_ANSWERS"]
        message = error_messages.user_message_for_code(
            code.value, {"failed_count": 60, "total": 1000}
        )
        assert "60" in message
        assert "1000" in message
        assert message != error_messages.FALLBACK_MESSAGE

    def test_stored_batch_codes_still_resolve(self):
        """切り替え前のエラーの記録も文言を引ける（FR11.2）。"""
        for value in ("survey_batch_job_failed", "survey_batch_job_timed_out"):
            message = error_messages.user_message_for_code(value, None)
            assert message != error_messages.FALLBACK_MESSAGE


class TestTableAnalysisDepthWording:
    """U3 (BR16.2, BR17.1, BR18.1): row numbers, sizes and the new code."""

    @staticmethod
    def _message(code: ErrorCode, **context: object) -> str:
        return user_message_for(CodedError("diag", code=code, context=context))

    def test_insufficient_data_is_validation_and_names_the_ways_out(self):
        assert ErrorCode.GENERATION_INSUFFICIENT_DATA.kind is ErrorKind.VALIDATION

        message = self._message(
            ErrorCode.GENERATION_INSUFFICIENT_DATA,
            rows_read=10,
            required_rows=50,
            persona_count=5,
        )

        assert "5 人" in message
        assert "指示で対象を絞る" in message
        assert "人数を減らす" in message
        assert "行数の多いファイル" in message

    def test_insufficient_data_without_numbers_still_reads(self):
        message = self._message(ErrorCode.GENERATION_INSUFFICIENT_DATA)

        assert "人数を減らす" in message
        assert "None" not in message

    def test_unreadable_names_the_lines(self):
        message = self._message(
            ErrorCode.GENERATION_SOURCE_UNREADABLE,
            row_examples=[3, 21, 90],
            rows_read=900,
            rows_excluded=40,
        )

        assert "3、21、90 行目などに問題があります" in message
        assert "ファイルを直して上げ直してください" in message
        assert "900" in message and "40" in message

    def test_unreadable_without_lines_keeps_the_plain_wording(self):
        plain = self._message(ErrorCode.GENERATION_SOURCE_UNREADABLE)

        assert plain == error_messages._CATALOG[ErrorCode.GENERATION_SOURCE_UNREADABLE]
        assert "行目" not in plain

    @pytest.mark.parametrize("bad", ["3", [3, "x"], [True], [0], [1, 2, 3, 4, 5, 6]])
    def test_unusable_line_values_are_not_shown(self, bad):
        message = self._message(
            ErrorCode.GENERATION_SOURCE_UNREADABLE, row_examples=bad
        )

        assert "行目" not in message

    def test_capacity_shows_rows_and_columns(self):
        message = self._message(
            ErrorCode.GENERATION_CAPACITY, rows_read=1_200_000, columns_count=80
        )

        assert "1,200,000 行" in message
        assert "80 列" in message

    def test_capacity_shows_only_what_is_known(self):
        only_columns = self._message(ErrorCode.GENERATION_CAPACITY, columns_count=80)
        nothing = self._message(ErrorCode.GENERATION_CAPACITY)

        assert "80 列" in only_columns and "行・" not in only_columns
        assert nothing == error_messages._CATALOG[ErrorCode.GENERATION_CAPACITY]


class TestDocumentWording:
    """U4 (BR21.2, BR22, BR24.4, BR25.1): the four new codes and their values."""

    @staticmethod
    def _message(code: ErrorCode, **context: object) -> str:
        return user_message_for(CodedError("diag", code=code, context=context))

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("SOURCE_NOT_A_DOCUMENT", ErrorKind.VALIDATION),
            ("DOCUMENT_ENCRYPTED", ErrorKind.VALIDATION),
            ("DOCUMENT_NO_TEXT", ErrorKind.VALIDATION),
            ("GENERATION_SOURCE_TOO_LARGE", ErrorKind.CAPACITY),
        ],
    )
    def test_kinds(self, name, kind):
        assert ErrorCode[name].kind is kind

    def test_a_legacy_word_file_is_told_to_be_saved_as_docx(self):
        legacy = self._message(ErrorCode.UPLOAD_SOURCE_INVALID, reason="legacy_word")
        other = self._message(ErrorCode.UPLOAD_SOURCE_INVALID, reason="format")

        assert ".docx" in legacy
        assert legacy != other

    def test_no_text_names_the_users_file(self):
        message = self._message(
            ErrorCode["DOCUMENT_NO_TEXT"], source_index=1, filename="面談.pdf"
        )

        assert "面談.pdf" in message

    def test_no_text_without_a_file_name_still_reads(self):
        message = self._message(ErrorCode["DOCUMENT_NO_TEXT"], source_index=1)

        assert message == error_messages._CATALOG[ErrorCode["DOCUMENT_NO_TEXT"]]

    def test_encrypted_names_the_file_when_known(self):
        named = self._message(ErrorCode["DOCUMENT_ENCRYPTED"], filename="a.pdf")
        plain = self._message(ErrorCode["DOCUMENT_ENCRYPTED"])

        assert "a.pdf" in named
        assert plain == error_messages._CATALOG[ErrorCode["DOCUMENT_ENCRYPTED"]]

    def test_too_large_shows_the_settled_and_the_limit(self):
        message = self._message(
            ErrorCode["GENERATION_SOURCE_TOO_LARGE"],
            total_chars=2_500_000,
            max_chars=2_000_000,
        )

        assert "2,500,000" in message and "2,000,000" in message

    @pytest.mark.parametrize("bad", ["many", -1, True, None])
    def test_too_large_ignores_unusable_numbers(self, bad):
        message = self._message(
            ErrorCode["GENERATION_SOURCE_TOO_LARGE"],
            total_chars=bad,
            max_chars=2_000_000,
        )

        assert (
            message == error_messages._CATALOG[ErrorCode["GENERATION_SOURCE_TOO_LARGE"]]
        )


class TestGenerationControlWording:
    """U5 (functional-spec.md §5, BR29.5, BR29.7, BR30.2, BR33.1)."""

    @staticmethod
    def _message(code: ErrorCode, **context: object) -> str:
        return user_message_for(CodedError("diag", code=code, context=context))

    @pytest.mark.parametrize(
        "name",
        [
            "GENERATION_STALLED",
            "GENERATION_NOT_AWAITING_PLAN",
            "GENERATION_ALREADY_FINISHED",
            "GENERATION_SOURCE_NOT_READY",
        ],
    )
    def test_the_new_codes_are_transient_and_worded(self, name):
        code = ErrorCode[name]

        assert code.kind is ErrorKind.TRANSIENT
        assert self._message(code) != FALLBACK_MESSAGE

    def test_the_timeout_tells_the_plan_waits_apart(self):
        timeout = ErrorCode["GENERATION_TIMEOUT"]
        card = self._message(timeout, limit="plan_wait_minutes", value=15)
        total = self._message(timeout, limit="plan_wait_total_minutes", value=60)
        whole = self._message(timeout, limit="generation_minutes", value=60)

        assert len({card, total, whole}) == 3
        assert "15" in card and "60" in total
        assert whole == error_messages._CATALOG[timeout]

    def test_a_plan_wait_without_a_usable_value_is_the_plain_wording(self):
        timeout = ErrorCode["GENERATION_TIMEOUT"]
        message = self._message(timeout, limit="plan_wait_minutes", value="x")

        assert message == error_messages._CATALOG[timeout]
