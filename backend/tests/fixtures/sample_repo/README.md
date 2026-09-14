# Sample Service

A tiny ASGI service used as a parsing fixture.

## Authentication

Requests carry a bearer token in the `Authorization` header. `AuthMiddleware`
verifies it and attaches the resolved `User` to the ASGI scope. Tokens are
HMAC-SHA256 signed and carry an `exp` claim.

## Layout

- `app/auth` — token signing, verification, and the middleware
- `app/db` — dataclass models
- `app/utils` — shared string helpers
- `web/src` — the TypeScript API client
