"""The agent's tools: how it investigates a repository.

This is the file that makes CodeScout not a RAG demo. Instead of retrieving
top-k chunks and stuffing them into a prompt, the model gets the same primitives
a human uses — a file tree, a file reader, grep, symbol lookup, reference
lookup, a call graph — plus semantic search as one option among several.

Two invariants hold for every tool:

1. Output carries real file paths and 1-based line numbers. Citations are only
   trustworthy because the model never has to invent a location.
2. Output is bounded and says when it was truncated, so the model knows the
   difference between "no more results" and "more results not shown".
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.ingest.overview import render_for_prompt
from app.models import Chunk, File, IndexRun, Symbol, SymbolEdge
from app.providers.base import EmbeddingProvider, ToolSpec

MAX_READ_LINES = 400
MAX_GREP_RESULTS = 40
MAX_TOOL_CHARS = 12_000
MAX_CALL_GRAPH_DEPTH = 3
MAX_CALL_GRAPH_BREADTH = 25


@dataclass(slots=True)
class ToolOutput:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False

    def summary(self) -> str:
        first = self.text.strip().split("\n", 1)[0]
        return first[:200]


def _truncate(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [output truncated at {limit} characters]"


def _numbered(content: str, start_line: int) -> str:
    lines = content.split("\n")
    width = len(str(start_line + len(lines) - 1))
    return "\n".join(f"{str(start_line + i).rjust(width)} | {line}" for i, line in enumerate(lines))


class ToolContext:
    """Every tool is scoped to one immutable index run."""

    def __init__(
        self,
        session: AsyncSession,
        index_run: IndexRun,
        embedder: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.run = index_run
        self.run_id = index_run.id
        self.embedder = embedder
        self.settings = settings or get_settings()
        self._path_cache: list[str] | None = None

    # -- helpers --------------------------------------------------------
    def _files(self) -> Select[Any]:
        return select(File).where(File.index_run_id == self.run_id)

    async def _all_paths(self) -> list[str]:
        if self._path_cache is None:
            rows = await self.session.execute(
                select(File.path).where(File.index_run_id == self.run_id).order_by(File.path)
            )
            self._path_cache = [r[0] for r in rows.all()]
        return self._path_cache

    async def _file_by_path(self, path: str) -> File | None:
        normalized = path.strip().lstrip("./")
        row = await self.session.execute(self._files().where(File.path == normalized))
        found = row.scalar_one_or_none()
        if found is not None:
            return found
        # Be forgiving about a model citing a basename or a partial path.
        candidates = [p for p in await self._all_paths() if p.endswith("/" + normalized)]
        if len(candidates) == 1:
            row = await self.session.execute(self._files().where(File.path == candidates[0]))
            return row.scalar_one_or_none()
        return None

    # -- tools ----------------------------------------------------------
    async def get_repo_overview(self) -> ToolOutput:
        slug = f"{self.run.repository.owner}/{self.run.repository.name}"
        return ToolOutput(text=render_for_prompt(self.run.overview, slug))

    async def list_directory(self, path: str = ".", depth: int = 2) -> ToolOutput:
        depth = max(1, min(depth, 3))
        prefix = "" if path in (".", "", "/") else path.strip("/") + "/"
        paths = [p for p in await self._all_paths() if p.startswith(prefix)]
        if not paths:
            return ToolOutput(
                text=f"No files under '{path}'. Use list_directory('.') to see the repo root.",
                meta={"count": 0},
            )

        entries: dict[str, str] = {}
        for full in paths:
            rest = full[len(prefix) :]
            parts = rest.split("/")
            if len(parts) <= depth:
                entries[rest] = "file"
            else:
                entries["/".join(parts[:depth]) + "/"] = "dir"

        lines = [f"{prefix or '.'} ({len(paths)} indexed files below)"]
        for name in sorted(entries, key=lambda n: (entries[n] == "file", n)):
            lines.append(f"  {name}")
        return ToolOutput(text=_truncate("\n".join(lines)), meta={"count": len(paths)})

    async def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ToolOutput:
        file = await self._file_by_path(path)
        if file is None:
            close = [p for p in await self._all_paths() if path.split("/")[-1] in p][:5]
            hint = ("\nDid you mean: " + ", ".join(close)) if close else ""
            return ToolOutput(text=f"File not found: {path}{hint}", is_error=True)

        lines = file.content.split("\n")
        total = len(lines)
        start = max(1, start_line or 1)
        end = min(total, end_line or total)
        if start > total:
            return ToolOutput(
                text=f"{file.path} has only {total} lines; {start} is past the end.",
                is_error=True,
            )

        truncated = False
        if end - start + 1 > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1
            truncated = True

        body = _numbered("\n".join(lines[start - 1 : end]), start)
        header = f"{file.path} (lines {start}-{end} of {total}, {file.language or 'text'})"
        footer = (
            f"\n... [{total - end} more lines; call read_file again with start_line={end + 1}]"
            if truncated
            else ""
        )
        return ToolOutput(
            text=_truncate(f"{header}\n{body}{footer}"),
            meta={"path": file.path, "start": start, "end": end, "total": total},
        )

    async def grep(
        self, pattern: str, path_glob: str | None = None, max_results: int = MAX_GREP_RESULTS
    ) -> ToolOutput:
        max_results = max(1, min(max_results, MAX_GREP_RESULTS))
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolOutput(text=f"Invalid regex {pattern!r}: {exc}", is_error=True)

        # Narrow in Postgres first so only candidate files come back over the wire.
        stmt = self._files().where(File.content.op("~")(pattern))
        try:
            rows = (await self.session.execute(stmt)).scalars().all()
        except Exception:
            # Postgres and Python regex dialects differ; fall back to scanning.
            await self.session.rollback()
            rows = (await self.session.execute(self._files())).scalars().all()

        matches: list[str] = []
        files_hit = 0
        for file in sorted(rows, key=lambda f: f.path):
            if path_glob and not fnmatch.fnmatch(file.path, path_glob):
                continue
            hit_in_file = False
            for i, line in enumerate(file.content.split("\n"), start=1):
                if regex.search(line):
                    hit_in_file = True
                    matches.append(f"{file.path}:{i}: {line.strip()[:200]}")
                    if len(matches) >= max_results:
                        break
            files_hit += int(hit_in_file)
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolOutput(
                text=f"No matches for {pattern!r}"
                + (f" in {path_glob}" if path_glob else "")
                + ". Try semantic_search for a conceptual query.",
                meta={"count": 0},
            )

        capped = len(matches) >= max_results
        footer = f"\n[showing first {max_results}; narrow with path_glob]" if capped else ""
        return ToolOutput(
            text=_truncate(
                f"{len(matches)} match(es) in {files_hit} file(s):\n" + "\n".join(matches) + footer
            ),
            meta={"count": len(matches), "files": files_hit, "capped": capped},
        )

    async def semantic_search(self, query: str, k: int = 10, kind: str | None = None) -> ToolOutput:
        k = max(1, min(k, 25))
        vector = await self.embedder.embed_query(query)

        stmt = (
            select(
                Chunk.start_line,
                Chunk.end_line,
                Chunk.content,
                Chunk.kind,
                File.path,
                Symbol.qualified_name,
                Chunk.embedding.cosine_distance(vector).label("distance"),
            )
            .join(File, Chunk.file_id == File.id)
            .outerjoin(Symbol, Chunk.symbol_id == Symbol.id)
            .where(Chunk.index_run_id == self.run_id, Chunk.embedding.isnot(None))
            .order_by("distance")
            .limit(k)
        )
        if kind:
            stmt = stmt.where(Chunk.kind == kind)

        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return ToolOutput(text=f"No semantic matches for {query!r}.", meta={"count": 0})

        blocks: list[str] = []
        for start, end, content, chunk_kind, path, qname, distance in rows:
            label = qname or f"{chunk_kind} block"
            score = round(1.0 - float(distance), 3)
            body = content if len(content) < 1800 else content[:1800] + "\n... [chunk truncated]"
            blocks.append(
                f"--- {path}:{start}-{end}  [{label}]  similarity={score}\n{_numbered(body, start)}"
            )

        return ToolOutput(
            text=_truncate(f"{len(rows)} result(s) for {query!r}:\n\n" + "\n\n".join(blocks)),
            meta={"count": len(rows), "paths": [r[4] for r in rows]},
        )

    async def find_symbol(self, name: str) -> ToolOutput:
        bare = name.rsplit(".", 1)[-1]
        stmt = (
            select(Symbol, File.path)
            .join(File, Symbol.file_id == File.id)
            .where(
                Symbol.index_run_id == self.run_id,
                (Symbol.qualified_name == name)
                | (Symbol.name == bare)
                | (Symbol.name.ilike(f"%{bare}%")),
            )
            .order_by(
                # Exact matches first, then prefix matches, then fuzzy.
                (Symbol.name != bare),
                func.length(Symbol.name),
                Symbol.qualified_name,
            )
            .limit(30)
        )
        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return ToolOutput(
                text=f"No symbol matching {name!r}. Try grep for a plain-text search.",
                meta={"count": 0},
            )

        lines = [f"{len(rows)} symbol(s) matching {name!r}:"]
        for symbol, path in rows:
            doc = f"  — {symbol.docstring.splitlines()[0][:120]}" if symbol.docstring else ""
            lines.append(
                f"  [{symbol.kind}] {symbol.qualified_name}\n"
                f"      {path}:{symbol.start_line}-{symbol.end_line}\n"
                f"      {symbol.signature or ''}{doc}"
            )
        return ToolOutput(
            text=_truncate("\n".join(lines)),
            meta={"count": len(rows), "qualified_names": [s.qualified_name for s, _ in rows]},
        )

    async def find_references(self, qualified_name: str, limit: int = 50) -> ToolOutput:
        limit = max(1, min(limit, 100))
        bare = qualified_name.rsplit(".", 1)[-1]

        target_ids = (
            (
                await self.session.execute(
                    select(Symbol.id).where(
                        Symbol.index_run_id == self.run_id,
                        (Symbol.qualified_name == qualified_name) | (Symbol.name == bare),
                    )
                )
            )
            .scalars()
            .all()
        )

        src = Symbol.__table__.alias("src")
        stmt = (
            select(
                src.c.qualified_name,
                src.c.kind,
                File.path,
                SymbolEdge.line,
                SymbolEdge.kind.label("edge_kind"),
            )
            .join(src, SymbolEdge.src_symbol_id == src.c.id)
            .join(File, src.c.file_id == File.id)
            .where(SymbolEdge.index_run_id == self.run_id)
            .order_by(File.path, SymbolEdge.line)
            .limit(limit)
        )
        stmt = stmt.where(
            SymbolEdge.dst_symbol_id.in_(target_ids)
            if target_ids
            else (SymbolEdge.dst_name == qualified_name) | (SymbolEdge.dst_name == bare)
        )

        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return ToolOutput(
                text=(
                    f"No references to {qualified_name!r} found in the code graph. "
                    "It may be called dynamically, or only from outside this repo — "
                    "grep is a good cross-check."
                ),
                meta={"count": 0},
            )

        lines = [f"{len(rows)} reference(s) to {qualified_name}:"]
        lines += [
            f"  {path}:{line}  {edge_kind} from {src_qname} [{src_kind}]"
            for src_qname, src_kind, path, line, edge_kind in rows
        ]
        return ToolOutput(text=_truncate("\n".join(lines)), meta={"count": len(rows)})

    async def get_call_graph(
        self, qualified_name: str, direction: str = "callees", depth: int = 2
    ) -> ToolOutput:
        if direction not in ("callers", "callees"):
            return ToolOutput(text="direction must be 'callers' or 'callees'", is_error=True)
        depth = max(1, min(depth, MAX_CALL_GRAPH_DEPTH))
        bare = qualified_name.rsplit(".", 1)[-1]

        roots = (
            await self.session.execute(
                select(Symbol.id, Symbol.qualified_name).where(
                    Symbol.index_run_id == self.run_id,
                    (Symbol.qualified_name == qualified_name) | (Symbol.name == bare),
                )
            )
        ).all()
        if not roots:
            return ToolOutput(text=f"Unknown symbol {qualified_name!r}.", meta={"count": 0})

        lines: list[str] = [f"{direction} of {roots[0][1]} (depth {depth}):"]
        seen: set[Any] = {roots[0][0]}
        frontier = [(roots[0][0], roots[0][1])]
        total = 0

        for level in range(1, depth + 1):
            if not frontier:
                break
            next_frontier: list[tuple[Any, str]] = []
            for node_id, node_name in frontier:
                other = Symbol.__table__.alias(f"other_{level}")
                if direction == "callees":
                    stmt = (
                        select(other.c.id, other.c.qualified_name, File.path, SymbolEdge.line)
                        .select_from(SymbolEdge)
                        .join(other, SymbolEdge.dst_symbol_id == other.c.id)
                        .join(File, other.c.file_id == File.id)
                        .where(
                            SymbolEdge.index_run_id == self.run_id,
                            SymbolEdge.src_symbol_id == node_id,
                            SymbolEdge.kind.in_(("calls", "inherits")),
                        )
                    )
                else:
                    stmt = (
                        select(other.c.id, other.c.qualified_name, File.path, SymbolEdge.line)
                        .select_from(SymbolEdge)
                        .join(other, SymbolEdge.src_symbol_id == other.c.id)
                        .join(File, other.c.file_id == File.id)
                        .where(
                            SymbolEdge.index_run_id == self.run_id,
                            SymbolEdge.dst_symbol_id == node_id,
                            SymbolEdge.kind.in_(("calls", "inherits")),
                        )
                    )
                rows = (await self.session.execute(stmt.limit(MAX_CALL_GRAPH_BREADTH))).all()
                for other_id, other_name, path, line in rows:
                    total += 1
                    lines.append(f"{'  ' * level}{node_name} -> {other_name}  ({path}:{line})")
                    if other_id not in seen:
                        seen.add(other_id)
                        next_frontier.append((other_id, other_name))
            frontier = next_frontier

        if total == 0:
            return ToolOutput(
                text=f"No {direction} recorded for {qualified_name} in this repo's call graph.",
                meta={"count": 0},
            )
        return ToolOutput(text=_truncate("\n".join(lines)), meta={"count": total})

    # -- dispatch -------------------------------------------------------
    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        handler = getattr(self, name, None)
        if handler is None or name not in TOOL_NAMES:
            return ToolOutput(text=f"Unknown tool: {name}", is_error=True)
        try:
            return await handler(**arguments)
        except TypeError as exc:
            return ToolOutput(text=f"Bad arguments for {name}: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - a tool failure is the model's problem to route around
            return ToolOutput(text=f"{name} failed: {type(exc).__name__}: {exc}", is_error=True)


# --------------------------------------------------------------------------
# schemas advertised to the model
# --------------------------------------------------------------------------
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_repo_overview",
        description=(
            "High-level summary of the repository: purpose, languages, top-level packages, "
            "entrypoints and manifests. Cheap. Call it first when you do not know the layout."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="list_directory",
        description="List indexed files under a directory. Use to orient yourself in the tree.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path, '.' for the root."},
                "depth": {"type": "integer", "description": "1-3, default 2."},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read a file, or a line window of one. Output is line-numbered — use those numbers "
            "verbatim in citations. Windows longer than 400 lines are truncated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path."},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="grep",
        description=(
            "Regex search across indexed files. Best for exact identifiers, string literals, "
            "config keys, and decorators. Use semantic_search for conceptual questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path_glob": {"type": "string", "description": "e.g. 'app/**/*.py'"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="semantic_search",
        description=(
            "Vector search over code, docs and config, chunked on symbol boundaries. Use when "
            "you know what something does but not what it is called."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language description."},
                "k": {"type": "integer", "description": "1-25, default 10."},
                "kind": {"type": "string", "enum": ["code", "doc", "config"]},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="find_symbol",
        description=(
            "Look up a function, class, method, interface or type by name. Returns definitions "
            "with signature, docstring and exact location."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    ToolSpec(
        name="find_references",
        description=(
            "Every place a symbol is called or referenced. The tool for impact questions: "
            "'what breaks if I change X?'"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="get_call_graph",
        description=(
            "Traverse the call graph from a symbol. direction='callees' for what it calls, "
            "'callers' for what calls it. Depth capped at 3."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {"type": "string"},
                "direction": {"type": "string", "enum": ["callers", "callees"]},
                "depth": {"type": "integer"},
            },
            "required": ["qualified_name"],
        },
    ),
]

TOOL_NAMES = frozenset(spec.name for spec in TOOL_SPECS)
