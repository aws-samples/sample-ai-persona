"""token_cipher の単体テスト（BR1.14、NFR3.2、security-design.md「暗号化と鍵」）。

鍵はテストの中で作る固定の文字列で、本物の秘密は使わない。
"""

import base64
import json

import pytest

from src.managers.shared.token_cipher import (
    TokenCipher,
    TokenDecryptError,
    TokenKeyConfigError,
)

_K1 = "test-key-material-version-one-0123456789abcdef"
_K2 = "test-key-material-version-two-0123456789abcdef"


def _keys(current: str = "v1", **keys: str) -> str:
    return json.dumps({"current": current, **(keys or {"k_v1": _K1})})


class TestRoundTrip:
    def test_decrypt_returns_the_original_text(self):
        cipher = TokenCipher(_keys())
        assert cipher.decrypt(cipher.encrypt("access-token-value")) == (
            "access-token-value"
        )

    def test_ciphertext_carries_the_key_version(self):
        cipher = TokenCipher(_keys())
        token = cipher.encrypt("access-token-value")
        assert token.startswith("v1.")
        assert "access-token-value" not in token
        assert cipher.current_version == "v1"

    def test_same_plaintext_gives_different_ciphertext(self):
        cipher = TokenCipher(_keys())
        assert cipher.encrypt("same") != cipher.encrypt("same")


class TestKeyRotation:
    def test_value_written_with_an_older_version_stays_readable(self):
        old = TokenCipher(_keys("v1", k_v1=_K1))
        token = old.encrypt("refresh-token-value")
        rotated = TokenCipher(_keys("v2", k_v1=_K1, k_v2=_K2))
        assert rotated.decrypt(token) == "refresh-token-value"
        assert rotated.encrypt("x").startswith("v2.")


class TestDecryptFailures:
    def test_unknown_key_version_fails(self):
        writer = TokenCipher(_keys("v2", k_v2=_K2))
        reader = TokenCipher(_keys("v1", k_v1=_K1))
        with pytest.raises(TokenDecryptError):
            reader.decrypt(writer.encrypt("token"))

    @pytest.mark.parametrize(
        "mutate",
        [
            # 暗号文の 1 バイトを書き換える（改ざん）
            lambda t: t[:-2] + ("A" if t[-2] != "A" else "B") + t[-1],
            # 版を別の版に付け替える（関連データの不一致）
            lambda t: "v9" + t[2:],
            # 形が壊れた値
            lambda t: "not-a-token",
            lambda t: "v1.%%%",
            lambda t: "v1." + base64.urlsafe_b64encode(b"short").decode(),
        ],
    )
    def test_tampered_or_broken_values_fail(self, mutate):
        cipher = TokenCipher(_keys("v1", k_v1=_K1, k_v9=_K2))
        with pytest.raises(TokenDecryptError):
            cipher.decrypt(mutate(cipher.encrypt("token")))


class TestKeyConfiguration:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            json.dumps(["v1"]),
            json.dumps({"k_v1": _K1}),
            json.dumps({"current": "v2", "k_v1": _K1}),
            json.dumps({"current": "v1", "k_v1": ""}),
            json.dumps({"current": "v1", "k_v1": 123}),
        ],
    )
    def test_malformed_key_list_fails_at_construction(self, raw):
        with pytest.raises(TokenKeyConfigError):
            TokenCipher(raw)
