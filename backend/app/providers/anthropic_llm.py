"""Anthropic adapter.

The agent loop is hand-rolled rather than delegated to a framework. It is ~150
lines (see app/agent/loop.py), it makes the tool budget and citation validation
explicit, and it means there is no framework version to chase when the provider
SDK changes.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic

from app.providers.base import LLMResponse, ToolCall, ToolResult, ToolSpec, Usage

# USD per million tokens. Update alongside the model list; the numbers flow into
# per-message cost accounting and the eval report.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_DEFAULT_PRICE = (3.00, 15.00)


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, timeout: float = 120.0) -> None:
        if not api_key:
            raise ValueError("anthropic_api_key is not set")
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=3)
        self.model = model

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Cache the system prompt + repo overview across turns; it is the
            # largest static block and by far the cheapest win available.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]

        resp = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            ),
            stop_reason=resp.stop_reason or "end_turn",
        )

    def cost_usd(self, usage: Usage) -> float:
        in_rate, out_rate = PRICING.get(self.model, _DEFAULT_PRICE)
        # Cache reads bill at ~10% of the input rate.
        billable_in = usage.input_tokens + usage.cache_read_tokens * 0.1
        return (billable_in * in_rate + usage.output_tokens * out_rate) / 1_000_000

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
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in results
            ],
        }
