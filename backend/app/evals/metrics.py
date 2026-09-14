"""Eval metrics.

Retrieval quality and answer quality are measured separately on purpose. When an
answer is wrong you need to know whether the agent failed to *find* the code or
failed to *reason* about it, and a single blended score hides that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from app.agent.schemas import AgentAnswer

ABSTENTION_MARKERS = (
    "does not",
    "doesn't",
    "no ",
    "not implemented",
    "not present",
    "could not find",
    "couldn't find",
    "there is no",
    "nothing in",
)


@dataclass(slots=True)
class CaseMetrics:
    case_id: str
    file_recall: float
    symbol_recall: float
    citation_validity: float
    abstained: bool
    tool_calls: int
    cost_usd: float
    latency_ms: int
    score: int | None = None
    judge_reasoning: str | None = None
    answer: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "file_recall": round(self.file_recall, 4),
            "symbol_recall": round(self.symbol_recall, 4),
            "citation_validity": round(self.citation_validity, 4),
            "abstained": self.abstained,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "score": self.score,
        }


@dataclass(slots=True)
class RunSummary:
    cases: int = 0
    mean_score: float = 0.0
    mean_file_recall: float = 0.0
    mean_citation_validity: float = 0.0
    abstention_accuracy: float = 1.0
    mean_tool_calls: float = 0.0
    total_cost_usd: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    per_tag: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "cases": self.cases,
            "mean_score": round(self.mean_score, 3),
            "mean_file_recall": round(self.mean_file_recall, 4),
            "mean_citation_validity": round(self.mean_citation_validity, 4),
            "abstention_accuracy": round(self.abstention_accuracy, 4),
            "mean_tool_calls": round(self.mean_tool_calls, 2),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "per_tag": {k: round(v, 3) for k, v in self.per_tag.items()},
        }


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
    return ordered[idx]


def looks_like_abstention(text: str) -> bool:
    head = text.strip().lower()[:400]
    return any(marker in head for marker in ABSTENTION_MARKERS)


def score_case(
    case_id: str,
    answer: AgentAnswer,
    expected_paths: list[str],
    expected_symbols: list[str],
) -> CaseMetrics:
    cited_paths = {c.path for c in answer.citations}
    file_recall = (
        len([p for p in expected_paths if p in cited_paths]) / len(expected_paths)
        if expected_paths
        else 1.0
    )
    body = answer.text.lower()
    symbol_recall = (
        len([s for s in expected_symbols if s.lower() in body]) / len(expected_symbols)
        if expected_symbols
        else 1.0
    )
    return CaseMetrics(
        case_id=case_id,
        file_recall=file_recall,
        symbol_recall=symbol_recall,
        citation_validity=answer.citation_validity,
        abstained=looks_like_abstention(answer.text),
        tool_calls=len(answer.steps),
        cost_usd=answer.cost_usd,
        latency_ms=answer.latency_ms,
        answer=answer.text,
    )


def summarize(results: list[CaseMetrics], tags_by_case: dict[str, list[str]]) -> RunSummary:
    if not results:
        return RunSummary()

    scored = [r.score for r in results if r.score is not None]
    absent_cases = [r for r in results if "absent" in tags_by_case.get(r.case_id, [])]
    present_cases = [r for r in results if "absent" not in tags_by_case.get(r.case_id, [])]

    # An abstention is correct on an `absent` case and wrong everywhere else.
    correct_abstentions = sum(1 for r in absent_cases if r.abstained)
    correct_answers = sum(1 for r in present_cases if not r.abstained)
    abstention_accuracy = (correct_abstentions + correct_answers) / len(results) if results else 1.0

    per_tag: dict[str, list[int]] = {}
    for r in results:
        if r.score is None:
            continue
        for tag in tags_by_case.get(r.case_id, []):
            per_tag.setdefault(tag, []).append(r.score)

    latencies = [r.latency_ms for r in results]
    return RunSummary(
        cases=len(results),
        mean_score=mean(scored) if scored else 0.0,
        mean_file_recall=mean(r.file_recall for r in results),
        mean_citation_validity=mean(r.citation_validity for r in results),
        abstention_accuracy=abstention_accuracy,
        mean_tool_calls=mean(r.tool_calls for r in results),
        total_cost_usd=sum(r.cost_usd for r in results),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        per_tag={tag: mean(scores) for tag, scores in per_tag.items()},
    )


def check_regression(
    current: RunSummary,
    baseline: dict[str, float] | None,
    *,
    score_tolerance: float = 0.3,
    recall_tolerance: float = 0.05,
) -> list[str]:
    """Returns a list of failures. Empty means the build passes."""
    if not baseline:
        return []
    failures: list[str] = []
    if current.mean_score < baseline.get("mean_score", 0) - score_tolerance:
        failures.append(
            f"mean_score {current.mean_score:.2f} below baseline "
            f"{baseline['mean_score']:.2f} by more than {score_tolerance}"
        )
    if current.mean_file_recall < baseline.get("mean_file_recall", 0) - recall_tolerance:
        failures.append(
            f"mean_file_recall {current.mean_file_recall:.2%} below baseline "
            f"{baseline['mean_file_recall']:.2%} by more than {recall_tolerance:.0%}"
        )
    if current.mean_citation_validity < baseline.get("mean_citation_validity", 0) - 0.05:
        failures.append(f"citation_validity {current.mean_citation_validity:.2%} regressed")
    return failures
