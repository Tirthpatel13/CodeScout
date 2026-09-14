# Eval suite

## Adding cases

Target ~50 cases across 3 repositories you know well. Suggested mix:

| Tag | Share | Question shape |
|---|---|---|
| `locate` | ~40% | "Where is X implemented?" |
| `explain` | ~30% | "How does Y work end to end?" |
| `impact` | ~20% | "What breaks if I change Z?" |
| `absent` | ~10% | Something the repo does **not** do |

```yaml
- id: fastapi-dependency-cache        # unique, stable, kebab-case
  repo: fastapi/fastapi
  commit: 0f7a1b2c...                 # REQUIRED for real repos — pin it
  question: How does FastAPI avoid re-running the same dependency twice?
  expected_paths: [fastapi/dependencies/utils.py]
  expected_symbols: [solve_dependencies]
  rubric: |
    Must mention the per-request dependency cache keyed by
    (call, security_scopes), and that use_cache=False opts out.
  tags: [explain]
```

**Pin every commit.** An unpinned case re-indexes whatever `main` happens to be,
so a refactor upstream shows up as a quality regression in your CI and you will
waste an afternoon before working out why.

### Writing a good rubric

The rubric is what the judge grades against, so it must name the specific thing a
correct answer contains — a mechanism, a function name, a condition. "Should
explain authentication" is not gradeable. "Must state that the HMAC is compared
with `hmac.compare_digest` and that the `exp` claim is checked afterwards" is.

For `absent` cases, say explicitly that inventing a mechanism scores 1. Otherwise
a judge will give partial credit to a fluent fabrication.

## Running

```bash
make eval                                   # everything, with the judge
make eval-baseline                          # freeze current scores as baseline
make eval-smoke                             # 15 cases + regression gate (CI)

python -m app.evals.runner --tag impact     # one slice
python -m app.evals.runner --no-judge       # retrieval metrics only, no judge cost
```

Each distinct `(repo, commit)` is indexed once and shared by every case against
it, so adding cases to an existing repo is nearly free.

## The regression gate

`--fail-on-regression` compares against `evals/baseline.json` and exits non-zero
when:

- `mean_score` drops more than **0.3**, or
- `mean_file_recall` drops more than **5 points**, or
- `mean_citation_validity` drops more than **5 points**

Re-baseline deliberately, in its own commit, with a note saying why. Silently
re-baselining after every regression turns the gate into decoration.

## Calibrating the judge

Before quoting any score as meaningful:

1. Run the suite and export the answers.
2. Hand-score 20 of them yourself, 1-5, without looking at the judge's scores.
3. Feed both dicts to `agreement_rate()`.
4. Put the exact and within-one numbers in the top-level README.

If within-one agreement is below ~80%, fix the rubrics before trusting the mean
score — a judge that disagrees with you is measuring something else.
