"""JWT creation and verification."""

import base64
import hashlib
import hmac
import json
import time

from app.utils.strings import b64url_decode, b64url_encode

DEFAULT_TTL_SECONDS = 3600


class TokenError(Exception):
    """Raised when a token is malformed, mis-signed, or expired."""


class ExpiredToken(TokenError):
    """Raised specifically when a token's exp claim is in the past."""


def sign_token(payload: dict, secret: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """Create a signed token carrying payload, valid for ttl seconds."""
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    encoded = b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signature = _compute_signature(encoded, secret)
    return f"{encoded}.{signature}"


def verify_token(token: str, secret: str) -> dict:
    """Verify a signed token and return its payload.

    Raises ExpiredToken if the exp claim has passed, TokenError otherwise.
    """
    try:
        encoded, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise TokenError("token is not in <payload>.<signature> form") from exc

    expected = _compute_signature(encoded, secret)
    if not hmac.compare_digest(expected, signature):
        raise TokenError("signature mismatch")

    payload = json.loads(b64url_decode(encoded))
    if payload.get("exp", 0) < time.time():
        raise ExpiredToken("token expired")
    return payload


def _compute_signature(encoded: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
