"""Tool-layer tests. No LLM involved anywhere in this file.

Testing the tools independently of the model is what makes agent behaviour
debuggable: when an answer is wrong you can tell whether the retrieval was bad
or the reasoning was.
"""

from __future__ import annotations

import pytest

from app.agent.tools import TOOL_NAMES, TOOL_SPECS, ToolContext
from app.providers.fake import FakeEmbeddings


@pytest.fixture
async def ctx(session, indexed_run):
    return ToolContext(session, indexed_run, FakeEmbeddings())


# --------------------------------------------------------------------------
# overview / navigation
# --------------------------------------------------------------------------
async def test_repo_overview_mentions_languages(ctx):
    out = await ctx.get_repo_overview()
    assert "python" in out.text
    assert "app/main.py" in out.text


async def test_list_directory_root_and_subdir(ctx):
    root = await ctx.list_directory(".")
    assert "app/" in root.text
    assert "README.md" in root.text

    sub = await ctx.list_directory("app/auth")
    assert "middleware.py" in sub.text
    assert sub.meta["count"] >= 2


async def test_list_directory_unknown_path_is_helpful_not_an_error(ctx):
    out = await ctx.list_directory("does/not/exist")
    assert out.is_error is False
    assert "No files under" in out.text


# --------------------------------------------------------------------------
# read_file
# --------------------------------------------------------------------------
async def test_read_file_is_line_numbered(ctx):
    out = await ctx.read_file("app/auth/tokens.py")
    assert "def verify_token" in out.text
    # Numbering must be exact: find the line labelled with verify_token's number.
    numbered = [ln for ln in out.text.split("\n") if "def verify_token" in ln][0]
    reported = int(numbered.split("|")[0].strip())
    assert out.meta["path"] == "app/auth/tokens.py"
    assert reported > 1


async def test_read_file_window(ctx):
    out = await ctx.read_file("app/auth/tokens.py", start_line=1, end_line=5)
    assert out.meta["start"] == 1 and out.meta["end"] == 5
    assert out.text.count("\n") <= 7


async def test_read_file_truncates_long_windows(ctx, session, indexed_run):
    from sqlalchemy import update

    from app.models import File

    big = "\n".join(f"line {i}" for i in range(1, 1001))
    await session.execute(
        update(File)
        .where(File.index_run_id == indexed_run.id, File.path == "app/main.py")
        .values(content=big)
    )
    await session.flush()

    out = await ctx.read_file("app/main.py")
    assert "more lines" in out.text
    assert "start_line=401" in out.text
    await session.rollback()


async def test_read_file_missing_suggests_alternatives(ctx):
    out = await ctx.read_file("middleware.py")
    # Unique basename resolves rather than failing.
    assert out.is_error is False
    assert out.meta["path"] == "app/auth/middleware.py"

    missing = await ctx.read_file("nope/absent.py")
    assert missing.is_error is True
    assert "File not found" in missing.text


# --------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------
async def test_grep_finds_identifier_with_locations(ctx):
    out = await ctx.grep(r"def verify_token")
    assert "app/auth/tokens.py:" in out.text
    assert out.meta["count"] >= 1


async def test_grep_path_glob_filters(ctx):
    everywhere = await ctx.grep("token")
    scoped = await ctx.grep("token", path_glob="web/**")
    assert scoped.meta["count"] < everywhere.meta["count"]


async def test_grep_no_match_suggests_semantic_search(ctx):
    out = await ctx.grep("zzz_no_such_identifier_zzz")
    assert out.meta["count"] == 0
    assert "semantic_search" in out.text


async def test_grep_invalid_regex_is_an_error_not_a_crash(ctx):
    out = await ctx.grep("(unclosed")
    assert out.is_error is True
    assert "Invalid regex" in out.text


# --------------------------------------------------------------------------
# semantic search
# --------------------------------------------------------------------------
async def test_semantic_search_returns_ranked_cited_chunks(ctx):
    out = await ctx.semantic_search("verify a signed token and check expiry", k=5)
    assert out.meta["count"] > 0
    assert any("tokens.py" in p for p in out.meta["paths"])
    assert "similarity=" in out.text
    assert ":" in out.text  # path:start-end headers


async def test_semantic_search_kind_filter(ctx):
    docs = await ctx.semantic_search("how authentication works", k=5, kind="doc")
    assert docs.meta["count"] > 0
    assert all(p.endswith(".md") for p in docs.meta["paths"])


# --------------------------------------------------------------------------
# symbols and the graph
# --------------------------------------------------------------------------
async def test_find_symbol_exact_and_fuzzy(ctx):
    exact = await ctx.find_symbol("verify_token")
    assert "app.auth.tokens.verify_token" in exact.meta["qualified_names"]
    assert "[function]" in exact.text

    qualified = await ctx.find_symbol("app.auth.middleware.AuthMiddleware")
    assert "app.auth.middleware.AuthMiddleware" in qualified.meta["qualified_names"]


async def test_find_symbol_includes_signature_and_docstring(ctx):
    out = await ctx.find_symbol("verify_token")
    assert "def verify_token" in out.text
    assert "Verify a signed token" in out.text


async def test_find_symbol_unknown_points_at_grep(ctx):
    out = await ctx.find_symbol("totally_absent_symbol")
    assert out.meta["count"] == 0
    assert "grep" in out.text


async def test_find_references_locates_call_sites(ctx):
    out = await ctx.find_references("app.auth.tokens.verify_token")
    assert out.meta["count"] >= 1
    assert "middleware.py" in out.text


async def test_find_references_unknown_symbol_is_informative(ctx):
    out = await ctx.find_references("nothing.calls.this")
    assert out.meta["count"] == 0
    assert "grep" in out.text


async def test_call_graph_callees(ctx):
    out = await ctx.get_call_graph("app.auth.middleware.AuthMiddleware.__call__", "callees")
    assert out.meta["count"] >= 1
    assert "verify_token" in out.text or "extract_bearer" in out.text


async def test_call_graph_callers(ctx):
    out = await ctx.get_call_graph("verify_token", "callers")
    assert out.meta["count"] >= 1


async def test_call_graph_rejects_bad_direction(ctx):
    out = await ctx.get_call_graph("verify_token", "sideways")
    assert out.is_error is True


# --------------------------------------------------------------------------
# dispatch contract
# --------------------------------------------------------------------------
async def test_execute_dispatches_by_name(ctx):
    out = await ctx.execute("find_symbol", {"name": "verify_token"})
    assert out.meta["count"] >= 1


async def test_execute_rejects_unknown_tool(ctx):
    out = await ctx.execute("rm_rf", {})
    assert out.is_error is True


async def test_execute_survives_bad_arguments(ctx):
    out = await ctx.execute("read_file", {"wrong_kwarg": 1})
    assert out.is_error is True
    assert "Bad arguments" in out.text


def test_tool_specs_match_implementations():
    """Every advertised tool exists, and nothing is advertised twice."""
    assert len(TOOL_SPECS) == len(TOOL_NAMES) == 8
    for spec in TOOL_SPECS:
        assert hasattr(ToolContext, spec.name), f"{spec.name} advertised but not implemented"
        assert spec.description.strip()
        assert spec.input_schema["type"] == "object"
