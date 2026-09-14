"""Tests for the eval harness itself.

The harness is measurement code: if it is wrong, every number it produces is
wrong and you will trust them anyway. So it gets tests too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.schemas import AgentAnswer, ParsedCitation, StepRecord
from app.evals.judge import agreement_rate, judge_answer
from app.evals.metrics import (
    RunSummary,
    check_regression,
    looks_like_abstention,
    score_case,
    summarize,
)
from app.evals.runner import load_cases
from app.providers.fake import ScriptedLLM

CASES_PATH = Path(__file__).parent.parent / "evals" / "cases.yaml"


def _answer(text: str, paths: list[str], dropped: int = 0) -> AgentAnswer:
    return AgentAnswer(
        text=text,
        citations=[ParsedCitation(p, 1, 10) for p in paths],
        dropped_citations=[ParsedCitation("bogus.py", 1, 2)] * dropped,
        steps=[StepRecord(0, "grep", {}, "", 5)],
        cost_usd=0.01,
        latency_ms=1200,
    )


# --------------------------------------------------------------------------
# case loading
# --------------------------------------------------------------------------
def test_cases_file_parses_and_is_well_formed():
    cases = load_cases(CASES_PATH)
    assert len(cases) >= 5
    for case in cases:
        assert case.id and case.question
        assert case.repo or case.local_path, f"{case.id} has no target"
        assert case.tags, f"{case.id} has no tags"


def test_cases_include_absent_cases():
    """Absent cases are the hallucination canary — never let them disappear."""
    cases = load_cases(CASES_PATH)
    absent = [c for c in cases if "absent" in c.tags]
    assert absent, "the suite must contain cases the repository cannot answer"
    for case in absent:
        assert case.expected_paths == []


def test_case_ids_are_unique():
    cases = load_cases(CASES_PATH)
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


def test_real_repo_cases_pin_a_commit():
    for case in load_cases(CASES_PATH):
        if case.repo:
            assert case.commit, f"{case.id} names a repo but pins no commit"


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
def test_file_recall_counts_only_expected_paths():
    metrics = score_case(
        "c1", _answer("x", ["a.py", "b.py"]), expected_paths=["a.py", "c.py"], expected_symbols=[]
    )
    assert metrics.file_recall == 0.5


def test_symbol_recall_matches_case_insensitively():
    metrics = score_case("c1", _answer("Calls Verify_Token here", []), [], ["verify_token"])
    assert metrics.symbol_recall == 1.0


def test_citation_validity_reflects_dropped_citations():
    metrics = score_case("c1", _answer("x", ["a.py"], dropped=1), [], [])
    assert metrics.citation_validity == 0.5


def test_empty_expectations_score_full_recall():
    metrics = score_case("c1", _answer("x", []), [], [])
    assert metrics.file_recall == 1.0 and metrics.symbol_recall == 1.0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("This repository does not implement rate limiting.", True),
        ("There is no database connection.", True),
        ("I could not find any caching layer.", True),
        ("Rate limiting is handled by a Redis fixed-window counter.", False),
    ],
)
def test_abstention_detection(text, expected):
    assert looks_like_abstention(text) is expected


def test_abstention_accuracy_rewards_the_right_behaviour():
    good = score_case("absent-1", _answer("There is no database.", []), [], [])
    bad = score_case("present-1", _answer("There is no auth either.", []), [], [])
    summary = summarize([good, bad], {"absent-1": ["absent"], "present-1": ["locate"]})
    # Correct abstention on the absent case, wrong abstention on the present one.
    assert summary.abstention_accuracy == 0.5


def test_summary_computes_percentiles_and_per_tag_scores():
    results = []
    for i in range(10):
        m = score_case(f"c{i}", _answer("x", ["a.py"]), ["a.py"], [])
        m.latency_ms = (i + 1) * 100
        m.score = 5 if i % 2 == 0 else 3
        results.append(m)
    summary = summarize(results, {f"c{i}": ["locate"] for i in range(10)})

    assert summary.cases == 10
    assert summary.mean_score == 4.0
    assert summary.p50_latency_ms <= summary.p95_latency_ms
    assert summary.per_tag["locate"] == 4.0


# --------------------------------------------------------------------------
# the regression gate
# --------------------------------------------------------------------------
def test_no_baseline_means_no_failure():
    assert check_regression(RunSummary(mean_score=1.0), None) == []


def test_small_score_drop_is_tolerated():
    current = RunSummary(mean_score=4.1, mean_file_recall=0.9, mean_citation_validity=0.95)
    baseline = {"mean_score": 4.3, "mean_file_recall": 0.9, "mean_citation_validity": 0.95}
    assert check_regression(current, baseline) == []


def test_large_score_drop_fails_the_build():
    current = RunSummary(mean_score=3.5, mean_file_recall=0.9, mean_citation_validity=0.95)
    baseline = {"mean_score": 4.3, "mean_file_recall": 0.9, "mean_citation_validity": 0.95}
    failures = check_regression(current, baseline)
    assert failures and "mean_score" in failures[0]


def test_recall_drop_fails_the_build():
    current = RunSummary(mean_score=4.3, mean_file_recall=0.70, mean_citation_validity=0.95)
    baseline = {"mean_score": 4.3, "mean_file_recall": 0.90, "mean_citation_validity": 0.95}
    failures = check_regression(current, baseline)
    assert any("file_recall" in f for f in failures)


# --------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------
async def test_judge_parses_clean_json():
    llm = ScriptedLLM(['{"score": 4, "reasoning": "Correct but terse."}'])
    result = await judge_answer(llm, question="q", rubric="r", answer="a")
    assert result.score == 4
    assert "terse" in result.reasoning


async def test_judge_parses_fenced_json():
    llm = ScriptedLLM(['```json\n{"score": 2, "reasoning": "Wrong file."}\n```'])
    result = await judge_answer(llm, question="q", rubric="r", answer="a")
    assert result.score == 2


async def test_judge_retries_then_raises_on_garbage():
    llm = ScriptedLLM(["not json", "still not json", "nope"])
    with pytest.raises(RuntimeError, match="judge failed"):
        await judge_answer(llm, question="q", rubric="r", answer="a")


def test_agreement_rate_against_hand_labels():
    model = {"a": 5, "b": 3, "c": 2}
    human = {"a": 5, "b": 4, "c": 5}
    rates = agreement_rate(model, human)
    assert rates["n"] == 3
    assert rates["exact"] == pytest.approx(1 / 3)
    assert rates["within_one"] == pytest.approx(2 / 3)


def test_agreement_rate_with_no_overlap():
    assert agreement_rate({"a": 5}, {"b": 5})["n"] == 0


# --------------------------------------------------------------------------
# end to end, offline
# --------------------------------------------------------------------------
async def test_eval_run_end_to_end_with_fixture_repo(_schema, monkeypatch):
    """The whole harness, on the fixture repo, with scripted models."""
    from app.evals import runner

    monkeypatch.chdir(Path(__file__).parent.parent)

    answers = ScriptedLLM(
        [
            [("find_symbol", {"name": "verify_token"})],
            "Verified in [app/auth/tokens.py:31-48] and called from "
            "[app/auth/middleware.py:24-45].\n\nCONFIDENCE: high",
        ]
    )
    judges = ScriptedLLM(['{"score": 5, "reasoning": "Correct and cited."}'])
    monkeypatch.setattr(
        runner,
        "get_llm",
        lambda settings=None, model=None: judges if model == settings.overview_model else answers,
    )

    cases = [c for c in load_cases(CASES_PATH) if c.id == "fixture-verify-token-location"]
    outcome = await runner.run_evals(cases, judge=True, persist=False)

    summary = outcome["summary"]
    assert summary.cases == 1
    assert summary.mean_score == 5.0
    assert summary.mean_file_recall == 1.0
    assert summary.mean_citation_validity == 1.0
