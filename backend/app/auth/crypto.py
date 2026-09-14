"""Encryption for stored GitHub tokens.

A GitHub OAuth token is a credential for someone else's private code. It is
encrypted at rest, never logged, and never serialised to the client.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(self._normalize(key))

    @staticmethod
    def _normalize(key: str) -> bytes:
        """Accept a real Fernet key, or derive one from a passphrase in dev."""
        if not key:
            raise ValueError(
                "token_encryption_key is not set. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet;'
                ' print(Fernet.generate_key().decode())"'
            )
        raw = key.encode()
        try:
            if len(base64.urlsafe_b64decode(raw)) == 32:
                return raw
        except Exception:  # noqa: BLE001, S110 - not a real key; derive one below
            pass
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise ValueError("stored token could not be decrypted") from exc
