"""GitHub OAuth and the small slice of the REST API we need."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"
SCOPES = "read:user repo"


class GitHubError(RuntimeError):
    pass


@dataclass(slots=True)
class GitHubUser:
    id: int
    login: str
    avatar_url: str | None


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode

    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )


class GitHubClient:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=API, headers=headers, timeout=timeout)

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def current_user(self) -> GitHubUser:
        resp = await self._client.get("/user")
        if resp.status_code != 200:
            raise GitHubError(f"GET /user returned {resp.status_code}")
        data = resp.json()
        return GitHubUser(id=data["id"], login=data["login"], avatar_url=data.get("avatar_url"))

    async def list_repos(self, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/user/repos",
            params={
                "page": page,
                "per_page": per_page,
                "sort": "updated",
                "affiliation": "owner,collaborator",
            },
        )
        if resp.status_code != 200:
            raise GitHubError(f"GET /user/repos returned {resp.status_code}")
        return [
            {
                "github_id": r["id"],
                "owner": r["owner"]["login"],
                "name": r["name"],
                "full_name": r["full_name"],
                "default_branch": r.get("default_branch") or "main",
                "is_private": r["private"],
                "description": r.get("description"),
                "language": r.get("language"),
                "pushed_at": r.get("pushed_at"),
            }
            for r in resp.json()
        ]

    async def get_repo(self, owner: str, name: str) -> dict[str, Any]:
        resp = await self._client.get(f"/repos/{owner}/{name}")
        if resp.status_code == 404:
            raise GitHubError(f"repository {owner}/{name} not found or not accessible")
        if resp.status_code != 200:
            raise GitHubError(f"GET /repos/{owner}/{name} returned {resp.status_code}")
        r = resp.json()
        return {
            "github_id": r["id"],
            "owner": r["owner"]["login"],
            "name": r["name"],
            "default_branch": r.get("default_branch") or "main",
            "is_private": r["private"],
            "size_kb": r.get("size", 0),
        }


async def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if resp.status_code != 200:
        raise GitHubError(f"token exchange returned {resp.status_code}")
    payload = resp.json()
    if "access_token" not in payload:
        raise GitHubError(payload.get("error_description", "token exchange failed"))
    return payload["access_token"]
