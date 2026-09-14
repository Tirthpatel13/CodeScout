"""Request authentication middleware.

Every inbound request passes through AuthMiddleware, which pulls the bearer
token off the Authorization header and attaches the resolved user to the scope.
"""

from app.auth.tokens import ExpiredToken, TokenError, verify_token
from app.db.models import User


class AuthMiddleware:
    """ASGI middleware that resolves the current user for each request."""

    def __init__(self, app, secret: str, public_paths: tuple[str, ...] = ()):
        self.app = app
        self.secret = secret
        self.public_paths = public_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return

        token = extract_bearer(scope.get("headers", []))
        if token is None:
            await self._reject(send, 401, "missing bearer token")
            return

        try:
            payload = verify_token(token, self.secret)
        except ExpiredToken:
            await self._reject(send, 401, "token expired")
            return
        except TokenError:
            await self._reject(send, 401, "invalid token")
            return

        scope["user"] = User.from_claims(payload)
        await self.app(scope, receive, send)

    async def _reject(self, send, status: int, detail: str):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": detail.encode()})


def extract_bearer(headers) -> str | None:
    """Pull the token out of an Authorization: Bearer <token> header."""
    for key, value in headers:
        if key.lower() == b"authorization":
            decoded = value.decode()
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip()
    return None
