"""Provider registry."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "EmbeddingProvider",
    "LLMProvider",
    "LLMResponse",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "get_llm",
    "get_embedder",
]


def get_llm(settings: Settings | None = None, model: str | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider == "fake":
        from app.providers.fake import ScriptedLLM

        return ScriptedLLM(script=[])
    from app.providers.anthropic_llm import AnthropicLLM

    return AnthropicLLM(settings.anthropic_api_key, model or settings.answer_model)


def get_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    if settings.embedding_provider == "fake":
        from app.providers.fake import FakeEmbeddings

        return FakeEmbeddings()
    from app.providers.voyage_embed import VoyageEmbeddings

    return VoyageEmbeddings(settings.voyage_api_key, settings.embedding_model)
