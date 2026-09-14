"""Voyage AI embeddings.

voyage-code-3 is trained on code specifically; it noticeably beats general-purpose
text embedding models at retrieving a function from a natural-language
description of what it does.
"""

from __future__ import annotations

import asyncio

import voyageai

from app.config import EMBEDDING_DIM


class VoyageEmbeddings:
    def __init__(self, api_key: str, model: str = "voyage-code-3") -> None:
        if not api_key:
            raise ValueError("voyage_api_key is not set")
        self._client = voyageai.Client(api_key=api_key)
        self.model = model
        self.dimension = EMBEDDING_DIM

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        # The Voyage SDK is synchronous; keep the event loop free.
        result = await asyncio.to_thread(
            self._client.embed, texts, model=self.model, input_type=input_type
        )
        vectors: list[list[float]] = result.embeddings
        if vectors and len(vectors[0]) != self.dimension:
            raise ValueError(
                f"{self.model} returned dim {len(vectors[0])}, schema expects {self.dimension}. "
                "Changing the embedding model requires a migration."
            )
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text], "query")
        return vectors[0]
