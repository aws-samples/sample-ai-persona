"""PasswordPolicy の単体テスト（U8 entities.md、BR2.5、BR4.1、BR4.3、NFR9.41）。

User Pool のパスワードの条件を環境変数 5 つで受け取り、Cognito に送る前に
アプリだけで確かめる（Cognito を呼ばない）。条件は Cognito の決まりに合わせ、
256 文字以下・先頭と末尾が空白でないことも確かめる。
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.models.password_policy import (
    COGNITO_SYMBOLS,
    MAX_PASSWORD_LENGTH,
    PASSWORD_POLICY_SETTINGS,
    PasswordPolicy,
    PasswordPolicyConfigError,
    PasswordRule,
)

_DEFAULT = PasswordPolicy(
    min_length=8,
    require_uppercase=True,
    require_lowercase=True,
    require_digits=True,
    require_symbols=False,
)
_WITH_SYMBOLS = PasswordPolicy(
    min_length=8,
    require_uppercase=True,
    require_lowercase=True,
    require_digits=True,
    require_symbols=True,
)
_ENV = {
    "PASSWORD_MIN_LENGTH": "8",
    "PASSWORD_REQUIRE_UPPERCASE": "true",
    "PASSWORD_REQUIRE_LOWERCASE": "true",
    "PASSWORD_REQUIRE_DIGITS": "true",
    "PASSWORD_REQUIRE_SYMBOLS": "false",
}


class TestFromSettings:
    def test_five_settings_build_the_policy(self):
        assert PasswordPolicy.from_settings(_ENV) == _DEFAULT
        assert set(PASSWORD_POLICY_SETTINGS) == set(_ENV)

    @pytest.mark.parametrize("value", ["TRUE", "1", "yes"])
    def test_true_spellings(self, value):
        policy = PasswordPolicy.from_settings(
            {**_ENV, "PASSWORD_REQUIRE_SYMBOLS": value}
        )
        assert policy.require_symbols is True

    @pytest.mark.parametrize("value", ["False", "0", "no"])
    def test_false_spellings(self, value):
        policy = PasswordPolicy.from_settings(
            {**_ENV, "PASSWORD_REQUIRE_UPPERCASE": value}
        )
        assert policy.require_uppercase is False

    @pytest.mark.parametrize("missing", sorted(_ENV))
    def test_missing_setting_is_a_configuration_error(self, missing):
        env = {k: v for k, v in _ENV.items() if k != missing}
        with pytest.raises(PasswordPolicyConfigError) as info:
            PasswordPolicy.from_settings(env)
        assert missing in str(info.value)

    @pytest.mark.parametrize("value", ["", "eight", "8.5", "5", "100", "-8"])
    def test_min_length_must_be_an_integer_from_6_to_99(self, value):
        with pytest.raises(PasswordPolicyConfigError):
            PasswordPolicy.from_settings({**_ENV, "PASSWORD_MIN_LENGTH": value})

    @pytest.mark.parametrize("value", ["6", "99"])
    def test_min_length_bounds_are_accepted(self, value):
        policy = PasswordPolicy.from_settings({**_ENV, "PASSWORD_MIN_LENGTH": value})
        assert policy.min_length == int(value)

    @pytest.mark.parametrize("value", ["", "maybe", "2"])
    def test_flags_must_be_booleans(self, value):
        with pytest.raises(PasswordPolicyConfigError):
            PasswordPolicy.from_settings({**_ENV, "PASSWORD_REQUIRE_DIGITS": value})

    def test_error_names_the_setting_but_not_the_value(self):
        with pytest.raises(PasswordPolicyConfigError) as info:
            PasswordPolicy.from_settings({**_ENV, "PASSWORD_MIN_LENGTH": "zzz-secret"})
        assert "PASSWORD_MIN_LENGTH" in str(info.value)
        assert "zzz-secret" not in str(info.value)

    def test_configuration_error_is_a_value_error(self):
        assert issubclass(PasswordPolicyConfigError, ValueError)


class TestRules:
    def test_rules_follow_the_policy_in_a_stable_order(self):
        assert _DEFAULT.rules() == (
            PasswordRule.MIN_LENGTH,
            PasswordRule.UPPERCASE,
            PasswordRule.LOWERCASE,
            PasswordRule.DIGITS,
            PasswordRule.MAX_LENGTH,
            PasswordRule.NO_SURROUNDING_SPACE,
        )
        assert PasswordRule.SYMBOLS in _WITH_SYMBOLS.rules()

    def test_optional_rules_are_dropped_when_not_required(self):
        lax = PasswordPolicy(
            min_length=6,
            require_uppercase=False,
            require_lowercase=False,
            require_digits=False,
            require_symbols=False,
        )
        assert lax.rules() == (
            PasswordRule.MIN_LENGTH,
            PasswordRule.MAX_LENGTH,
            PasswordRule.NO_SURROUNDING_SPACE,
        )

    def test_rule_keys_are_stable_strings(self):
        assert [r.value for r in PasswordRule] == [
            "min_length",
            "uppercase",
            "lowercase",
            "digits",
            "symbols",
            "max_length",
            "no_surrounding_space",
        ]


class TestViolations:
    def test_password_meeting_every_rule_has_no_violation(self):
        assert _DEFAULT.violations("Abcdefg1") == ()

    @pytest.mark.parametrize(
        ("password", "missing"),
        [
            ("Abcdef1", PasswordRule.MIN_LENGTH),
            ("abcdefg1", PasswordRule.UPPERCASE),
            ("ABCDEFG1", PasswordRule.LOWERCASE),
            ("Abcdefgh", PasswordRule.DIGITS),
        ],
    )
    def test_each_missing_rule_is_reported(self, password, missing):
        assert _DEFAULT.violations(password) == (missing,)

    def test_empty_password_reports_every_applicable_rule(self):
        assert _DEFAULT.violations("") == (
            PasswordRule.MIN_LENGTH,
            PasswordRule.UPPERCASE,
            PasswordRule.LOWERCASE,
            PasswordRule.DIGITS,
        )

    def test_length_is_counted_in_characters(self):
        assert _DEFAULT.violations("Aあいうえお1x") == ()

    def test_256_characters_is_the_upper_bound(self):
        assert MAX_PASSWORD_LENGTH == 256
        ok = "Aa1" + "x" * 253
        assert _DEFAULT.violations(ok) == ()
        assert _DEFAULT.violations(ok + "x") == (PasswordRule.MAX_LENGTH,)

    @pytest.mark.parametrize("password", [" Abcdefg1", "Abcdefg1 ", "\tAbcdefg1"])
    def test_leading_or_trailing_space_is_refused(self, password):
        assert PasswordRule.NO_SURROUNDING_SPACE in _DEFAULT.violations(password)

    def test_symbols_follow_the_cognito_set(self):
        assert COGNITO_SYMBOLS == frozenset("^$*.[]{}()?-\"!@#%&/\\,><':;|_~`+=")
        for symbol in COGNITO_SYMBOLS:
            assert _WITH_SYMBOLS.violations(f"Abcdefg1{symbol}") == (), symbol

    def test_inner_space_counts_as_a_symbol(self):
        assert _WITH_SYMBOLS.violations("Abcd efg1") == ()

    def test_letters_outside_the_cognito_set_are_not_symbols(self):
        assert _WITH_SYMBOLS.violations("Abcdefg1あ") == (PasswordRule.SYMBOLS,)

    def test_symbol_rule_is_ignored_when_not_required(self):
        assert _DEFAULT.violations("Abcdefg1") == ()

    @given(st.text(max_size=300))
    def test_violations_are_always_a_subset_of_the_rules(self, password):
        assert set(_WITH_SYMBOLS.violations(password)) <= set(_WITH_SYMBOLS.rules())
