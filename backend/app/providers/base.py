"""Provider protocols.

The agent loop and the ingestion pipeline depend on these two interfaces and
nothing else. That is what lets the whole test suite run with no API key and no
network, deterministically, in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(slots=True)
class LLMResponse:
    """One assistant turn: some text, and/or some tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMProvider(Protocol):
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    def cost_usd(self, usage: Usage) -> float: ...

    def encode_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        """Render an assistant turn back into provider-native message format."""
        ...

    def encode_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        """Render tool results into a provider-native user turn."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str
    dimension: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
