from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.ingest.chunk import chunk_file
from app.ingest.parse import parse_file
from app.ingest.walk import walk_repo
from app.models import Chunk, File, Symbol, SymbolEdge

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


# --------------------------------------------------------------------------
# walk
# --------------------------------------------------------------------------
def test_walk_finds_source_and_docs():
    report = walk_repo(FIXTURE, max_files=1000, max_file_bytes=1_000_000)
    paths = {f.path for f in report.files}

    assert "app/auth/tokens.py" in paths
    assert "web/src/client.ts" in paths
    assert "README.md" in paths
    assert all(".git/" not in p for p in paths)

    by_path = {f.path: f for f in report.files}
    assert by_path["app/auth/tokens.py"].language == "python"
    assert by_path["web/src/client.ts"].language == "typescript"
    assert by_path["README.md"].kind == "doc"
    assert by_path["pyproject.toml"].kind == "config"


def test_walk_respects_file_size_cap():
    report = walk_repo(FIXTURE, max_files=1000, max_file_bytes=10)
    assert report.files == []
    assert report.skipped.get("too_large", 0) > 0


def test_walk_hashes_are_stable():
    a = walk_repo(FIXTURE, max_files=1000, max_file_bytes=1_000_000)
    b = walk_repo(FIXTURE, max_files=1000, max_file_bytes=1_000_000)
    assert [f.content_hash for f in a.files] == [f.content_hash for f in b.files]


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------
def test_parse_python_symbols_and_lines():
    source = (FIXTURE / "app/auth/tokens.py").read_text()
    parsed = parse_file("app/auth/tokens.py", source, "python")
    by_name = {s.qualified_name: s for s in parsed.symbols}

    assert "app.auth.tokens.verify_token" in by_name
    verify = by_name["app.auth.tokens.verify_token"]
    assert verify.kind == "function"
    assert verify.docstring and "Verify a signed token" in verify.docstring

    # Line range must actually contain the definition.
    lines = source.split("\n")
    assert lines[verify.start_line - 1].startswith("def verify_token")
    assert verify.end_line > verify.start_line


def test_parse_marks_methods_inside_classes():
    source = (FIXTURE / "app/auth/middleware.py").read_text()
    parsed = parse_file("app/auth/middleware.py", source, "python")
    by_name = {s.qualified_name: s for s in parsed.symbols}

    assert by_name["app.auth.middleware.AuthMiddleware"].kind == "class"
    assert by_name["app.auth.middleware.AuthMiddleware.__call__"].kind == "method"
    assert by_name["app.auth.middleware.extract_bearer"].kind == "function"


def test_parse_records_calls_and_imports():
    source = (FIXTURE / "app/auth/middleware.py").read_text()
    parsed = parse_file("app/auth/middleware.py", source, "python")

    calls = {(e.src_qualified_name, e.dst_name) for e in parsed.edges if e.kind == "calls"}
    assert ("app.auth.middleware.AuthMiddleware.__call__", "verify_token") in calls
    assert ("app.auth.middleware.AuthMiddleware.__call__", "extract_bearer") in calls

    imports = {e.dst_name for e in parsed.edges if e.kind == "imports"}
    assert "app.auth.tokens" in imports


def test_parse_typescript():
    source = (FIXTURE / "web/src/client.ts").read_text()
    parsed = parse_file("web/src/client.ts", source, "typescript")
    kinds = {s.name: s.kind for s in parsed.symbols}

    assert kinds["ApiClient"] == "class"
    assert kinds["fetchUser"] == "method"
    assert kinds["buildClient"] == "function"
    assert kinds["Credentials"] == "interface"


def test_parse_unknown_language_is_empty_not_an_error():
    parsed = parse_file("a.rs", "fn main() {}", "rust")
    assert parsed.symbols == []
    assert parsed.edges == []


# --------------------------------------------------------------------------
# chunk
# --------------------------------------------------------------------------
def test_chunks_never_split_a_function():
    source = (FIXTURE / "app/auth/tokens.py").read_text()
    parsed = parse_file("app/auth/tokens.py", source, "python")
    chunks = chunk_file(source, "code", parsed.symbols, max_tokens=700, overlap=2)

    verify = next(
        c
        for c in chunks
        if c.symbol_qualified_name and c.symbol_qualified_name.endswith("verify_token")
    )
    assert "def verify_token" in verify.content
    assert "return payload" in verify.content


def test_chunks_cover_module_level_code():
    source = (FIXTURE / "app/auth/tokens.py").read_text()
    parsed = parse_file("app/auth/tokens.py", source, "python")
    chunks = chunk_file(source, "code", parsed.symbols, max_tokens=700, overlap=2)

    module_level = [c for c in chunks if c.symbol_qualified_name is None]
    assert any("import hmac" in c.content for c in module_level)


def test_markdown_chunks_split_on_headings():
    source = (FIXTURE / "README.md").read_text()
    chunks = chunk_file(source, "doc", [], max_tokens=700, overlap=2)
    headings = {c.heading for c in chunks if c.heading}
    assert "Authentication" in headings
    assert "Layout" in headings


def test_oversized_symbol_is_windowed_with_overlap():
    body = "\n".join(f"    x{i} = {i}" for i in range(4000))
    source = f"def huge():\n{body}\n"
    parsed = parse_file("huge.py", source, "python")
    chunks = chunk_file(source, "code", parsed.symbols, max_tokens=200, overlap=2)

    assert len(chunks) > 1
    assert chunks[0].end_line > chunks[1].start_line  # overlapping windows


# --------------------------------------------------------------------------
# pipeline (against a real database)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_persists_the_graph(session, indexed_run):
    run_id = indexed_run.id

    files = (await session.execute(select(File).where(File.index_run_id == run_id))).scalars().all()
    assert {f.path for f in files} >= {
        "app/auth/tokens.py",
        "app/auth/middleware.py",
        "web/src/client.ts",
        "README.md",
    }

    symbols = (
        (await session.execute(select(Symbol).where(Symbol.index_run_id == run_id))).scalars().all()
    )
    qnames = {s.qualified_name for s in symbols}
    assert "app.auth.tokens.verify_token" in qnames
    assert "app.auth.middleware.AuthMiddleware" in qnames

    # Parent links resolved.
    call_method = next(s for s in symbols if s.qualified_name.endswith("AuthMiddleware.__call__"))
    assert call_method.parent_id is not None

    chunk_count = (
        await session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.index_run_id == run_id)
        )
    ).scalar_one()
    assert chunk_count > 10

    embedded = (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.index_run_id == run_id, Chunk.embedding.isnot(None))
        )
    ).scalar_one()
    assert embedded == chunk_count


@pytest.mark.asyncio
async def test_pipeline_resolves_intra_repo_edges(session, indexed_run):
    rows = (
        (
            await session.execute(
                select(SymbolEdge).where(
                    SymbolEdge.index_run_id == indexed_run.id, SymbolEdge.kind == "calls"
                )
            )
        )
        .scalars()
        .all()
    )

    resolved = [e for e in rows if e.dst_symbol_id is not None]
    assert resolved, "expected at least one call edge resolved to a symbol in this repo"
    assert any(e.dst_name == "verify_token" for e in resolved)


@pytest.mark.asyncio
async def test_pipeline_records_stats_and_overview(session, indexed_run):
    await session.refresh(indexed_run)
    assert indexed_run.status == "ready"
    assert indexed_run.phase_pct == 100
    assert indexed_run.stats["files"] > 5
    assert indexed_run.stats["symbols"] > 5
    assert indexed_run.overview["languages"]["python"] > 0
    assert "app/main.py" in indexed_run.overview["entrypoints"]


@pytest.mark.asyncio
async def test_embedding_cache_is_reused_on_reindex(session, repository, settings):
    """The whole point of content-hash caching: a second run embeds nothing."""
    import uuid as _uuid

    from sqlalchemy import delete

    from app.ingest.pipeline import run_ingest
    from app.models import EmbeddingCache, IndexRun, RunStatus
    from app.providers.fake import FakeEmbeddings

    # The cache is global by (content_hash, model), so earlier tests in this
    # session have already warmed it for this fixture. Start cold.
    await session.execute(delete(EmbeddingCache))
    await session.commit()

    outcomes = []
    for sha in ("b" * 40, "c" * 40):
        run = IndexRun(
            id=_uuid.uuid4(),
            repository_id=repository.id,
            commit_sha=sha,
            status=RunStatus.QUEUED.value,
        )
        session.add(run)
        await session.commit()
        outcomes.append(
            await run_ingest(
                session,
                run_id=run.id,
                settings=settings,
                embedder=FakeEmbeddings(),
                llm=None,
                local_path=FIXTURE,
            )
        )
        await session.commit()

    first, second = outcomes
    assert first.stats["embedding"]["embedded"] > 0
    assert second.stats["embedding"]["cache_hits"] == second.stats["embedding"]["requested"]
    assert second.stats["embedding"]["embedded"] == 0
