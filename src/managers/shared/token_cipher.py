"""
Encryption of OIDC tokens stored in LoginSession (BR1.14, NFR3.2).

The key list arrives as JSON (Secrets Manager ``token-encryption-keys``,
passed by ECS as the ``TOKEN_ENCRYPTION_KEYS`` environment variable)::

    {"current": "v1", "k_v1": "<random string>", "k_v2": "<random string>"}

Each ``k_<version>`` string is stretched with HKDF-SHA256 into a 32-byte
AES-256-GCM key; the string itself is never used as the key. ``current``
selects the key used for new ciphertexts, while every listed version stays
readable so that key rotation never produces unreadable values
(``security-design.md``).

Ciphertext format: ``<version>.<base64url(nonce || ciphertext+tag)>`` with a
fresh 96-bit nonce per value and the version bound as associated data.

This is a judgement-free helper (Shared layer): the Manager decides what a
decrypt failure means for the session.
"""

import base64
import binascii
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.models.errors import CodedError, ErrorCode

_NONCE_BYTES = 12
_KEY_BYTES = 32
_TAG_BYTES = 16
_HKDF_INFO_PREFIX = b"ai-persona/token-encryption/"


class TokenKeyConfigError(CodedError):
    """The key list is malformed. Raised at construction, i.e. at startup."""

    code = ErrorCode.AUTH_CONFIG_INVALID


class TokenDecryptError(CodedError):
    """A stored token could not be decrypted (unknown version, tampered, broken)."""

    code = ErrorCode.AUTH_REQUIRED


def _derive_key(material: str, version: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_HKDF_INFO_PREFIX + version.encode("utf-8"),
    )
    return hkdf.derive(material.encode("utf-8"))


def _parse_keys(raw: str) -> tuple[str, dict[str, AESGCM]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise TokenKeyConfigError("token key list is not valid JSON") from e
    if not isinstance(data, dict):
        raise TokenKeyConfigError("token key list must be a JSON object")
    current = data.get("current")
    if not isinstance(current, str) or not current:
        raise TokenKeyConfigError("token key list has no 'current' version")
    keys: dict[str, AESGCM] = {}
    for name, material in data.items():
        if not name.startswith("k_"):
            continue
        version = name[2:]
        if not version or "." in version:
            raise TokenKeyConfigError("token key version name is invalid")
        if not isinstance(material, str) or not material:
            raise TokenKeyConfigError("token key material must be a non-empty string")
        keys[version] = AESGCM(_derive_key(material, version))
    if current not in keys:
        raise TokenKeyConfigError("the 'current' version has no key")
    return current, keys


class TokenCipher:
    """AES-GCM encryption with versioned keys."""

    def __init__(self, keys_json: str) -> None:
        """Parse and derive every key.

        Raises:
            TokenKeyConfigError: The key list is malformed (fail at startup).
        """
        self._current, self._keys = _parse_keys(keys_json)

    @property
    def current_version(self) -> str:
        """The key version used for new ciphertexts."""
        return self._current

    def encrypt(self, plaintext: str) -> str:
        """Encrypt with the current key. A fresh nonce is used every time."""
        nonce = os.urandom(_NONCE_BYTES)
        version = self._current
        sealed = self._keys[version].encrypt(
            nonce, plaintext.encode("utf-8"), version.encode("utf-8")
        )
        payload = base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")
        return f"{version}.{payload}"

    def decrypt(self, token: str) -> str:
        """Decrypt a value written with any listed key version.

        Raises:
            TokenDecryptError: Unknown version, tampering, or a broken value.
                The message never contains the token.
        """
        version, sep, payload = token.partition(".")
        aead = self._keys.get(version)
        if not sep or aead is None:
            raise TokenDecryptError("token key version is unknown or missing")
        try:
            blob = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        except (binascii.Error, ValueError) as e:
            raise TokenDecryptError("token payload is not valid base64") from e
        if len(blob) < _NONCE_BYTES + _TAG_BYTES:
            raise TokenDecryptError("token payload is too short")
        nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            plain = aead.decrypt(nonce, sealed, version.encode("utf-8"))
        except InvalidTag as e:
            raise TokenDecryptError("token failed authentication") from e
        return plain.decode("utf-8")
