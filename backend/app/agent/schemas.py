from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# [app/auth/tokens.py:28-46] or [app/auth/tokens.py:28]
CITATION_RE = re.compile(r"\[([\w./\-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?\]")
CONFIDENCE_RE = re.compile(r"^\s*CONFIDENCE:\s*(high|medium|low)\s*$", re.IGNORECASE | re.MULTILINE)

Confidence = Literal["high", "medium", "low"]


@dataclass(slots=True, frozen=True)
class ParsedCitation:
    path: str
    start_line: int
    end_line: int

    def key(self) -> tuple[str, int, int]:
        return (self.path, self.start_line, self.end_line)


@dataclass(slots=True)
class StepRecord:
    idx: int
    tool_name: str
    tool_input: dict[str, Any]
    result_summary: str
    duration_ms: int
    is_error: bool = False


@dataclass(slots=True)
class AgentAnswer:
    text: str
    citations: list[ParsedCitation] = field(default_factory=list)
    dropped_citations: list[ParsedCitation] = field(default_factory=list)
    confidence: Confidence = "medium"
    steps: list[StepRecord] = field(default_factory=list)
    budget_exhausted: bool = False
    cost_usd: float = 0.0
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def citation_validity(self) -> float:
        total = len(self.citations) + len(self.dropped_citations)
        return len(self.citations) / total if total else 1.0


def extract_citations(text: str) -> list[ParsedCitation]:
    out: list[ParsedCitation] = []
    seen: set[tuple[str, int, int]] = set()
    for match in CITATION_RE.finditer(text):
        path, start_raw, end_raw = match.group(1), match.group(2), match.group(3)
        start = int(start_raw)
        end = int(end_raw) if end_raw else start
        if end < start:
            start, end = end, start
        citation = ParsedCitation(path=path, start_line=start, end_line=end)
        if citation.key() not in seen:
            seen.add(citation.key())
            out.append(citation)
    return out


def extract_confidence(text: str) -> tuple[str, Confidence]:
    """Pull the trailing CONFIDENCE line off and return (body, confidence)."""
    match = None
    for match in CONFIDENCE_RE.finditer(text):  # noqa: B007 - want the last one
        pass
    if match is None:
        return text.strip(), "medium"
    body = (text[: match.start()] + text[match.end() :]).strip()
    return body, match.group(1).lower()  # type: ignore[return-value]
