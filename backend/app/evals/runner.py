"""Eval runner.

    python -m app.evals.runner --cases evals/cases.yaml --tag locate
    python -m app.evals.runner --smoke --fail-on-regression

Indexes each distinct (repo, commit) once, runs every case against it, judges
the answers, writes a run row, and prints a summary. `--fail-on-regression`
exits non-zero when quality drops against the stored baseline, which is what
makes this a CI gate rather than a dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.agent.loop import answer_question
from app.agent.tools import ToolContext
from app.config import get_settings
from app.db import session_scope
from app.evals.judge import judge_answer
from app.evals.metrics import CaseMetrics, check_regression, score_case, summarize
from app.ingest.pipeline import run_ingest
from app.models import EvalRun, IndexRun, Repository, RunStatus
from app.providers import get_embedder, get_llm

BASELINE_PATH = Path("evals/baseline.json")


@dataclass(slots=True)
class Case:
    id: str
    question: str
    repo: str | None = None
    commit: str | None = None
    local_path: str | None = None
    expected_paths: list[str] = None  # type: ignore[assignment]
    expected_symbols: list[str] = None  # type: ignore[assignment]
    rubric: str = ""
    tags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.expected_paths = self.expected_paths or []
        self.expected_symbols = self.expected_symbols or []
        self.tags = self.tags or []

    @property
    def target(self) -> str:
        return self.local_path or f"{self.repo}@{self.commit}"


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Case(**entry) for entry in raw]


def git_sha() -> str | None:
    try:
        # Fixed argv, no shell, no user input: S603/S607 do not apply here.
        return subprocess.run(  # noqa: S603, S607
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


async def _index_target(session, settings, case: Case) -> IndexRun:
    """Index one (repo, commit) target and return its run."""
    owner, name = (case.repo or "local/fixture").split("/", 1)
    repo = Repository(
        id=uuid.uuid4(),
        github_id=abs(hash(case.target)) % 10_000_000_000,
        owner=owner,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        default_branch="main",
    )
    session.add(repo)
    await session.flush()

    run = IndexRun(
        id=uuid.uuid4(),
        repository_id=repo.id,
        commit_sha=(case.commit or "0" * 40)[:40],
        status=RunStatus.QUEUED.value,
    )
    session.add(run)
    await session.flush()

    await run_ingest(
        session,
        run_id=run.id,
        settings=settings,
        embedder=get_embedder(settings),
        llm=None,
        local_path=Path(case.local_path) if case.local_path else None,
    )
    await session.flush()
    return run


async def run_evals(
    cases: list[Case],
    *,
    judge: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    answer_llm = get_llm(settings, model=settings.answer_model)
    # Deliberately a different model from the one under test.
    judge_llm = get_llm(settings, model=settings.overview_model)

    results: list[CaseMetrics] = []
    tags_by_case = {c.id: c.tags for c in cases}

    async with session_scope() as session:
        by_target: dict[str, list[Case]] = {}
        for case in cases:
            by_target.setdefault(case.target, []).append(case)

        for target, target_cases in by_target.items():
            print(f"\n=== indexing {target} ===")
            run = await _index_target(session, settings, target_cases[0])
            ctx = ToolContext(session, run, get_embedder(settings), settings)

            for case in target_cases:
                answer = await answer_question(ctx, case.question, answer_llm, settings=settings)
                metrics = score_case(case.id, answer, case.expected_paths, case.expected_symbols)
                if judge and case.rubric:
                    judgement = await judge_answer(
                        judge_llm,
                        question=case.question,
                        rubric=case.rubric,
                        answer=answer.text,
                    )
                    metrics.score = judgement.score
                    metrics.judge_reasoning = judgement.reasoning
                results.append(metrics)
                print(
                    f"  [{case.id}] score={metrics.score} "
                    f"recall={metrics.file_recall:.0%} "
                    f"cites={metrics.citation_validity:.0%} "
                    f"tools={metrics.tool_calls} {metrics.latency_ms}ms"
                )

        summary = summarize(results, tags_by_case)

        if persist:
            session.add(
                EvalRun(
                    id=uuid.uuid4(),
                    git_sha=git_sha(),
                    model=answer_llm.model,
                    prompt_version=settings.prompt_version,
                    summary=summary.as_dict(),
                )
            )

    return {"summary": summary, "results": results}


def _print_summary(summary) -> None:
    print("\n" + "=" * 60)
    for key, value in summary.as_dict().items():
        print(f"  {key:<24} {value}")
    print("=" * 60)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CodeScout eval suite.")
    parser.add_argument("--cases", default="evals/cases.yaml")
    parser.add_argument("--tag", action="append", help="Only cases carrying this tag.")
    parser.add_argument("--smoke", action="store_true", help="First 15 cases only (CI).")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if args.tag:
        wanted = set(args.tag)
        cases = [c for c in cases if wanted & set(c.tags)]
    if args.smoke:
        cases = cases[:15]
    if not cases:
        print("no cases matched")
        return 1

    outcome = await run_evals(cases, judge=not args.no_judge)
    summary = outcome["summary"]
    _print_summary(summary)

    if args.write_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(summary.as_dict(), indent=2))
        print(f"\nbaseline written to {BASELINE_PATH}")
        return 0

    if args.fail_on_regression:
        baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.is_file() else None
        failures = check_regression(summary, baseline)
        if failures:
            print("\nREGRESSION:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print("\nno regression against baseline")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
