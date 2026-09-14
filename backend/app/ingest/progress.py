"""Progress fan-out: worker -> Redis pub/sub -> WebSocket -> browser."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as aioredis

CHANNEL = "index_run:{run_id}"


@dataclass(slots=True)
class ProgressEvent:
    run_id: str
    status: str
    phase_pct: int
    message: str = ""
    stats: dict[str, Any] | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class ProgressPublisher:
    def __init__(self, redis: aioredis.Redis | None) -> None:
        self._redis = redis

    async def publish(self, event: ProgressEvent) -> None:
        if self._redis is None:
            return
        await self._redis.publish(CHANNEL.format(run_id=event.run_id), event.to_json())
        # Keep the latest frame so a client connecting mid-run sees state
        # immediately instead of waiting for the next phase transition.
        await self._redis.set(f"index_run_state:{event.run_id}", event.to_json(), ex=3600)


class NullPublisher(ProgressPublisher):
    def __init__(self) -> None:
        super().__init__(None)
