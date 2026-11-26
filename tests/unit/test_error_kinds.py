"""Contract tests for the ErrorCode classification (ErrorKind).

`ErrorKind` says what the user should do next and decides how the error is shown.
The display layer branches on kind, not on HTTP status or individual codes.

These tests check classification consistency. Unlike wording, classification can be
verified mechanically, so a new code added without thinking about its kind is caught.
"""

import json

import pytest

from src.models.errors import CodedError, ErrorCode, ErrorKind
from web.error_messages import is_transient, toast_response, user_message_for


class TestKindAssignment:
    """Every code has a classification."""

    def test_every_code_has_a_kind(self):
        for code in ErrorCode:
            assert isinstance(code.kind, ErrorKind), f"{code.name} に kind がない"

    def test_unknown_is_transient(self):
        """An unclassified failure must not look fixable by correcting input."""
        assert ErrorCode.UNKNOWN.kind is ErrorKind.TRANSIENT

    def test_every_kind_is_used(self):
        """No unused classification is left in the definition."""
        used = {code.kind for code in ErrorCode}
        unused = [kind.name for kind in ErrorKind if kind not in used]
        assert not unused, f"未使用の ErrorKind: {unused}"


class TestStrEnumSemanticsPreserved:
    """Adding a kind does not break StrEnum behavior.

    Because `__new__` is defined, identity of values, reverse lookup and use as dict keys is
    checked explicitly (the catalog is keyed by ErrorCode).
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
    """Naming and classification do not contradict each other.

    Naming alone cannot classify some cases (e.g. SEGMENT_CSV_URL_MISSING contains
    "MISSING" but is an internal cause, so TRANSIENT), so only cases where "a name
    containing this word always has this kind" are checked.
    """

    @pytest.mark.parametrize(
        "code",
        [c for c in ErrorCode if c.name.endswith("_OPERATION_FAILED")],
    )
    def test_operation_failed_is_transient(self, code):
        """`*_OPERATION_FAILED` is a generic code for internal causes, so TRANSIENT."""
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
        """A configuration fault needs operator action, so CONFIG."""
        assert code.kind is ErrorKind.CONFIG

    def test_validation_is_the_majority(self):
        """Input validation is the largest kind (a sanity check on the classification).

        If most codes fall to TRANSIENT, the classification should be revisited.
        """
        counts: dict[ErrorKind, int] = {}
        for code in ErrorCode:
            counts[code.kind] = counts.get(code.kind, 0) + 1
        assert counts[ErrorKind.VALIDATION] == max(counts.values())


class TestAuthCodes:
    """Login codes."""

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.AUTH_REQUIRED,
            ErrorCode.SESSION_EXPIRED,
            ErrorCode.OIDC_GRANT_INVALID,
            ErrorCode.OIDC_TOKEN_INVALID,
        ],
    )
    def test_login_required_codes_are_unauthorized(self, code):
        assert code.kind is ErrorKind.UNAUTHORIZED

    def test_csrf_failure_is_forbidden(self):
        assert ErrorCode.CSRF_FAILED.kind is ErrorKind.FORBIDDEN

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.LOOKUP_UNAVAILABLE,
            ErrorCode.SESSION_STORE_UNAVAILABLE,
            ErrorCode.OIDC_UNAVAILABLE,
        ],
    )
    def test_unreachable_dependencies_are_transient(self, code):
        assert code.kind is ErrorKind.TRANSIENT

    def test_auth_required_error_can_carry_session_expired(self):
        from src.models.errors import AuthRequiredError, LookupUnavailableError

        assert AuthRequiredError("no session").code is ErrorCode.AUTH_REQUIRED
        expired = AuthRequiredError("idle", code=ErrorCode.SESSION_EXPIRED)
        assert expired.code is ErrorCode.SESSION_EXPIRED
        assert LookupUnavailableError("store down").code is ErrorCode.LOOKUP_UNAVAILABLE


class TestAuthzCodes:
    """Authorization codes."""

    @pytest.mark.parametrize("code", [ErrorCode.ROLE_REQUIRED, ErrorCode.ACCESS_DENIED])
    def test_denials_are_forbidden(self, code):
        assert code.kind is ErrorKind.FORBIDDEN

    def test_malformed_request_is_validation(self):
        assert ErrorCode.REQUEST_INVALID.kind is ErrorKind.VALIDATION

    def test_framework_http_error_is_not_found(self):
        assert ErrorCode.HTTP_ERROR.kind is ErrorKind.NOT_FOUND

    def test_exception_classes_carry_their_codes(self):
        from src.models.errors import AccessDeniedError, RoleRequiredError

        assert AccessDeniedError("no").code is ErrorCode.ACCESS_DENIED
        assert RoleRequiredError("no").code is ErrorCode.ROLE_REQUIRED
        assert issubclass(AccessDeniedError, CodedError)
        assert issubclass(RoleRequiredError, CodedError)


class TestPersonaU5Codes:
    """Codes for upload files and knowledge base bindings."""

    @pytest.mark.parametrize(
        "code",
        [ErrorCode.GENERATION_RESULT_TOO_LARGE, ErrorCode.FILE_ATTACH_LIMIT_EXCEEDED],
    )
    def test_size_limits_are_capacity(self, code):
        assert code.kind is ErrorKind.CAPACITY

    @pytest.mark.parametrize(
        "code", [ErrorCode.FILE_NOT_FOUND, ErrorCode.KB_BINDING_NOT_FOUND]
    )
    def test_missing_targets_are_not_found(self, code):
        assert code.kind is ErrorKind.NOT_FOUND


class TestCodedErrorExposesKind:
    """The kind can be traced from an exception instance (the path the display layer uses)."""

    def test_kind_via_exception_instance(self):
        exc = CodedError("diag", code=ErrorCode.FILE_EMPTY)
        assert exc.code.kind is ErrorKind.VALIDATION

    def test_default_code_is_transient(self):
        """An exception without a code is treated as TRANSIENT."""
        assert CodedError("diag").code.kind is ErrorKind.TRANSIENT

    def test_subclass_default_kind(self):
        class _Err(CodedError):
            code = ErrorCode.PERSONA_NOT_FOUND

        assert _Err("diag").code.kind is ErrorKind.NOT_FOUND


class TestToastResponse:
    """Contract tests for notifying TRANSIENT errors by toast.

    An error that a retry may resolve is shown as a toast without rewriting the screen. This
    keeps the user input and works around htmx 1.9.10 not swapping 4xx bodies (HX-Trigger is
    processed before the swap decision).
    """

    def test_body_is_empty(self):
        """No body is returned (a swap would not break the screen)."""
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
        """HTTP headers are latin-1 only. Japanese text is escaped as \\uXXXX.

        With ensure_ascii=False, Starlette raises UnicodeEncodeError.
        """
        response = toast_response(
            CodedError("diag", code=ErrorCode.MEMORY_OPERATION_FAILED)
        )
        header = response.headers["HX-Trigger"]
        header.encode("latin-1")  # sendable if no exception is raised
        assert "\\u" in json.dumps(json.loads(header), ensure_ascii=True)

    def test_does_not_leak_the_exception_message(self):
        """The exception message is not copied into the header.

        `toast_response` is in the allowlist of the AST check (tests/api/test_error_exposure.py),
        so leakage is verified directly.
        """
        secret = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret/key"
        response = toast_response(
            CodedError(secret, code=ErrorCode.PERSONA_OPERATION_FAILED)
        )
        assert secret not in response.headers["HX-Trigger"]

    def test_status_code_stays_4xx_by_default(self):
        """The default is 4xx to keep htmx:responseError semantics and logging accurate."""
        response = toast_response(CodedError("diag", code=ErrorCode.UNKNOWN))
        assert 400 <= response.status_code < 500

    def test_uncoded_exception_uses_default(self):
        response = toast_response(RuntimeError("boom"), default="保存に失敗しました")
        payload = json.loads(response.headers["HX-Trigger"])
        assert payload["showToast"]["message"] == "保存に失敗しました"


class TestIsTransient:
    """Helpers the Router uses to decide how to display."""

    def test_true_for_transient_codes(self):
        assert is_transient(CodedError("d", code=ErrorCode.PERSONA_OPERATION_FAILED))

    def test_false_for_validation_codes(self):
        assert not is_transient(CodedError("d", code=ErrorCode.PERSONA_FIELD_REQUIRED))

    def test_false_for_not_found_codes(self):
        assert not is_transient(CodedError("d", code=ErrorCode.PERSONA_NOT_FOUND))

    def test_true_for_uncoded_exception(self):
        """An unclassifiable failure is not shown as fixable by correcting input."""
        assert is_transient(RuntimeError("boom"))

    def test_true_for_connection_errors(self):
        assert is_transient(ConnectionError("unreachable"))


class TestSharingCodes:
    """Classification of the shared codes."""

    @pytest.mark.parametrize(
        "name",
        [
            "SHARE_SCOPE_INVALID",
            "SHARE_NO_DEPARTMENT",
            "SHARE_OWNER_MISSING",
            "SHARE_NO_TARGET",
        ],
    )
    def test_input_errors_are_validation(self, name):
        assert ErrorCode[name].kind is ErrorKind.VALIDATION

    @pytest.mark.parametrize(
        "name", ["SHARE_TOO_MANY_TARGETS", "SHARE_TOO_MANY_GROUPS"]
    )
    def test_limits_are_capacity(self, name):
        assert ErrorCode[name].kind is ErrorKind.CAPACITY

    def test_update_failure_is_transient(self):
        assert ErrorCode["SHARE_UPDATE_FAILED"].kind is ErrorKind.TRANSIENT


class TestDiscussionInterviewCodes:
    """Codes for discussions and interview sessions."""

    def test_too_large_is_capacity(self):
        assert ErrorCode["DISCUSSION_TOO_LARGE"].kind is ErrorKind.CAPACITY

    @pytest.mark.parametrize(
        "name",
        [
            "DISCUSSION_OPERATION_FAILED",
            "INTERVIEW_SESSION_OPERATION_FAILED",
            "CONCURRENT_UPDATE",
        ],
    )
    def test_failures_and_conflicts_are_transient(self, name):
        assert ErrorCode[name].kind is ErrorKind.TRANSIENT

    def test_concurrent_update_error_carries_its_code(self):
        from src.models.errors import ConcurrentUpdateError

        assert ConcurrentUpdateError("busy").code is ErrorCode.CONCURRENT_UPDATE
        assert is_transient(ConcurrentUpdateError("busy"))

    def test_no_interview_message_conflict_code(self):
        """The only conflict code is CONCURRENT_UPDATE (no 409 code is added)."""
        assert "INTERVIEW_MESSAGE_CONFLICT" not in ErrorCode.__members__


class TestSurveyOrgAssetCodes:
    """Codes for custom persona data and settings."""

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("SETTINGS_NOT_FOUND", ErrorKind.NOT_FOUND),
            ("CUSTOM_PERSONA_DATA_NOT_FOUND", ErrorKind.NOT_FOUND),
            ("CUSTOM_PERSONA_DATA_NAME_INVALID", ErrorKind.VALIDATION),
            ("CUSTOM_PERSONA_DATA_NAME_TAKEN", ErrorKind.VALIDATION),
            ("SURVEY_TEMPLATE_TOO_LARGE", ErrorKind.CAPACITY),
            ("SURVEY_REPORT_TOO_LARGE", ErrorKind.CAPACITY),
            ("SURVEY_TEMPLATE_IMAGE_UNAVAILABLE", ErrorKind.TRANSIENT),
        ],
    )
    def test_kind(self, name, kind):
        assert ErrorCode[name].kind is kind

    def test_hidden_settings_answer_404(self):
        from web.error_messages import http_status_for

        assert http_status_for(ErrorCode["SETTINGS_NOT_FOUND"]) == 404
        assert http_status_for(ErrorCode["CUSTOM_PERSONA_DATA_NOT_FOUND"]) == 404


class TestClosedNetworkLoginCodes:
    """Login codes for the closed-network setup.

    The two forms are full-page forms, and both VALIDATION and TRANSIENT show text inside the
    screen. Only a timeout restarts from the login screen (UNAUTHORIZED).
    """

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("LOGIN_FAILED", ErrorKind.VALIDATION),
            ("PASSWORD_POLICY_NOT_MET", ErrorKind.VALIDATION),
            ("PASSWORD_CONFIRMATION_MISMATCH", ErrorKind.VALIDATION),
            ("LOGIN_RATE_LIMITED", ErrorKind.TRANSIENT),
            ("LOGIN_UNAVAILABLE", ErrorKind.TRANSIENT),
            ("LOGIN_CHALLENGE_EXPIRED", ErrorKind.UNAUTHORIZED),
        ],
    )
    def test_kind(self, name, kind):
        assert ErrorCode[name].kind is kind

    def test_values_are_snake_case_of_the_names(self):
        for name in (
            "LOGIN_FAILED",
            "PASSWORD_POLICY_NOT_MET",
            "PASSWORD_CONFIRMATION_MISMATCH",
            "LOGIN_RATE_LIMITED",
            "LOGIN_UNAVAILABLE",
            "LOGIN_CHALLENGE_EXPIRED",
        ):
            assert ErrorCode[name].value == name.lower()


class TestSurveyOnDemandCodes:
    """Codes for on-demand survey inference."""

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("SURVEY_RUNNER_NOT_CONFIGURED", ErrorKind.CONFIG),
            ("SURVEY_RUNNER_UNAVAILABLE", ErrorKind.CONFIG),
            ("SURVEY_INFERENCE_FAILED", ErrorKind.TRANSIENT),
            ("SURVEY_EXECUTION_TIMED_OUT", ErrorKind.TRANSIENT),
            ("SURVEY_TOO_MANY_FAILED_ANSWERS", ErrorKind.TRANSIENT),
            ("SURVEY_EXECUTION_INTERRUPTED", ErrorKind.TRANSIENT),
        ],
    )
    def test_kind(self, name, kind):
        assert ErrorCode[name].kind is kind

    @pytest.mark.parametrize(
        "value",
        [
            "survey_batch_role_not_configured",
            "survey_batch_job_failed",
            "survey_batch_job_timed_out",
        ],
    )
    def test_codes_stored_before_the_switch_still_parse(self, value):
        """Records written by batch inference keep their code."""
        assert ErrorCode.parse(value) is not None
