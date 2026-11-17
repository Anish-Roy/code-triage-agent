# Phase 3 Handoff — Outcomes and Evals

**Created:** 2026-11-17
**Repository:** `code-triage-agent`
**Branch:** `main`
**Commit:** `45930b1` (`Implement Phase 2 runner with replay triage and comments`)
**Remote:** `origin/main` is in sync
**Working tree:** clean
**Test status:** 183 passing; `make check` green (ruff, format, mypy, pytest)

This document is the starting point for Phase 3. Read it with
[`IMPLEMENTATION.md`](./IMPLEMENTATION.md) section **Phase 3** (steps 25–31)
and [`plan.md`](./plan.md).

---

## 1. Phase 3 objective

> **Exit criteria:** a replay score exists for every skill, the outcome
> collector labels finished runs, and a deliberately degraded skill is caught
> by the eval harness.

Phase 3 turns the Phase 2 runner into something measurable:

1. Collect outcomes from GitHub and label finished runs.
2. Store hand-labeled eval cases in a frozen set.
3. Replay cases through the real runner and emit reproducible reports.
4. Compute accuracy, false-flaky rate, action precision, cost, and a composite
   score.
5. Prove the harness catches a deliberately broken skill.

---

## 2. Current state after Phase 2

### Implemented

- **Phase 0:** settings, config validation, SQLite schema/migrations, health
  endpoint, webhook signature verification, CI.
- **Phase 1:** webhook events → GitHub logs/diff → trim/redact → signature →
  snapshot + run/steps in SQLite.
- **Phase 2:** routing, prompt assembly, tool registry, bounded agent loop,
  `submit_triage` validation, run records, idempotent PR comments, replay CLI,
  injection tests.

### Phase 2 runner files

| File | Responsibility |
|---|---|
| `src/code_triage_agent/runner/llm.py` | `LLMClient` protocol, `FakeLLM`, pricing, timeout, transient retry |
| `src/code_triage_agent/runner/prompt.py` | skill loader (`sha256(file)` version), system/user prompt, untrusted blocks |
| `src/code_triage_agent/runner/tools.py` | per-skill tool allowlist, path/size checks, replay results |
| `src/code_triage_agent/runner/loop.py` | budgets, tool dispatch, `submit_triage`, evidence validation, safe `unknown` |
| `src/code_triage_agent/runner/router.py` | ordered trigger matching, Phase 2 comment-only action intersection |
| `src/code_triage_agent/runner/actions.py` | comment rendering, run-ID marker, idempotent posting |
| `src/code_triage_agent/runner/record.py` | run/step persistence and CLI artifact |
| `src/code_triage_agent/runner/pipeline.py` | `triage_snapshot(...)` end-to-end pipeline |

### Useful commands

```bash
git switch main
git pull --ff-only
make check

# Run one fixture through the real Phase 2 pipeline
uv run code-triage-agent triage --snapshot tests/fixtures/snapshots/pr-failure

# Force a skill for replay/eval work
uv run code-triage-agent triage --snapshot tests/fixtures/snapshots/pr-failure \
  --skill propose-fix
```

### Existing fixtures worth reusing

- `tests/fixtures/llm/replay_cases.json` — 10 synthetic cases across all 4
  skills (`triage-pr-failure`, `find-culprit`, `triage-nightly`,
  `propose-fix`).
- `tests/fixtures/snapshots/pr-failure/` — a complete collector-format snapshot
  plus `llm_script.json`.
- `tests/fixtures/golden/triage_pr_failure.json` — golden Phase 2 run artifact.
- `tests/fixtures/prompts/triage-pr-failure.txt` — golden rendered prompt.
- `tests/fixtures/logs/malicious_job.log` — prompt-injection fixture.
- `tests/integration/test_replay_cases.py` — 10-case replay + idempotent comment
  pattern.
- `tests/integration/test_injection.py` — injection behavior to preserve.

### Relevant contracts

- **Snapshot format:** `metadata.json`, `logs.txt`, `diff.patch`, `memory.json`,
  `signature.txt`; dumped from `SnapshotMetadata` / `LoadedSnapshot`.
- **Run record columns:** `runs` has `category`, `confidence`, `summary`,
  `actions_json`, `cost_usd`, `status`, `config_sha`, `skill_version`.
- **Decision location:** the accepted decision lives in the `steps` row whose
  `tool == "submit_triage"` under `args_json["decision"]`. Phase 3 should make
  this queryable instead of parsing step JSON in every report.
- **Outcome tables already exist** in `schema.sql`: `outcomes` and `evals`.
  There are **no repository functions or models for them yet**.
- **No `background/` package exists yet.**
- **No scheduler exists yet.** Phase 3 should expose deterministic functions and
  CLI commands; scheduling can remain a thin wrapper.
- **No live LLM provider adapter exists.** `FakeLLM` replays JSON scripts only.

---

## 3. Blocking decisions before coding

### 3.1 LLM provider for evals (highest priority)

Phase 2 deliberately has no live provider. `FakeLLM` returns fixed scripted
turns, so changing a skill prompt does **not** change the model output. That
means a scripted replay cannot satisfy Phase 3 step 31 (“deliberately break a
skill and prove the harness fails”).

Decide one of:

1. **Recommended:** implement one provider adapter behind `LLMClient` (pin the
   model version), add `CTA_LLM_API_KEY` plumbing, and use it for eval runs.
   Update `docs/adr/0001-v1-stack.md` with the provider decision.
2. Build a record/replay cache keyed by prompt + config SHA. A changed skill
   invalidates the cache and forces a live call. This still needs a provider
   for cache misses.
3. Limit Phase 3 to deterministic outcome/label/report plumbing and defer
   prompt-sensitive evals. This would **not** meet the stated exit criteria.

Also decide the eval cost ceiling. A full frozen run of 30 cases should have a
known worst-case cost using the existing `max_cost_usd` and pricing settings.

### 3.2 Schema gaps

`outcomes` and `evals` tables exist, but Phase 3 needs:

- `runs.outcome` (the implementation text says “runs with `outcome IS NULL`”)
  or an explicit join to `outcomes`. The current `runs` table has no `outcome`
  column.
- The predicted action is not a column. It exists only inside the
  `submit_triage` step JSON.
- `evals` lacks `labeler` and timestamps required by the `case.json` format.
- Outcome polling needs bookkeeping: attempt count, `next_check_at`, last
  checked time, and terminal state.

Recommended: add `src/code_triage_agent/migrations/0002_phase3_outcomes_evals.sql`
with additive columns (never edit `schema.sql` after release; add a migration).
Then add models and repository functions in `db.py` / `models.py`.

### 3.3 Hand-labeled cases are a human task

The 10 existing replay cases are synthetic and can seed the set, but Phase 3
requires **30 hand-labeled cases**:

- at least 6 per skill,
- at least 10 deliberately hard `real_bug` vs `flaky_test` boundaries,
- 3 injection cases,
- a second reviewer for at least 10.

Do not let the eval harness write to `evals/frozen/`. Add a guard test for
that. Keep the labeling guide in `docs/` (for example
`docs/labeling-guide.md`).

### 3.4 Scheduler

There is no scheduler process. Start with:

```bash
uv run code-triage-agent outcomes collect --limit 50
uv run code-triage-agent eval run --set frozen
```

A later phase can call the same functions from APScheduler. Keep functions
clock-injectable and idempotent.

---

## 4. Work breakdown for Phase 3

### Step 25 — Outcome collector

Create `src/code_triage_agent/background/outcomes.py`.

Inputs: DB, GitHub client, clock, limits.
Outputs: `outcomes` rows / updates to `runs.outcome`.

Collect signals:

- **Rerun:** did a rerun for this workflow run finish? conclusion?
- **Fix PR:** merged, closed without merge, reverted (if detectable).
- **Recurrence:** same signature fails again within 7 days.
- **Human feedback:** optional comment/reaction signal; never overrides frozen
  labels.

Rules:

- Never call an LLM.
- Only poll runs that are finished and unresolved.
- Cap attempts and set `next_check_at` with backoff; stop after the 7-day
  recurrence window.
- Respect GitHub rate limits and use the existing client retry behavior.
- Store raw signals and evidence JSON for audit.
- Do not poll forever when no action was recommended.

Suggested data shape:

```python
@dataclass(frozen=True)
class OutcomeSignals:
    rerun_conclusion: str | None
    fix_pr_state: str | None
    fix_pr_merged: bool | None
    signature_recurred: bool
    human_feedback: str | None
    checked_at: datetime
```

### Step 26 — Label decision table

Create `src/code_triage_agent/background/score.py`.

Labels: `correct`, `incorrect`, `unknown`.

Precedence exactly:

1. explicit human feedback,
2. fix PR outcome,
3. rerun result,
4. recurrence.

Map at minimum:

| Prediction / signal | Label |
|---|---|
| `flaky_test` + rerun passed | `correct` |
| `flaky_test` + rerun failed with same signature | `incorrect` |
| fix PR merged | `correct` |
| fix PR closed or reverted | `incorrect` |
| same signature recurs within 7 days after claimed fix | `incorrect` unless human override |
| human feedback | authoritative for the label table |
| no usable signal | `unknown` |

Unit-test every row and every ambiguous combination. Keep `signals_json` and
`evidence_json` with each label.

### Step 27 — Eval case format and importer

Define a `case.json` schema:

```json
{
  "case_id": "pr-flaky-001",
  "snapshot_path": "evals/frozen/pr-flaky-001/snapshot",
  "expected_category": "flaky_test",
  "expected_action": "comment",
  "labeler": "name",
  "notes": "why this label is correct"
}
```

Directory shape:

```
evals/frozen/<case_id>/
  case.json
  snapshot/
  llm_script.json      # only if the harness uses recorded turns
```

Add:

```bash
uv run code-triage-agent eval import --run-id r_...
```

Importer behavior:

- copy the run snapshot into a new case directory,
- copy the decision/artifact into `case.json` as a starting label,
- never overwrite an existing case,
- never write outside the target set directory,
- reject path traversal in `case_id`.

Add repository functions if the `evals` table is used as an index; keep
`case.json` authoritative if there is a conflict.

### Step 28 — Hand-label 30 cases

- Freeze under `evals/frozen/`.
- Minimum 6 per skill and 10 hard boundary cases.
- Include the 3 injection cases.
- Record labeler and reviewer in `case.json` or a sidecar.
- Write `docs/labeling-guide.md` with category definitions, boundary rules,
  and examples.
- Verify frozen files are read-only to the harness (test it).

### Step 29 — Replay harness

Create `src/code_triage_agent/background/replay.py`.

Behavior:

- Load a case and its snapshot.
- Force `replay` mode and temperature `0`.
- Load the config at the specified config SHA/directory.
- Run `triage_snapshot(..., persist=False)`.
- Compare predicted category/action with the case.
- Emit JSON and Markdown reports to a writable reports directory.
- Support `--case`, `--set`, and a small `--smoke` subset.
- Never write to `evals/frozen/`.
- Record config SHA, skill versions, model ID, cost, and runtime.

Report schema proposal:

```json
{
  "run_at": "...",
  "config_sha": "...",
  "set_name": "frozen",
  "model": "...",
  "cases": [
    {
      "case_id": "...",
      "expected_category": "...",
      "predicted_category": "...",
      "expected_action": "...",
      "predicted_action": "...",
      "label": "correct",
      "cost_usd": 0.01,
      "failure_reason": null
    }
  ],
  "metrics": {}
}
```

Markdown report should include the confusion matrix and every mismatch with
evidence.

### Step 30 — Metrics and score

Create `src/code_triage_agent/background/metrics.py` (or keep in
`replay.py` if small).

Compute:

- accuracy overall and per skill/category,
- confusion matrix (mandatory),
- `false_flaky_rate = false-flaky predictions / all non-flaky labeled cases`,
- `action_precision = successful actions / actions attempted`,
- median cost,
- failure reasons.

Composite:

```text
0.45 * accuracy
+ 0.25 * (1 - false_flaky_rate)
+ 0.20 * action_precision
+ 0.10 * cost_efficiency
```

`cost_efficiency = min(1, budget / actual_cost)`.

Hard gates are separate and may not be averaged away:

- no new false-flaky on frozen cases,
- no accuracy regression,
- cost within +10%,
- config guard passes.

### Step 31 — Baseline, CI, and degradation proof

- Freeze a baseline report for v1.
- Add a 5-case smoke subset for PR CI.
- Add a full frozen nightly run.
- Store reports under a writable path such as `evals/reports/` (do not commit
  generated reports unless intentionally baseline).
- Deliberately break a skill locally (for example, weaken the flaky rule) and
  prove the harness fails. Keep this as a test or documented drill.
- CI secrets must never be required for unit tests or `make check`.

---

## 5. Suggested file plan

```text
src/code_triage_agent/
  background/
    __init__.py
    outcomes.py       # collect + persist signals
    score.py          # signal -> label decision table
    replay.py         # case runner + JSON/Markdown reports
    metrics.py        # metrics, confusion matrix, composite score
  evals/              # optional: case parsing, importer, guard
    __init__.py
    cases.py
    import_run.py
  migrations/
    0002_phase3_outcomes_evals.sql
  models.py           # OutcomeRecord, EvalCase, ReplayReport, ...
  db.py               # outcome/eval repository functions
  cli.py              # `outcomes collect`, `eval import`, `eval run`, `eval report`

tests/
  unit/test_outcome_signals.py
  unit/test_label_table.py
  unit/test_eval_cases.py
  unit/test_replay_metrics.py
  integration/test_outcomes_collect.py
  integration/test_replay_harness.py
  integration/test_frozen_guard.py

evals/
  frozen/<case_id>/{case.json,snapshot/,llm_script.json}
  reports/            # generated, normally gitignored
docs/
  labeling-guide.md
  phase-3-report.md   # baseline summary
```

---

## 6. Testing strategy

- Use `FakeGitHub` clients and a fixed clock for outcome tests.
- Unit-test the label table exhaustively; this is the highest-risk logic.
- Run replay tests against at least one frozen case per skill.
- Assert the harness cannot write to `evals/frozen/` (path traversal and direct
  writes).
- Assert no LLM is imported by the outcome collector.
- Keep the Phase 2 injection suite green.
- Degradation test: alter a skill rule in a temporary config copy, run a case
  whose label depends on that rule, and assert the report/score fails.

---

## 7. Commands and environment

```bash
git switch main
git pull --ff-only
git switch -c phase-3-outcomes-evals

make check
uv run code-triage-agent validate-config
uv run code-triage-agent triage --snapshot tests/fixtures/snapshots/pr-failure
```

Provider credentials (once chosen):

```bash
CTA_LLM_PROVIDER=...
CTA_LLM_MODEL=...
CTA_LLM_API_KEY=...
CTA_LLM_TIMEOUT_SECONDS=60
CTA_LLM_INPUT_COST_PER_MTOK=...
CTA_LLM_OUTPUT_COST_PER_MTOK=...
```

Never commit `.env` or real credentials. Do not modify `.gitignore` casually.

---

## 8. Pitfalls to avoid

- **Do not treat scripted `FakeLLM` output as an eval of prompt quality.**
  Resolve the provider/cache decision first.
- **Do not let the replay harness write into `evals/frozen/`.**
- **Do not re-poll forever.** Cap attempts and stop after the recurrence window.
- **Do not call the LLM from the outcome collector or label table.**
- **Do not trust step JSON forever.** Persist the decision/action as columns.
- **Respect the seven-day recurrence rule** when labeling.
- **Keep SQLite transactions short**; use WAL and existing helpers.
- **Pin config SHA and model ID** in every report.
- **Do not break the Phase 2 action allowlist.** Only `comment_on_pr` executes.
- **Do not require provider secrets for `make check`.**

---

## 9. Definition of done

- [ ] Outcome collector labels finished runs automatically and idempotently.
- [ ] Label table covers all rows and ambiguous precedence.
- [ ] 30 frozen cases exist, including 6 per skill, 10 boundary cases, 3
      injection cases, and a second review for at least 10.
- [ ] `eval import --run-id` creates a case without overwriting.
- [ ] `eval run --set frozen` produces JSON + Markdown reports.
- [ ] Metrics include accuracy, confusion matrix, false-flaky rate, action
      precision, median cost, and composite score.
- [ ] Hard gates are evaluated separately.
- [ ] Baseline report is recorded.
- [ ] PR smoke subset and nightly full run are wired.
- [ ] A deliberately degraded skill is caught by the harness.
- [ ] All Phase 2 tests still pass; `make check` is green.

---

## 10. Suggested kickoff prompt for the new session

```text
Read HANDOFF_PHASE_3.md, IMPLEMENTATION.md Phase 3 (steps 25–31), and plan.md.
We are starting Phase 3 on a new branch from main (45930b1).

First:
1. Confirm the LLM provider decision needed for prompt-sensitive evals.
2. Propose the migration for outcome/eval columns and the queryable decision.
3. Implement steps 25–26 (outcome collector + label table) with tests.

Do not write to evals/frozen/ from the harness. Keep make check green.
```

---

## 11. Open questions for the user

1. Which LLM provider/model should Phase 3 use for eval runs, and what is the
   per-run or per-eval cost ceiling?
2. Who will provide the second review for the 30 frozen cases?
3. Should generated eval reports be committed as baselines or kept under a
   gitignored `evals/reports/` directory?
4. Is a 5-case PR smoke run allowed to call the live model, or should PR CI use
   only deterministic/cached cases?
