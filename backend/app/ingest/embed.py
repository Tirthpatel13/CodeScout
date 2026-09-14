"""Phase 5b: embed chunks, reusing anything already embedded.

The cache is keyed on (content_hash, model) globally, not per repository. Re-indexing
a new commit only pays for code that actually changed, and two repos that vendor
the same file pay once. Cache hit rate is reported in the run stats.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.chunk import PreparedChunk
from app.models import EmbeddingCache
from app.providers.base import EmbeddingProvider


@dataclass(slots=True)
class EmbedStats:
    requested: int = 0
    cache_hits: int = 0
    embedded: int = 0
    failed: int = 0
    batches: int = 0

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / self.requested if self.requested else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "requested": self.requested,
            "cache_hits": self.cache_hits,
            "embedded": self.embedded,
            "failed": self.failed,
            "batches": self.batches,
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass(slots=True)
class EmbedResult:
    vectors: dict[str, list[float]] = field(default_factory=dict)
    stats: EmbedStats = field(default_factory=EmbedStats)


async def _load_cached(
    session: AsyncSession, hashes: list[str], model: str
) -> dict[str, list[float]]:
    if not hashes:
        return {}
    out: dict[str, list[float]] = {}
    # Chunked IN() so a large repo does not build a 40k-parameter statement.
    for i in range(0, len(hashes), 1000):
        window = hashes[i : i + 1000]
        rows = await session.execute(
            select(EmbeddingCache.content_hash, EmbeddingCache.embedding).where(
                EmbeddingCache.model == model, EmbeddingCache.content_hash.in_(window)
            )
        )
        for content_hash, vector in rows.all():
            out[content_hash] = list(vector)
    return out


async def _store_cached(
    session: AsyncSession, model: str, pairs: list[tuple[str, list[float]]]
) -> None:
    if not pairs:
        return
    stmt = pg_insert(EmbeddingCache).values(
        [{"content_hash": h, "model": model, "embedding": v} for h, v in pairs]
    )
    await session.execute(stmt.on_conflict_do_nothing(index_elements=["content_hash", "model"]))


async def embed_chunks(
    session: AsyncSession,
    chunks: list[PreparedChunk],
    embedder: EmbeddingProvider,
    *,
    batch_size: int = 96,
    max_retries: int = 3,
) -> EmbedResult:
    result = EmbedResult()
    if not chunks:
        return result

    # Deduplicate within this run too: repeated boilerplate is common.
    by_hash: dict[str, str] = {}
    for chunk in chunks:
        by_hash.setdefault(chunk.content_hash, chunk.content)

    result.stats.requested = len(by_hash)

    cached = await _load_cached(session, list(by_hash), embedder.model)
    result.vectors.update(cached)
    result.stats.cache_hits = len(cached)

    pending = [(h, text) for h, text in by_hash.items() if h not in cached]
    if not pending:
        return result

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [text for _, text in batch]
        vectors: list[list[float]] | None = None

        for attempt in range(max_retries):
            try:
                vectors = await embedder.embed_documents(texts)
                break
            except Exception:
                if attempt == max_retries - 1:
                    vectors = None
                    break
                await asyncio.sleep(2**attempt)

        result.stats.batches += 1
        if vectors is None or len(vectors) != len(batch):
            # A failed batch leaves those chunks without vectors. They remain
            # reachable via grep and symbol lookup, and the run is marked
            # degraded rather than silently incomplete.
            result.stats.failed += len(batch)
            continue

        pairs = [(h, vec) for (h, _), vec in zip(batch, vectors, strict=True)]
        result.vectors.update(dict(pairs))
        result.stats.embedded += len(pairs)
        await _store_cached(session, embedder.model, pairs)

    return result
