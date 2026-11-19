# Phase 3 baseline: outcomes and evals

**Captured:** 2026-11-19
**Baseline:** [`evals/baselines/frozen.json`](../evals/baselines/frozen.json)
**Set:** `evals/frozen/` (30 cases)

## What the baseline measures

The frozen set has 30 synthetic seed cases:

| Skill | Cases |
|---|---|
| `triage-pr-failure` | 8 |
| `find-culprit` | 7 |
| `triage-nightly` | 7 |
| `propose-fix` | 8 |

It contains 14 hard `real_bug` vs `flaky_test` boundaries, 3 injection cases, 13
cases with a second reviewer, and 7 `smoke`-tagged cases. The seeds were
generated from the Phase 2 replay fixtures and are labelled
`labeler: seed-synthetic`; they are **not** a substitute for human review. See
[`docs/labeling-guide.md`](./labeling-guide.md).

## Baseline metrics

| Metric | Value |
|---|---|
| Accuracy | 100% (30/30) |
| False-flaky rate | 0% (0/20 non-flaky cases) |
| Action precision | 100% (27/27 attempted) |
| Median cost | $0.00 |
| Composite score | 1.0000 |
| Hard gates | pass |

Cost is zero because frozen cases replay recorded `llm_script.json` turns. That
is intentional: unit tests, `make check`, and PR CI never need provider
credentials. A live/cached provider run reports the same metrics with non-zero
cost; the baseline cost gate accepts the first live run and then compares later
runs to it with a +10% allowance.

## Commands

```bash
# Full frozen set (writes JSON + Markdown to evals/reports/)
uv run code-triage-agent eval run --set frozen

# Small deterministic subset used by PR CI
uv run code-triage-agent eval run --set frozen --smoke --json

# Recompute hard gates against the committed baseline
uv run code-triage-agent eval gates \
  --report evals/reports/<report>.json \
  --baseline evals/baselines/frozen.json

# Print a saved report
uv run code-triage-agent eval report --report evals/reports/<report>.json
```

## Degradation proof

`tests/integration/test_degraded_skill.py` removes the flaky-test rule from a
temporary copy of `config/skills/triage-pr-failure.md` and replays
`pr-flaky-retry-pass` through a deterministic rule-following policy model. The
intact skill scores 100%; the weakened skill predicts `real_bug`, the accuracy
gate fails, and the report records the regression. Unlike a scripted `FakeLLM`,
the policy reads the system prompt, so the result proves the harness is sensitive
to skill text.

## Operations

- `outcomes collect` labels finished runs from GitHub signals only. It never
  calls an LLM. Attempts are capped and back off; the seven-day recurrence window
  is a hard stop.
- `eval run` refuses to write inside `evals/frozen/`.
- PR CI runs the 5-case smoke subset. The nightly workflow runs the full 30-case
  set. Neither requires secrets.
- Generated reports and the provider cache live under gitignored `evals/reports/`
  and `evals/cache/`. Only the baseline is committed.
