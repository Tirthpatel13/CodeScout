"""Shared FastAPI dependencies: sessions, current user, access control, rate limits."""

from __future__ import annotations

import uuid
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import TokenCipher
from app.config import Settings, get_settings
from app.db import get_session
from app.models import IndexRun, RepoAccess, Repository, User

SESSION_COOKIE = "codescout_session"

_redis: aioredis.Redis | None = None


def get_redis(settings: Annotated[Settings, Depends(get_settings)]) -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def get_serializer(settings: Annotated[Settings, Depends(get_settings)]) -> URLSafeSerializer:
    return URLSafeSerializer(settings.session_secret, salt="codescout-session")


def get_cipher(settings: Annotated[Settings, Depends(get_settings)]) -> TokenCipher:
    return TokenCipher(settings.token_encryption_key or settings.session_secret)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


async def current_user(
    request: Request,
    session: SessionDep,
    serializer: Annotated[URLSafeSerializer, Depends(get_serializer)],
) -> User:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        payload = serializer.loads(raw)
        user_id = uuid.UUID(payload["user_id"])
    except (BadSignature, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session") from exc

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session user no longer exists")
    return user


UserDep = Annotated[User, Depends(current_user)]


async def authorize_repository(
    repository_id: uuid.UUID, user: User, session: AsyncSession
) -> Repository:
    """Every repo-scoped read goes through here.

    A missing grant is reported as 404, not 403, so the endpoint does not leak
    whether a private repository exists.
    """
    repo = (
        await session.execute(select(Repository).where(Repository.id == repository_id))
    ).scalar_one_or_none()
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")

    granted = (
        await session.execute(
            select(RepoAccess).where(
                RepoAccess.user_id == user.id, RepoAccess.repository_id == repository_id
            )
        )
    ).scalar_one_or_none()
    if granted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    return repo


async def authorize_index_run(run_id: uuid.UUID, user: User, session: AsyncSession) -> IndexRun:
    run = (
        await session.execute(select(IndexRun).where(IndexRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index run not found")
    await authorize_repository(run.repository_id, user, session)
    return run


async def enforce_rate_limit(
    redis: aioredis.Redis, key: str, limit: int, window_seconds: int
) -> None:
    """Fixed-window counter. Good enough at this scale; swap for a sliding
    window log if bursts at the boundary ever matter."""
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
    except Exception:  # noqa: BLE001 - never let a Redis blip take the API down
        return
    if count > limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded: {limit} per {window_seconds}s",
        )
