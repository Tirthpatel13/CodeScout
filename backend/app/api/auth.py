from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from itsdangerous import URLSafeSerializer
from sqlalchemy import select

from app.auth.crypto import TokenCipher
from app.auth.github import GitHubClient, authorize_url, exchange_code
from app.deps import SESSION_COOKIE, SessionDep, SettingsDep, UserDep, get_cipher, get_serializer
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

SerializerDep = Annotated[URLSafeSerializer, Depends(get_serializer)]
CipherDep = Annotated[TokenCipher, Depends(get_cipher)]


@router.get("/github/start")
async def github_start(settings: SettingsDep) -> dict[str, str]:
    if not settings.github_client_id:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "GitHub OAuth is not configured")
    state = secrets.token_urlsafe(24)
    return {
        "url": authorize_url(
            settings.github_client_id,
            f"{settings.frontend_origin}/auth/callback",
            state,
        ),
        "state": state,
    }


@router.post("/github/callback")
async def github_callback(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    serializer: SerializerDep,
    cipher: CipherDep,
    code: Annotated[str, Query(min_length=8)],
) -> dict[str, object]:
    token = await exchange_code(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        code=code,
        redirect_uri=f"{settings.frontend_origin}/auth/callback",
    )

    async with GitHubClient(token) as gh:
        profile = await gh.current_user()

    user = (
        await session.execute(select(User).where(User.github_id == profile.id))
    ).scalar_one_or_none()
    encrypted = cipher.encrypt(token)

    if user is None:
        user = User(
            id=uuid.uuid4(),
            github_id=profile.id,
            login=profile.login,
            avatar_url=profile.avatar_url,
            access_token=encrypted,
        )
        session.add(user)
    else:
        user.login = profile.login
        user.avatar_url = profile.avatar_url
        user.access_token = encrypted
    await session.flush()

    # In prod the frontend and API live on different domains (e.g. Vercel and
    # Render), so the cookie must be sent cross-site: that requires
    # SameSite=None, which browsers only honor alongside Secure. In dev,
    # frontend and API are same-site (different localhost ports), so Lax
    # keeps working over plain HTTP.
    cross_site = settings.env == "prod"
    response.set_cookie(
        SESSION_COOKIE,
        serializer.dumps({"user_id": str(user.id)}),
        httponly=True,
        secure=cross_site,
        samesite="none" if cross_site else "lax",
        max_age=60 * 60 * 24 * 14,
    )
    return {"id": str(user.id), "login": user.login, "avatar_url": user.avatar_url}


@router.get("/me")
async def me(user: UserDep) -> dict[str, object]:
    return {"id": str(user.id), "login": user.login, "avatar_url": user.avatar_url}


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, settings: SettingsDep) -> None:
    # Must match the attributes the cookie was set with, or some browsers
    # won't treat this as the same cookie and it never actually clears.
    cross_site = settings.env == "prod"
    response.delete_cookie(
        SESSION_COOKIE, secure=cross_site, samesite="none" if cross_site else "lax"
    )
