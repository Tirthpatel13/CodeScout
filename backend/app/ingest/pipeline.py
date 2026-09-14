"""The ingestion orchestrator.

Six phases, each publishing progress. Every phase is idempotent with respect to
the index run it writes, so a retried job overwrites its own partial output
rather than duplicating it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ingest.chunk import PreparedChunk, chunk_file
from app.ingest.clone import CloneError, build_clone_url, cleanup, clone_repo
from app.ingest.embed import embed_chunks
from app.ingest.overview import build_overview
from app.ingest.parse import ParsedSymbol, parse_file
from app.ingest.progress import NullPublisher, ProgressEvent, ProgressPublisher
from app.ingest.walk import WalkedFile, walk_repo
from app.models import Chunk, File, IndexRun, Repository, RunStatus, Symbol, SymbolEdge
from app.providers.base import EmbeddingProvider, LLMProvider

log = structlog.get_logger(__name__)

PHASE_PCT = {
    RunStatus.CLONING: 5,
    RunStatus.PARSING: 25,
    RunStatus.RESOLVING: 45,
    RunStatus.EMBEDDING: 60,
    RunStatus.SUMMARIZING: 90,
    RunStatus.READY: 100,
}


class IngestError(RuntimeError):
    pass


@dataclass(slots=True)
class IngestOutcome:
    run_id: uuid.UUID
    commit_sha: str
    stats: dict[str, Any]


async def _set_status(
    session: AsyncSession,
    run: IndexRun,
    status: RunStatus,
    publisher: ProgressPublisher,
    *,
    message: str = "",
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    run.status = status.value
    run.phase_pct = PHASE_PCT.get(status, run.phase_pct)
    if stats is not None:
        run.stats = stats
    if error is not None:
        run.error = error
    await session.flush()
    await publisher.publish(
        ProgressEvent(
            run_id=str(run.id),
            status=status.value,
            phase_pct=run.phase_pct,
            message=message,
            stats=stats,
            error=error,
        )
    )


async def _clear_run_artifacts(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Make a retry idempotent. Cascades handle symbols/chunks/edges."""
    await session.execute(delete(File).where(File.index_run_id == run_id))
    await session.flush()


def _resolve_edges(
    symbols_by_qname: dict[str, uuid.UUID],
    symbols_by_name: dict[str, list[uuid.UUID]],
    raw_edges: list[tuple[str, str, str, int]],
    run_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Attach edges to real symbol ids where the target lives in this repo.

    Resolution order: exact qualified name, then unique bare name. An ambiguous
    bare name stays unresolved rather than guessing, because a wrong edge is
    worse than a missing one when the agent is traversing a call graph.
    """
    rows: list[dict[str, Any]] = []
    for src_qname, dst_name, kind, line in raw_edges:
        src_id = symbols_by_qname.get(src_qname)
        if src_id is None:
            continue

        dst_id = symbols_by_qname.get(dst_name)
        if dst_id is None:
            bare = dst_name.rsplit(".", 1)[-1]
            candidates = symbols_by_name.get(bare, [])
            if len(candidates) == 1:
                dst_id = candidates[0]

        rows.append(
            {
                "id": uuid.uuid4(),
                "index_run_id": run_id,
                "src_symbol_id": src_id,
                "dst_symbol_id": dst_id,
                "dst_name": dst_name,
                "kind": kind,
                "line": line,
            }
        )
    return rows


async def run_ingest(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    settings: Settings,
    embedder: EmbeddingProvider,
    llm: LLMProvider | None = None,
    publisher: ProgressPublisher | None = None,
    github_token: str | None = None,
    local_path: Path | None = None,
) -> IngestOutcome:
    """Index one repository snapshot.

    local_path bypasses cloning; the test suite and the eval harness use it to
    index a fixture directory without touching the network.
    """
    publisher = publisher or NullPublisher()
    started = time.perf_counter()

    run = (
        await session.execute(select(IndexRun).where(IndexRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise IngestError(f"index run {run_id} not found")

    repo = (
        await session.execute(select(Repository).where(Repository.id == run.repository_id))
    ).scalar_one()

    workspace = Path(settings.workspace_dir) / str(run_id)
    stats: dict[str, Any] = {}

    try:
        await _clear_run_artifacts(session, run_id)

        # -- phase 1: clone -------------------------------------------------
        await _set_status(session, run, RunStatus.CLONING, publisher, message="Fetching source")
        if local_path is not None:
            repo_root = local_path
            commit_sha = run.commit_sha or "0" * 40
        else:
            clone = await clone_repo(
                url=build_clone_url(repo.owner, repo.name, github_token),
                dest=workspace,
                branch=repo.default_branch,
                depth=settings.clone_depth,
            )
            repo_root = clone.path
            commit_sha = clone.commit_sha
        run.commit_sha = commit_sha

        # -- phase 2: walk --------------------------------------------------
        report = walk_repo(
            repo_root,
            max_files=settings.max_repo_files,
            max_file_bytes=settings.max_file_bytes,
        )
        if not report.files:
            raise IngestError("no indexable files found in repository")

        # -- phase 3: parse -------------------------------------------------
        await _set_status(
            session,
            run,
            RunStatus.PARSING,
            publisher,
            message=f"Parsing {len(report.files)} files",
        )

        file_rows: list[File] = []
        file_ids: dict[str, uuid.UUID] = {}
        for walked in report.files:
            file_id = uuid.uuid4()
            file_ids[walked.path] = file_id
            file_rows.append(
                File(
                    id=file_id,
                    index_run_id=run_id,
                    path=walked.path,
                    language=walked.language,
                    loc=walked.loc,
                    size_bytes=walked.size_bytes,
                    content_hash=walked.content_hash,
                    content=walked.text,
                )
            )
        session.add_all(file_rows)
        await session.flush()

        parsed_symbols: dict[str, list[ParsedSymbol]] = {}
        raw_edges: list[tuple[str, str, str, int]] = []
        symbol_rows: list[Symbol] = []
        symbols_by_qname: dict[str, uuid.UUID] = {}
        symbols_by_name: dict[str, list[uuid.UUID]] = {}
        qname_to_symbol: dict[str, ParsedSymbol] = {}

        for walked in report.files:
            if walked.kind != "code" or not walked.language:
                parsed_symbols[walked.path] = []
                continue
            parsed = parse_file(walked.path, walked.text, walked.language)
            parsed_symbols[walked.path] = parsed.symbols
            raw_edges.extend(
                (e.src_qualified_name, e.dst_name, e.kind, e.line) for e in parsed.edges
            )

            for sym in parsed.symbols:
                sym_id = uuid.uuid4()
                # Later definitions of the same qualified name win; that matches
                # Python/JS runtime semantics closely enough for navigation.
                symbols_by_qname[sym.qualified_name] = sym_id
                symbols_by_name.setdefault(sym.name, []).append(sym_id)
                qname_to_symbol[sym.qualified_name] = sym
                symbol_rows.append(
                    Symbol(
                        id=sym_id,
                        index_run_id=run_id,
                        file_id=file_ids[walked.path],
                        kind=sym.kind,
                        name=sym.name,
                        qualified_name=sym.qualified_name,
                        signature=sym.signature,
                        docstring=sym.docstring,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                    )
                )

        session.add_all(symbol_rows)
        await session.flush()

        # Parent links, now that every symbol has an id.
        for qname, sym_id in symbols_by_qname.items():
            parent_qname = qname.rsplit(".", 1)[0]
            if parent_qname != qname and parent_qname in symbols_by_qname:
                await session.execute(
                    Symbol.__table__.update()
                    .where(Symbol.id == sym_id)
                    .values(parent_id=symbols_by_qname[parent_qname])
                )

        # -- phase 4: resolve edges ------------------------------------------
        await _set_status(
            session,
            run,
            RunStatus.RESOLVING,
            publisher,
            message=f"Linking {len(raw_edges)} references",
        )
        edge_rows = _resolve_edges(symbols_by_qname, symbols_by_name, raw_edges, run_id)
        if edge_rows:
            for i in range(0, len(edge_rows), 5000):
                await session.execute(SymbolEdge.__table__.insert(), edge_rows[i : i + 5000])
        await session.flush()

        # -- phase 5: chunk and embed ----------------------------------------
        await _set_status(session, run, RunStatus.EMBEDDING, publisher, message="Embedding")

        prepared: list[tuple[WalkedFile, PreparedChunk]] = []
        for walked in report.files:
            chunks = chunk_file(
                walked.text,
                walked.kind,
                parsed_symbols.get(walked.path, []),
                max_tokens=settings.max_chunk_tokens,
                overlap=settings.chunk_overlap_lines,
            )
            prepared.extend((walked, c) for c in chunks)

        embed_result = await embed_chunks(
            session,
            [c for _, c in prepared],
            embedder,
            batch_size=settings.embed_batch_size,
        )

        chunk_rows: list[dict[str, Any]] = []
        for walked, chunk in prepared:
            chunk_rows.append(
                {
                    "id": uuid.uuid4(),
                    "index_run_id": run_id,
                    "file_id": file_ids[walked.path],
                    "symbol_id": symbols_by_qname.get(chunk.symbol_qualified_name or ""),
                    "kind": chunk.kind,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "token_count": chunk.token_count,
                    "embedding": embed_result.vectors.get(chunk.content_hash),
                }
            )
        for i in range(0, len(chunk_rows), 2000):
            await session.execute(Chunk.__table__.insert(), chunk_rows[i : i + 2000])
        await session.flush()

        # -- phase 6: overview -----------------------------------------------
        await _set_status(
            session, run, RunStatus.SUMMARIZING, publisher, message="Summarising repository"
        )
        run.overview = await build_overview(report.files, llm)

        stats = {
            "files": len(report.files),
            "symbols": len(symbol_rows),
            "edges": len(edge_rows),
            "edges_resolved": sum(1 for e in edge_rows if e["dst_symbol_id"] is not None),
            "chunks": len(chunk_rows),
            "loc": report.total_loc,
            "skipped": report.skipped,
            "embedding": embed_result.stats.as_dict(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        run.finished_at = _now()
        await _set_status(session, run, RunStatus.READY, publisher, message="Ready", stats=stats)

        log.info(
            "ingest.complete",
            run_id=str(run_id),
            repo=repo.slug,
            **{k: v for k, v in stats.items() if isinstance(v, int)},
        )
        return IngestOutcome(run_id=run_id, commit_sha=commit_sha, stats=stats)

    except (CloneError, IngestError) as exc:
        run.finished_at = _now()
        await _set_status(session, run, RunStatus.FAILED, publisher, error=str(exc)[:1000])
        raise
    except Exception as exc:  # noqa: BLE001 - surface the failure, never a half-index
        run.finished_at = _now()
        await _set_status(
            session, run, RunStatus.FAILED, publisher, error=f"{type(exc).__name__}: {exc}"[:1000]
        )
        log.exception("ingest.failed", run_id=str(run_id))
        raise
    finally:
        if local_path is None:
            cleanup(workspace)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
