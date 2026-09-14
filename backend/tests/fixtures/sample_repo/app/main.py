"""Application entrypoint."""

import os

from app.auth.middleware import AuthMiddleware
from app.auth.tokens import sign_token
from app.utils.strings import slugify

PUBLIC_PATHS = ("/health", "/login")


async def health(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def create_app():
    """Build the ASGI app with authentication wired in."""
    secret = os.environ.get("APP_SECRET", "dev-secret")
    return AuthMiddleware(health, secret=secret, public_paths=PUBLIC_PATHS)


def issue_demo_token(email: str) -> str:
    """Convenience helper used by the local dev server."""
    return sign_token({"sub": slugify(email), "email": email}, os.environ["APP_SECRET"])
