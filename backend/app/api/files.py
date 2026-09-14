"""File content for the citation side-panel, plus the progress WebSocket."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.db import session_scope
from app.deps import SessionDep, UserDep, authorize_index_run
from app.ingest.progress import CHANNEL
from app.models import File, IndexRun

router = APIRouter(prefix="/api", tags=["files"])


@router.get("/index-runs/{run_id}/files/{path:path}")
async def read_file(
    run_id: uuid.UUID,
    path: str,
    user: UserDep,
    session: SessionDep,
    start: int = Query(default=1, ge=1),
    end: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    await authorize_index_run(run_id, user, session)
    file = (
        await session.execute(select(File).where(File.index_run_id == run_id, File.path == path))
    ).scalar_one_or_none()
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found in this index run")

    lines = file.content.split("\n")
    stop = min(len(lines), end or len(lines))
    return {
        "path": file.path,
        "language": file.language,
        "start": start,
        "end": stop,
        "total_lines": len(lines),
        "content": "\n".join(lines[start - 1 : stop]),
    }


@router.websocket("/ws/index-runs/{run_id}")
async def index_progress(websocket: WebSocket, run_id: uuid.UUID) -> None:
    """Live ingestion progress.

    Authenticated from the session cookie on connect, then relays Redis pub/sub
    frames. The last frame is cached so a client joining mid-run sees state
    immediately rather than waiting for the next phase transition.
    """
    from itsdangerous import BadSignature, URLSafeSerializer

    from app.config import get_settings
    from app.models import RepoAccess

    settings = get_settings()
    raw = websocket.cookies.get("codescout_session")
    try:
        payload = URLSafeSerializer(settings.session_secret, salt="codescout-session").loads(raw)
        user_id = uuid.UUID(payload["user_id"])
    except (BadSignature, TypeError, KeyError, ValueError):
        await websocket.close(code=4401)
        return

    async with session_scope() as db:
        run = (await db.execute(select(IndexRun).where(IndexRun.id == run_id))).scalar_one_or_none()
        if run is None:
            await websocket.close(code=4404)
            return
        granted = (
            await db.execute(
                select(RepoAccess).where(
                    RepoAccess.user_id == user_id,
                    RepoAccess.repository_id == run.repository_id,
                )
            )
        ).scalar_one_or_none()
        if granted is None:
            await websocket.close(code=4404)
            return
        snapshot = {
            "run_id": str(run.id),
            "status": run.status,
            "phase_pct": run.phase_pct,
            "stats": run.stats,
            "error": run.error,
        }

    await websocket.accept()
    await websocket.send_text(json.dumps(snapshot))

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL.format(run_id=run_id))
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
            if message is None:
                await websocket.send_text('{"type":"ping"}')
                continue
            await websocket.send_text(message["data"])
            frame = json.loads(message["data"])
            if frame.get("status") in ("ready", "failed"):
                break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
        await redis.aclose()
