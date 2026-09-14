"""Small string helpers shared across the app."""

import base64


def b64url_encode(raw: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    """Inverse of b64url_encode, restoring the stripped padding."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def slugify(text: str) -> str:
    """Lowercase, hyphen-separated form suitable for a URL path segment."""
    out = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    return "-".join(filter(None, "".join(out).split("-")))
