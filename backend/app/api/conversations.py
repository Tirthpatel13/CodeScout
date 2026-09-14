"""Chat endpoints, including the SSE stream that carries the investigation."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.loop import run_agent
from app.agent.tools import ToolContext
from app.db import session_scope
from app.deps import (
    RedisDep,
    SessionDep,
    SettingsDep,
    UserDep,
    authorize_repository,
    enforce_rate_limit,
)
from app.models import AgentStep, Citation, Conversation, File, IndexRun, Message, RunStatus
from app.providers import get_embedder, get_llm

router = APIRouter(prefix="/api/conversations", tags=["chat"])


class CreateConversation(BaseModel):
    repository_id: uuid.UUID
    title: str | None = Field(default=None, max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversation, user: UserDep, session: SessionDep
) -> dict[str, str]:
    await authorize_repository(body.repository_id, user, session)
    conversation = Conversation(
        id=uuid.uuid4(), repository_id=body.repository_id, user_id=user.id, title=body.title
    )
    session.add(conversation)
    await session.flush()
    return {"id": str(conversation.id)}


@router.get("")
async def list_conversations(
    user: UserDep, session: SessionDep, repository_id: uuid.UUID | None = Query(default=None)
) -> list[dict[str, Any]]:
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if repository_id:
        stmt = stmt.where(Conversation.repository_id == repository_id)
    rows = (
        (await session.execute(stmt.order_by(Conversation.created_at.desc()).limit(50)))
        .scalars()
        .all()
    )
    return [
        {
            "id": str(c.id),
            "repository_id": str(c.repository_id),
            "title": c.title,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


async def _load_conversation(conversation_id: uuid.UUID, user, session) -> Conversation:
    conversation = (
        await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conversation


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, user: UserDep, session: SessionDep
) -> dict[str, Any]:
    conversation = await _load_conversation(conversation_id, user, session)
    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    message_ids = [m.id for m in messages]
    steps = citations = []
    if message_ids:
        steps = (
            (
                await session.execute(
                    select(AgentStep)
                    .where(AgentStep.message_id.in_(message_ids))
                    .order_by(AgentStep.idx)
                )
            )
            .scalars()
            .all()
        )
        citations = (
            (await session.execute(select(Citation).where(Citation.message_id.in_(message_ids))))
            .scalars()
            .all()
        )

    return {
        "id": str(conversation.id),
        "repository_id": str(conversation.repository_id),
        "title": conversation.title,
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "confidence": m.confidence,
                "cost_usd": float(m.total_cost_usd) if m.total_cost_usd else None,
                "latency_ms": m.latency_ms,
                "truncated": m.truncated,
                "steps": [
                    {
                        "idx": s.idx,
                        "tool": s.tool_name,
                        "input": s.tool_input,
                        "summary": s.result_summary,
                        "ms": s.duration_ms,
                    }
                    for s in steps
                    if s.message_id == m.id
                ],
                "citations": [
                    {"path": c.path, "start": c.start_line, "end": c.end_line}
                    for c in citations
                    if c.message_id == m.id
                ],
            }
            for m in messages
        ],
    }


@router.post("/{conversation_id}/messages")
async def ask(
    conversation_id: uuid.UUID,
    body: AskRequest,
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> StreamingResponse:
    await enforce_rate_limit(redis, f"rl:ask:{user.id}", settings.questions_per_minute, 60)
    conversation = await _load_conversation(conversation_id, user, session)

    run = (
        (
            await session.execute(
                select(IndexRun)
                .where(
                    IndexRun.repository_id == conversation.repository_id,
                    IndexRun.status == RunStatus.READY.value,
                )
                .order_by(IndexRun.finished_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this repository has no completed index yet")

    run_id = run.id
    user_question = body.question

    async def stream() -> AsyncIterator[str]:
        # A fresh session: the request-scoped one closes when this handler
        # returns, which is before the stream has finished producing.
        async with session_scope() as db:
            fresh_run = (
                await db.execute(select(IndexRun).where(IndexRun.id == run_id))
            ).scalar_one()
            ctx = ToolContext(db, fresh_run, get_embedder(settings), settings)
            llm = get_llm(settings)

            db.add(
                Message(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    role="user",
                    content=user_question,
                    index_run_id=run_id,
                )
            )
            await db.flush()

            try:
                async for event in run_agent(ctx, user_question, llm, settings=settings):
                    if event.type == "done":
                        answer = event.data.pop("_answer")
                        message = Message(
                            id=uuid.uuid4(),
                            conversation_id=conversation_id,
                            role="assistant",
                            content=answer.text,
                            index_run_id=run_id,
                            confidence=answer.confidence,
                            prompt_version=settings.prompt_version,
                            model=llm.model,
                            total_cost_usd=answer.cost_usd,
                            latency_ms=answer.latency_ms,
                            truncated=answer.budget_exhausted,
                        )
                        db.add(message)
                        await db.flush()

                        db.add_all(
                            AgentStep(
                                id=uuid.uuid4(),
                                message_id=message.id,
                                idx=s.idx,
                                tool_name=s.tool_name,
                                tool_input=s.tool_input,
                                result_summary=s.result_summary,
                                duration_ms=s.duration_ms,
                            )
                            for s in answer.steps
                        )

                        paths = {c.path for c in answer.citations}
                        file_ids = dict(
                            (
                                await db.execute(
                                    select(File.path, File.id).where(
                                        File.index_run_id == run_id, File.path.in_(paths)
                                    )
                                )
                            ).all()
                        )
                        db.add_all(
                            Citation(
                                id=uuid.uuid4(),
                                message_id=message.id,
                                file_id=file_ids[c.path],
                                path=c.path,
                                start_line=c.start_line,
                                end_line=c.end_line,
                            )
                            for c in answer.citations
                            if c.path in file_ids
                        )
                        await db.flush()

                        event.data["message_id"] = str(message.id)
                        yield _sse("done", event.data)
                    else:
                        yield _sse(event.type, event.data)
            except Exception as exc:  # noqa: BLE001 - the stream owns its own errors
                yield _sse("error", {"code": type(exc).__name__, "message": str(exc)[:500]})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
