"""Deterministic fakes, so the whole suite runs offline with no API key.

FakeEmbeddings is a hashed bag-of-words projection rather than random noise. It
is a weak embedding model, but it is a *real* one: similar text genuinely lands
close together, so retrieval tests assert behaviour instead of asserting that a
mock was called.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from app.config import EMBEDDING_DIM
from app.providers.base import LLMResponse, ToolCall, ToolResult, ToolSpec, Usage

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _split_identifiers(token: str) -> list[str]:
    """verify_token -> [verify_token, verify, token]; parseJSON -> [parsejson, parse, json]"""
    parts = [token.lower()]
    snake = [p for p in token.split("_") if p]
    camel = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", token)
    for p in snake + camel:
        if len(p) > 2:
            parts.append(p.lower())
    return parts


class FakeEmbeddings:
    model = "fake-hash-v1"
    dimension = EMBEDDING_DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def _vec(self, text: str) -> list[float]:
        counts: Counter[str] = Counter()
        for raw in _TOKEN_RE.findall(text):
            for piece in _split_identifiers(raw):
                counts[piece] += 1

        vec = [0.0] * self.dimension
        for term, count in counts.items():
            digest = hashlib.blake2b(term.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


class ScriptedLLM:
    """Replays a fixed sequence of turns.

    Each scripted turn is either a list of (tool_name, arguments) tuples or a
    final string answer. Unscripted extra calls return a generic answer rather
    than raising, so a test that changes the tool budget does not explode.
    """

    def __init__(self, script: list[Any], model: str = "fake-llm") -> None:
        self.model = model
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages)})
        usage = Usage(input_tokens=100, output_tokens=50)

        if not self._script:
            return LLMResponse(text="No further information available.", usage=usage)

        turn = self._script.pop(0)
        if isinstance(turn, str):
            return LLMResponse(text=turn, usage=usage, stop_reason="end_turn")

        calls = [
            ToolCall(id=f"call_{len(self.calls)}_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(turn)
        ]
        return LLMResponse(tool_calls=calls, usage=usage, stop_reason="tool_use")

    def cost_usd(self, usage: Usage) -> float:
        return (usage.input_tokens * 3.0 + usage.output_tokens * 15.0) / 1_000_000

    def encode_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if response.text:
            content.append({"type": "text", "text": response.text})
        for call in response.tool_calls:
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return {"role": "assistant", "content": content}

    def encode_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
                for r in results
            ],
        }
