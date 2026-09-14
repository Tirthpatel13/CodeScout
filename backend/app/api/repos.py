from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select

from app.auth.crypto import TokenCipher
from app.auth.github import GitHubClient, GitHubError
from app.deps import (
    RedisDep,
    SessionDep,
    SettingsDep,
    UserDep,
    authorize_index_run,
    authorize_repository,
    enforce_rate_limit,
    get_cipher,
)
from app.models import IndexRun, RepoAccess, Repository, RunStatus

router = APIRouter(prefix="/api", tags=["repos"])
CipherDep = Annotated[TokenCipher, Depends(get_cipher)]

ACTIVE_STATUSES = tuple(s.value for s in RunStatus if s not in (RunStatus.READY, RunStatus.FAILED))


@router.get("/github/repos")
async def list_github_repos(
    user: UserDep,
    cipher: CipherDep,
    page: Annotated[int, Query(ge=1, le=50)] = 1,
) -> list[dict[str, Any]]:
    async with GitHubClient(cipher.decrypt(user.access_token)) as gh:
        try:
            return await gh.list_repos(page=page)
        except GitHubError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/repos/{owner}/{name}/index", status_code=status.HTTP_202_ACCEPTED)
async def start_index(
    owner: str,
    name: str,
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
    redis: RedisDep,
    cipher: CipherDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    await enforce_rate_limit(redis, f"rl:index:{user.id}", settings.index_jobs_per_hour, 3600)

    async with GitHubClient(cipher.decrypt(user.access_token)) as gh:
        try:
            meta = await gh.get_repo(owner, name)
        except GitHubError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    repo = (
        await session.execute(select(Repository).where(Repository.github_id == meta["github_id"]))
    ).scalar_one_or_none()
    if repo is None:
        repo = Repository(
            id=uuid.uuid4(),
            **{k: meta[k] for k in ("github_id", "owner", "name", "default_branch", "is_private")},
        )
        session.add(repo)
        await session.flush()

    if not (
        await session.execute(
            select(RepoAccess).where(
                RepoAccess.user_id == user.id, RepoAccess.repository_id == repo.id
            )
        )
    ).scalar_one_or_none():
        session.add(RepoAccess(user_id=user.id, repository_id=repo.id))

    # Idempotency: an in-flight run for this repo is returned rather than queued twice.
    existing = (
        (
            await session.execute(
                select(IndexRun)
                .where(IndexRun.repository_id == repo.id, IndexRun.status.in_(ACTIVE_STATUSES))
                .order_by(IndexRun.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return {"index_run_id": str(existing.id), "status": existing.status, "reused": True}

    run = IndexRun(
        id=uuid.uuid4(), repository_id=repo.id, commit_sha="", status=RunStatus.QUEUED.value
    )
    session.add(run)
    await session.flush()

    from app.worker import enqueue_ingest

    await enqueue_ingest(
        redis_url=settings.redis_url,
        run_id=run.id,
        user_id=user.id,
        idempotency_key=idempotency_key,
    )
    return {"index_run_id": str(run.id), "status": run.status, "reused": False}


@router.get("/repos/{repository_id}")
async def get_repo(repository_id: uuid.UUID, user: UserDep, session: SessionDep) -> dict[str, Any]:
    repo = await authorize_repository(repository_id, user, session)
    latest = (
        (
            await session.execute(
                select(IndexRun)
                .where(IndexRun.repository_id == repo.id, IndexRun.status == RunStatus.READY.value)
                .order_by(IndexRun.finished_at.desc())
            )
        )
        .scalars()
        .first()
    )
    return {
        "id": str(repo.id),
        "slug": repo.slug,
        "default_branch": repo.default_branch,
        "is_private": repo.is_private,
        "latest_run": None
        if latest is None
        else {
            "id": str(latest.id),
            "commit_sha": latest.commit_sha,
            "stats": latest.stats,
            "overview": latest.overview,
        },
    }


@router.get("/repos/{repository_id}/runs")
async def list_runs(
    repository_id: uuid.UUID, user: UserDep, session: SessionDep
) -> list[dict[str, Any]]:
    await authorize_repository(repository_id, user, session)
    runs = (
        (
            await session.execute(
                select(IndexRun)
                .where(IndexRun.repository_id == repository_id)
                .order_by(IndexRun.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "commit_sha": r.commit_sha,
            "status": r.status,
            "phase_pct": r.phase_pct,
            "stats": r.stats,
            "error": r.error,
            "created_at": r.created_at.isoformat(),
        }
        for r in runs
    ]


@router.get("/index-runs/{run_id}")
async def get_run(run_id: uuid.UUID, user: UserDep, session: SessionDep) -> dict[str, Any]:
    run = await authorize_index_run(run_id, user, session)
    return {
        "id": str(run.id),
        "repository_id": str(run.repository_id),
        "commit_sha": run.commit_sha,
        "status": run.status,
        "phase_pct": run.phase_pct,
        "stats": run.stats,
        "overview": run.overview,
        "error": run.error,
    }
