"""ARQ worker: ingestion runs here, not in the request path.

Indexing a real repository takes minutes. The API enqueues and returns 202; this
process does the work and publishes progress to Redis, which the WebSocket
relays to the browser.
"""

from __future__ import annotations

import uuid
from typing import Any

import redis.asyncio as aioredis
import structlog
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.auth.crypto import TokenCipher
from app.config import get_settings
from app.db import dispose_engine, session_scope
from app.ingest.pipeline import run_ingest
from app.ingest.progress import ProgressPublisher
from app.logging import configure_logging
from app.models import User
from app.providers import get_embedder, get_llm

log = structlog.get_logger(__name__)


async def ingest_repository(
    ctx: dict[str, Any],
    run_id: str,
    user_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    redis: aioredis.Redis = ctx["redis_client"]
    publisher = ProgressPublisher(redis)

    async with session_scope() as session:
        github_token: str | None = None
        if user_id:
            user = (
                await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
            ).scalar_one_or_none()
            if user is not None:
                github_token = TokenCipher(
                    settings.token_encryption_key or settings.session_secret
                ).decrypt(user.access_token)

        outcome = await run_ingest(
            session,
            run_id=uuid.UUID(run_id),
            settings=settings,
            embedder=get_embedder(settings),
            llm=get_llm(settings, model=settings.overview_model),
            publisher=publisher,
            github_token=github_token,
        )

    log.info(
        "worker.ingest_done",
        run_id=run_id,
        **{k: v for k, v in outcome.stats.items() if isinstance(v, int)},
    )
    return {"run_id": run_id, "commit_sha": outcome.commit_sha}


async def enqueue_ingest(
    *,
    redis_url: str,
    run_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> None:
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        await pool.enqueue_job(
            "ingest_repository",
            str(run_id),
            str(user_id) if user_id else None,
            idempotency_key,
            # ARQ deduplicates on job id: a retried request with the same
            # Idempotency-Key never queues a second run.
            _job_id=f"ingest:{idempotency_key or run_id}",
        )
    finally:
        await pool.aclose()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.env)
    ctx["redis_client"] = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("worker.startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["redis_client"].aclose()
    await dispose_engine()


class WorkerSettings:
    functions = [ingest_repository]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
    job_timeout = 1800
    keep_result = 3600
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
