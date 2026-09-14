"""Phase 5a: turn files into retrievable chunks.

The rule that matters: never split mid-symbol. A chunk is a whole function, a
whole class (when small enough), a markdown section, or a config file. Fixed-size
windowing is what makes naive RAG return half a function and an unrelated import
block, and it is the single biggest quality difference between this and a
tutorial RAG pipeline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.ingest.parse import ParsedSymbol

# Code is denser than prose per character; ~3.2 chars/token is a decent estimate
# for source and avoids a tokenizer dependency in the hot path.
CHARS_PER_TOKEN_CODE = 3.2
CHARS_PER_TOKEN_PROSE = 4.0

MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(slots=True)
class PreparedChunk:
    kind: str  # code | doc | config
    start_line: int
    end_line: int
    content: str
    content_hash: str
    token_count: int
    symbol_qualified_name: str | None = None
    heading: str | None = None


def estimate_tokens(text: str, kind: str = "code") -> int:
    divisor = CHARS_PER_TOKEN_CODE if kind == "code" else CHARS_PER_TOKEN_PROSE
    return max(1, int(len(text) / divisor))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slice(lines: list[str], start: int, end: int) -> str:
    """1-based inclusive line slice."""
    return "\n".join(lines[start - 1 : end])


def _make(
    lines: list[str],
    start: int,
    end: int,
    kind: str,
    *,
    symbol: str | None = None,
    heading: str | None = None,
) -> PreparedChunk:
    body = _slice(lines, start, end)
    return PreparedChunk(
        kind=kind,
        start_line=start,
        end_line=end,
        content=body,
        content_hash=_hash(f"{kind}:{symbol or heading or ''}:{body}"),
        token_count=estimate_tokens(body, kind),
        symbol_qualified_name=symbol,
        heading=heading,
    )


def _split_oversized(
    lines: list[str],
    start: int,
    end: int,
    kind: str,
    symbol: str | None,
    max_tokens: int,
    overlap: int,
) -> list[PreparedChunk]:
    """Window a too-large symbol, overlapping so a boundary never hides a line."""
    total_lines = end - start + 1
    body = _slice(lines, start, end)
    if estimate_tokens(body, kind) <= max_tokens or total_lines <= overlap + 1:
        return [_make(lines, start, end, kind, symbol=symbol)]

    approx_chars = int(max_tokens * CHARS_PER_TOKEN_CODE)
    avg_line_chars = max(1, len(body) // total_lines)
    window = max(20, approx_chars // avg_line_chars)

    chunks: list[PreparedChunk] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + window - 1)
        chunks.append(_make(lines, cursor, stop, kind, symbol=symbol))
        if stop >= end:
            break
        cursor = stop - overlap + 1
    return chunks


def chunk_code_file(
    text: str,
    symbols: list[ParsedSymbol],
    *,
    max_tokens: int,
    overlap: int,
) -> list[PreparedChunk]:
    lines = text.split("\n")
    n = len(lines)
    if n == 0:
        return []

    # Only leaf symbols become chunks. A class whose methods are indexed
    # separately would otherwise duplicate its entire body; a class with no
    # members is itself a leaf and gets indexed whole.
    names = [s.qualified_name for s in symbols]
    selected = [
        s for s in symbols if not any(other.startswith(s.qualified_name + ".") for other in names)
    ]
    selected.sort(key=lambda s: (s.start_line, s.end_line))

    chunks: list[PreparedChunk] = []
    covered: set[int] = set()
    for sym in selected:
        start = max(1, sym.start_line)
        end = min(n, sym.end_line)
        if start > end:
            continue
        chunks.extend(
            _split_oversized(lines, start, end, "code", sym.qualified_name, max_tokens, overlap)
        )
        covered.update(range(start, end + 1))

    # Module-level code (imports, constants, top-level config) is often exactly
    # what "how is this wired up?" needs, so index the gaps too.
    gap_start: int | None = None
    for line_no in range(1, n + 1):
        blank = not lines[line_no - 1].strip()
        if line_no not in covered and not blank:
            if gap_start is None:
                gap_start = line_no
        elif gap_start is not None:
            chunks.extend(
                _split_oversized(lines, gap_start, line_no - 1, "code", None, max_tokens, overlap)
            )
            gap_start = None
    if gap_start is not None:
        chunks.extend(_split_oversized(lines, gap_start, n, "code", None, max_tokens, overlap))

    return [c for c in chunks if c.content.strip()]


def chunk_markdown(text: str, *, max_tokens: int, overlap: int) -> list[PreparedChunk]:
    lines = text.split("\n")
    n = len(lines)
    sections: list[tuple[int, str]] = [
        (i + 1, m.group(2).strip()) for i, line in enumerate(lines) if (m := MD_HEADING.match(line))
    ]
    if not sections:
        return _split_oversized(lines, 1, n, "doc", None, max_tokens, overlap)

    chunks: list[PreparedChunk] = []
    if sections[0][0] > 1:
        chunks.extend(
            _split_oversized(lines, 1, sections[0][0] - 1, "doc", None, max_tokens, overlap)
        )
    for idx, (line_no, heading) in enumerate(sections):
        end = sections[idx + 1][0] - 1 if idx + 1 < len(sections) else n
        body = _slice(lines, line_no, end)
        if estimate_tokens(body, "doc") <= max_tokens:
            chunks.append(_make(lines, line_no, end, "doc", heading=heading))
        else:
            chunks.extend(
                _split_oversized(lines, line_no, end, "doc", heading, max_tokens, overlap)
            )
    return [c for c in chunks if c.content.strip()]


def chunk_config(text: str, *, max_tokens: int, overlap: int) -> list[PreparedChunk]:
    lines = text.split("\n")
    return [
        c
        for c in _split_oversized(lines, 1, len(lines), "config", None, max_tokens, overlap)
        if c.content.strip()
    ]


def chunk_file(
    text: str,
    kind: str,
    symbols: list[ParsedSymbol],
    *,
    max_tokens: int,
    overlap: int,
) -> list[PreparedChunk]:
    if kind == "code" and symbols:
        return chunk_code_file(text, symbols, max_tokens=max_tokens, overlap=overlap)
    if kind == "doc":
        return chunk_markdown(text, max_tokens=max_tokens, overlap=overlap)
    if kind == "code":
        # Supported extension, unsupported grammar, or a file with no symbols.
        lines = text.split("\n")
        return [
            c
            for c in _split_oversized(lines, 1, len(lines), "code", None, max_tokens, overlap)
            if c.content.strip()
        ]
    return chunk_config(text, max_tokens=max_tokens, overlap=overlap)
