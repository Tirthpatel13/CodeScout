"""Agent loop tests, driven by a scripted LLM.

The point of the scripted provider is that these assert loop *behaviour* —
budget enforcement, citation validation, event ordering, error containment —
without a network call or a flaky model in the way.
"""

from __future__ import annotations

import pytest

from app.agent.loop import answer_question, build_system_prompt, run_agent, validate_citations
from app.agent.schemas import ParsedCitation, extract_citations, extract_confidence
from app.agent.tools import ToolContext
from app.config import get_settings  # noqa: F401
from app.providers.fake import FakeEmbeddings, ScriptedLLM


@pytest.fixture
async def ctx(session, indexed_run):
    return ToolContext(session, indexed_run, FakeEmbeddings())


# `settings` comes from conftest at session scope; do not shadow it here.


# --------------------------------------------------------------------------
# citation parsing
# --------------------------------------------------------------------------
def test_extract_citations_handles_ranges_and_single_lines():
    text = "Auth happens in [app/auth/middleware.py:24-58] and [app/auth/tokens.py:31]."
    cites = extract_citations(text)
    assert cites[0] == ParsedCitation("app/auth/middleware.py", 24, 58)
    assert cites[1] == ParsedCitation("app/auth/tokens.py", 31, 31)


def test_extract_citations_deduplicates():
    text = "[a/b.py:1-2] then again [a/b.py:1-2]"
    assert len(extract_citations(text)) == 1


def test_extract_confidence_strips_the_marker():
    body, confidence = extract_confidence("The answer.\n\nCONFIDENCE: high")
    assert body == "The answer."
    assert confidence == "high"


def test_missing_confidence_defaults_to_medium():
    body, confidence = extract_confidence("Just an answer.")
    assert confidence == "medium"
    assert body == "Just an answer."


# --------------------------------------------------------------------------
# citation validation against the index
# --------------------------------------------------------------------------
async def test_validate_citations_keeps_real_locations(ctx):
    kept, dropped = await validate_citations(ctx, [ParsedCitation("app/auth/tokens.py", 1, 5)])
    assert len(kept) == 1 and not dropped


async def test_validate_citations_drops_unknown_file(ctx):
    kept, dropped = await validate_citations(ctx, [ParsedCitation("app/does/not/exist.py", 1, 5)])
    assert not kept and len(dropped) == 1


async def test_validate_citations_drops_out_of_range_line(ctx):
    kept, dropped = await validate_citations(
        ctx, [ParsedCitation("app/auth/tokens.py", 99_999, 100_000)]
    )
    assert not kept and len(dropped) == 1


async def test_validate_citations_clamps_overlong_range(ctx):
    kept, _ = await validate_citations(ctx, [ParsedCitation("app/auth/tokens.py", 2, 99_999)])
    assert kept[0].end_line < 99_999


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------
async def test_agent_investigates_then_answers(ctx, settings):
    llm = ScriptedLLM(
        [
            [("find_symbol", {"name": "verify_token"})],
            [("read_file", {"path": "app/auth/tokens.py", "start_line": 28, "end_line": 50})],
            "Tokens are verified in [app/auth/tokens.py:31-48].\n\nCONFIDENCE: high",
        ]
    )
    answer = await answer_question(ctx, "How are tokens verified?", llm, settings=settings)

    assert len(answer.steps) == 2
    assert [s.tool_name for s in answer.steps] == ["find_symbol", "read_file"]
    assert answer.confidence == "high"
    assert answer.citations == [ParsedCitation("app/auth/tokens.py", 31, 48)]
    assert answer.citation_validity == 1.0
    assert answer.budget_exhausted is False
    assert answer.cost_usd > 0


async def test_agent_emits_events_in_order(ctx, settings):
    llm = ScriptedLLM(
        [
            [("grep", {"pattern": "verify_token"})],
            "Found it in [app/auth/tokens.py:31-48].\n\nCONFIDENCE: medium",
        ]
    )
    types = [e.type async for e in run_agent(ctx, "where?", llm, settings=settings)]

    assert types[0] == "step"
    assert types[1] == "step_result"
    assert types[-1] == "done"
    assert "token" in types
    assert types.index("citation") > types.index("token")


async def test_agent_drops_hallucinated_citations(ctx, settings):
    llm = ScriptedLLM(["Auth lives in [app/imaginary/file.py:10-20].\n\nCONFIDENCE: high"])
    answer = await answer_question(ctx, "where is auth?", llm, settings=settings)

    assert answer.citations == []
    assert len(answer.dropped_citations) == 1
    assert answer.citation_validity == 0.0
    # The prose is preserved; only the citation record is dropped.
    assert "imaginary" in answer.text


async def test_agent_enforces_the_tool_budget(ctx, settings):
    # Always asks for another tool call; only the budget can stop it.
    llm = ScriptedLLM([[("grep", {"pattern": "def"})]] * 50)
    answer = await answer_question(ctx, "tell me everything", llm, settings=settings)

    assert answer.budget_exhausted is True
    assert len(answer.steps) == settings.tool_budget


async def test_agent_survives_a_failing_tool(ctx, settings):
    llm = ScriptedLLM(
        [
            [("grep", {"pattern": "(unclosed"})],
            "That search failed, but auth is in [app/auth/middleware.py:1-10].\n\nCONFIDENCE: low",
        ]
    )
    answer = await answer_question(ctx, "where?", llm, settings=settings)

    assert answer.steps[0].is_error is True
    assert answer.confidence == "low"
    assert len(answer.citations) == 1


async def test_agent_handles_parallel_tool_calls_in_one_turn(ctx, settings):
    llm = ScriptedLLM(
        [
            [
                ("find_symbol", {"name": "verify_token"}),
                ("find_symbol", {"name": "extract_bearer"}),
            ],
            "Both exist.\n\nCONFIDENCE: high",
        ]
    )
    answer = await answer_question(ctx, "do both exist?", llm, settings=settings)
    assert len(answer.steps) == 2
    assert {s.idx for s in answer.steps} == {0, 1}


async def test_agent_error_event_on_provider_failure(ctx, settings):
    class BrokenLLM(ScriptedLLM):
        async def complete(self, **kwargs):
            raise RuntimeError("provider exploded")

    events = [e async for e in run_agent(ctx, "q", BrokenLLM([]), settings=settings)]
    assert events[-1].type == "error"
    assert "provider exploded" in events[-1].data["message"]


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------
def test_system_prompt_embeds_overview_and_budget(ctx, settings):
    prompt = build_system_prompt(ctx, settings)
    assert str(settings.tool_budget) in prompt
    assert "app/main.py" in prompt  # from the repo overview
    assert "CONFIDENCE:" in prompt


async def test_system_prompt_is_sent_to_the_model(ctx, settings):
    llm = ScriptedLLM(["Done.\n\nCONFIDENCE: low"])
    await answer_question(ctx, "q", llm, settings=settings)
    assert "CodeScout" in llm.calls[0]["system"]
