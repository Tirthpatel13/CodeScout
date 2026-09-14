"""LLM-as-judge.

A judge is a measurement instrument, and an uncalibrated instrument is worse
than no instrument because it produces confident numbers. Two rules follow:

1. Judge with a different model than the one that answered, so the judge is not
   grading its own reasoning style.
2. Hand-label a subset yourself and record the agreement rate. `make judge-agreement`
   prints it; put the number in the README.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.providers.base import LLMProvider

JUDGE_SYSTEM = """You grade answers about a codebase against a rubric. You are \
strict, and you never reward fluent writing that does not satisfy the rubric.

Score 1-5:
  5 - fully satisfies the rubric, correct, and cites the right code.
  4 - correct and substantially complete, minor omission.
  3 - partially correct, or correct but missing a required element of the rubric.
  2 - mostly wrong, or right conclusion with wrong reasoning.
  1 - wrong, or fabricates code that does not exist.

An answer that correctly says the repository does NOT do something, when the \
rubric says so, scores 5. An answer that invents a plausible-sounding mechanism \
scores 1, however well written.

Reply with JSON only: {"score": <1-5>, "reasoning": "<two sentences>"}"""

SCORE_RE = re.compile(r'"score"\s*:\s*([1-5])')


@dataclass(slots=True)
class Judgement:
    score: int
    reasoning: str


def _parse(text: str) -> Judgement:
    stripped = text.strip()
    if stripped.startswith("```"):
        parts = stripped.split("```")
        stripped = parts[1][4:] if len(parts) > 1 and parts[1].startswith("json") else parts[1]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end != -1:
        try:
            data = json.loads(stripped[start : end + 1])
            return Judgement(
                score=int(data["score"]), reasoning=str(data.get("reasoning", ""))[:1000]
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
    match = SCORE_RE.search(text)
    if match:
        return Judgement(score=int(match.group(1)), reasoning=text[:500])
    raise ValueError(f"could not parse judge output: {text[:200]}")


async def judge_answer(
    llm: LLMProvider,
    *,
    question: str,
    rubric: str,
    answer: str,
    max_retries: int = 2,
) -> Judgement:
    prompt = f"QUESTION\n{question}\n\nRUBRIC\n{rubric}\n\nANSWER UNDER TEST\n{answer}\n"
    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            response = await llm.complete(
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            )
            return _parse(response.text)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"judge failed after retries: {last_error}")


def agreement_rate(model_scores: dict[str, int], human_scores: dict[str, int]) -> dict[str, float]:
    """Exact and within-one agreement between the judge and hand labels."""
    shared = set(model_scores) & set(human_scores)
    if not shared:
        return {"n": 0, "exact": 0.0, "within_one": 0.0}
    exact = sum(1 for k in shared if model_scores[k] == human_scores[k])
    close = sum(1 for k in shared if abs(model_scores[k] - human_scores[k]) <= 1)
    return {
        "n": len(shared),
        "exact": exact / len(shared),
        "within_one": close / len(shared),
    }
