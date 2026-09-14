"""The agent loop.

Hand-rolled on purpose. It is short enough to read in one sitting, the tool
budget and the citation validation are explicit rather than buried in a
framework, and there is no orchestration library to re-learn when a provider
SDK changes.

The loop is an async generator of events so the API layer can stream the
investigation to the browser as it happens. Watching the tool calls arrive is
the part of the product that makes the approach legible.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from app.agent.schemas import (
    AgentAnswer,
    ParsedCitation,
    StepRecord,
    extract_citations,
    extract_confidence,
)
from app.agent.tools import TOOL_SPECS, ToolContext
from app.config import Settings, get_settings
from app.ingest.overview import render_for_prompt
from app.models import File
from app.providers.base import LLMProvider, ToolResult, Usage

log = structlog.get_logger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(slots=True)
class AgentEvent:
    type: str  # step | step_result | token | citation | done | error
    data: dict[str, Any] = field(default_factory=dict)


def load_prompt(version: str) -> str:
    path = PROMPT_DIR / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"unknown prompt version: {version}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(ctx: ToolContext, settings: Settings) -> str:
    slug = f"{ctx.run.repository.owner}/{ctx.run.repository.name}"
    return load_prompt(settings.prompt_version).format(
        tool_budget=settings.tool_budget,
        repo_overview=render_for_prompt(ctx.run.overview, slug),
    )


async def validate_citations(
    ctx: ToolContext, citations: list[ParsedCitation]
) -> tuple[list[ParsedCitation], list[ParsedCitation]]:
    """Drop any citation that does not resolve to real lines in this index run.

    This is the check that turns "the model said a file and a number" into
    something a user can click. The drop rate is a headline eval metric: if it
    climbs, the prompt or the tool output format has regressed.
    """
    if not citations:
        return [], []

    paths = {c.path for c in citations}
    rows = (
        await ctx.session.execute(
            select(File.path, File.loc).where(File.index_run_id == ctx.run_id, File.path.in_(paths))
        )
    ).all()
    loc_by_path = {path: loc for path, loc in rows}

    kept: list[ParsedCitation] = []
    dropped: list[ParsedCitation] = []
    for citation in citations:
        total = loc_by_path.get(citation.path)
        if total is None or citation.start_line < 1 or citation.start_line > total:
            dropped.append(citation)
        else:
            kept.append(
                ParsedCitation(
                    path=citation.path,
                    start_line=citation.start_line,
                    end_line=min(citation.end_line, total),
                )
            )
    return kept, dropped


async def run_agent(
    ctx: ToolContext,
    question: str,
    llm: LLMProvider,
    *,
    settings: Settings | None = None,
    history: list[dict[str, Any]] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Investigate, then answer. Yields events; the final one carries the answer."""
    settings = settings or settings_or_default()
    started = time.perf_counter()

    system = build_system_prompt(ctx, settings)
    messages: list[dict[str, Any]] = list(history or [])
    messages.append({"role": "user", "content": question})

    steps: list[StepRecord] = []
    usage_total = Usage()
    budget_exhausted = False
    final_text = ""

    try:
        for _ in range(settings.tool_budget + 1):
            remaining = settings.tool_budget - len(steps)
            if remaining <= 0:
                budget_exhausted = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool budget exhausted. Answer now with what you have, "
                            "and state that the investigation was cut short."
                        ),
                    }
                )

            response = await llm.complete(
                system=system,
                messages=messages,
                tools=None if budget_exhausted else TOOL_SPECS,
                max_tokens=settings.max_answer_tokens,
            )
            usage_total = usage_total + response.usage

            # Once the budget is spent the turn is final, whatever comes back.
            # Passing tools=None should be enough, but a provider that returns a
            # tool_use block anyway must not be able to buy an extra call.
            if budget_exhausted or not response.wants_tools:
                final_text = response.text
                break

            messages.append(llm.encode_assistant_turn(response))

            results: list[ToolResult] = []
            for call in response.tool_calls:
                idx = len(steps)
                yield AgentEvent("step", {"idx": idx, "tool": call.name, "input": call.arguments})

                call_started = time.perf_counter()
                output = await ctx.execute(call.name, call.arguments)
                duration_ms = int((time.perf_counter() - call_started) * 1000)

                steps.append(
                    StepRecord(
                        idx=idx,
                        tool_name=call.name,
                        tool_input=call.arguments,
                        result_summary=output.summary(),
                        duration_ms=duration_ms,
                        is_error=output.is_error,
                    )
                )
                results.append(
                    ToolResult(call_id=call.id, content=output.text, is_error=output.is_error)
                )
                yield AgentEvent(
                    "step_result",
                    {
                        "idx": idx,
                        "summary": output.summary(),
                        "ms": duration_ms,
                        "error": output.is_error,
                    },
                )

            messages.append(llm.encode_tool_results(results))
        else:
            budget_exhausted = True

        body, confidence = extract_confidence(final_text)
        kept, dropped = await validate_citations(ctx, extract_citations(body))

        # Stream the answer out in sentence-sized pieces. Token-level streaming
        # needs the provider's streaming API threaded through the tool loop;
        # this keeps the UI responsive without pretending to be that.
        for piece in _chunk_text(body):
            yield AgentEvent("token", {"text": piece})

        for citation in kept:
            yield AgentEvent(
                "citation",
                {
                    "path": citation.path,
                    "start": citation.start_line,
                    "end": citation.end_line,
                },
            )

        answer = AgentAnswer(
            text=body,
            citations=kept,
            dropped_citations=dropped,
            confidence=confidence,
            steps=steps,
            budget_exhausted=budget_exhausted,
            cost_usd=llm.cost_usd(usage_total),
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=usage_total.input_tokens,
            tokens_out=usage_total.output_tokens,
        )

        if dropped:
            log.warning(
                "agent.citations_dropped",
                run_id=str(ctx.run_id),
                dropped=[(c.path, c.start_line) for c in dropped],
            )

        yield AgentEvent(
            "done",
            {
                "answer": answer.text,
                "confidence": answer.confidence,
                "citations": [
                    {"path": c.path, "start": c.start_line, "end": c.end_line}
                    for c in answer.citations
                ],
                "dropped_citations": len(answer.dropped_citations),
                "citation_validity": round(answer.citation_validity, 4),
                "tool_calls": len(steps),
                "budget_exhausted": budget_exhausted,
                "cost_usd": round(answer.cost_usd, 6),
                "ms": answer.latency_ms,
                "_answer": answer,
            },
        )

    except Exception as exc:  # noqa: BLE001 - one failed turn must not kill the stream
        log.exception("agent.failed", run_id=str(ctx.run_id))
        yield AgentEvent("error", {"code": type(exc).__name__, "message": str(exc)[:500]})


def _chunk_text(text: str, size: int = 120) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    buffer = ""
    for word in text.split(" "):
        buffer = f"{buffer} {word}" if buffer else word
        if len(buffer) >= size:
            out.append(buffer + " ")
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


def settings_or_default() -> Settings:
    return get_settings()


async def answer_question(
    ctx: ToolContext,
    question: str,
    llm: LLMProvider,
    *,
    settings: Settings | None = None,
) -> AgentAnswer:
    """Non-streaming convenience wrapper, used by the eval harness and tests."""
    answer: AgentAnswer | None = None
    error: dict[str, Any] | None = None
    async for event in run_agent(ctx, question, llm, settings=settings):
        if event.type == "done":
            answer = event.data["_answer"]
        elif event.type == "error":
            error = event.data
    if answer is None:
        raise RuntimeError(f"agent produced no answer: {error}")
    return answer
