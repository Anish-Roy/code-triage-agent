# Code Triage Agent — Implementation Plan

This is the execution plan for the design in [`plan.md`](./plan.md).

It is ordered by dependency, not by ambition. Each phase produces something runnable and testable. Do not start a phase before the previous phase's exit criteria pass.

---

## 0. V1 scope and assumptions

**Build this first:**

- One GitHub App installed on one repository (plus a test repo).
- Python 3.13 + `uv` (already configured).
- FastAPI webhook receiver, one background worker, SQLite for state.
- One LLM provider, model name configurable behind an interface.
- Actions: PR comment first, then rerun, issue, and fix PR.
- Config lives in this repo under `config/` for v1. The background agent can only touch allowlisted paths and only through a PR.
- Deployment: one container/VM. No queue service, no Postgres, no dashboard service.

**Explicit non-goals for v1:**

- Multi-tenant, multi-org scale.
- Sub-minute latency.
- Fine-tuning a model.
- Autonomous merges of skill or trigger edits.
- A polished web UI.
- Running arbitrary repo commands or tests locally.

**Success criteria for v1:**

| Metric | Target |
|---|---|
| Category accuracy on frozen eval set | ≥ 80%, and no worse than the baseline after any auto-change |
| Real bug classified as `flaky_test` | ≤ 5% of frozen `real_bug` cases |
| Median time from webhook to comment | < 3 minutes |
| Cost per triage | < configured budget (start at $0.50) |
| Secret redaction | 100% on canary fixtures |
| Action success rate (non-LLM failures) | ≥ 95% |
| Validated self-improvement PRs | ≥ 1 per two weeks once Phase 4 is live |

---

## 1. Target repository layout

Create this incrementally; do not scaffold empty directories that have no tests.

```text
code-triage-agent/
  pyproject.toml
  .env.example
  Dockerfile
  compose.yaml
  Makefile                      # or justfile; thin wrappers only
  config/
    triggers.yaml
    skills/
      triage-pr-failure.md
      find-culprit.md
      triage-nightly.md
      propose-fix.md
    memory/
      known_failures.jsonl
      repo_notes.md
  evals/
    frozen/                     # committed, never written by the agent
      <case-id>/
        case.json
        snapshot/
    rolling/                    # generated from recent labeled runs
  src/code_triage_agent/
    __init__.py
    __main__.py                 # CLI entrypoint
    cli.py
    settings.py
    logging.py
    models.py                   # shared Pydantic models
    db.py
    schema.sql
    github/
      __init__.py
      auth.py                   # App JWT + installation token cache
      client.py                 # typed REST wrapper
      logs.py                   # download + unzip job logs
      webhook.py                # signature verification + parsing
    collector/
      collect.py                # orchestration
      trim.py
      redact.py
      signature.py
      snapshot.py
    config/
      loader.py
      schema.py
      guard.py                  # path allowlist enforcement
    runner/
      router.py
      prompt.py
      llm.py
      loop.py
      tools.py
      actions.py
      record.py
    background/
      outcomes.py
      score.py
      patterns.py
      propose.py
      replay.py
      apply.py
      rollback.py
    server/
      app.py                    # webhook + health + metrics endpoints
    jobs/
      worker.py
      scheduler.py
  tests/
    unit/
    integration/
    fixtures/
      github/                   # recorded API responses
      logs/                     # sample job logs, including malicious ones
      llm/                      # scripted model responses
  var/                          # gitignored runtime data
    events/
    snapshots/
    triage.db
  docs/
    adr/
    runbooks/
```

Add `var/` to `.gitignore`.

---

## 2. Core contracts to settle before coding

These are the decisions that everything else depends on. Write them down in `docs/adr/` and encode them as Pydantic models in `models.py`.

### 2.1 Event and dedupe

Dedupe key: `(repository.full_name, workflow_run.id, workflow_run.run_attempt)`.

Process only:

- event: `workflow_run`
- action: `completed`
- conclusion: `failure` or `timed_out`

Ignore `cancelled` unless a trigger explicitly opts in later.

### 2.2 Database tables

Use plain `sqlite3` with WAL mode and `PRAGMA user_version` migrations. No ORM for v1.

| Table | Key columns |
|---|---|
| `events` | `id`, `delivery_id` (unique), `repo`, `workflow_run_id`, `run_attempt`, `conclusion`, `payload_path`, `status`, `error`, `received_at` |
| `runs` | `id`, `event_id`, `trigger_id`, `skill_id`, `skill_version`, `signature`, `snapshot_path`, `category`, `confidence`, `summary`, `actions_json`, `cost_usd`, `status`, `config_sha`, `started_at`, `finished_at` |
| `steps` | `run_id`, `seq`, `tool`, `args_json`, `result_json`, `is_error`, `latency_ms`, `cost_usd` |
| `outcomes` | `run_id`, `label` (`correct`/`incorrect`/`unknown`), `signals_json`, `evidence_json`, `collected_at` |
| `signatures` | `signature` (PK), `exemplar`, `job_name`, `step_name`, `first_seen`, `last_seen`, `count` |
| `proposals` | `id`, `kind`, `base_sha`, `patch`, `supporting_runs_json`, `eval_before_json`, `eval_after_json`, `status`, `pr_url`, `created_at` |
| `config_versions` | `sha`, `created_at`, `source` (`human`/`agent`), `note` |
| `evals` | `case_id`, `set_name` (`frozen`/`rolling`), `snapshot_path`, `expected_category`, `expected_action`, `notes` |

Indexes: `events(delivery_id)` unique, `events(repo, workflow_run_id, run_attempt)` unique, `runs(signature)`, `runs(started_at)`, `outcomes(label)`.

### 2.3 Config schemas

`config/triggers.yaml`:

```yaml
- id: pr_failure
  enabled: true
  when:
    event: workflow_run
    conclusion: [failure, timed_out]
    source: pull_request
  skill: triage-pr-failure
  allowed_actions: [comment_on_pr]
  budget: { max_steps: 12, max_cost_usd: 0.50, max_seconds: 180 }
```

Skill markdown frontmatter:

```yaml
---
id: triage-pr-failure
version: 1
allowed_tools: [read_file, git_log, git_diff, search_memory, comment_on_pr]
output_schema: submit_triage
---
```

Memory line (`known_failures.jsonl`):

```json
{"signature":"sig_v1:9f3e...","repo":"owner/name","note":"Integration timeouts are infra, not flaky.","confidence":"high","evidence_runs":["r_123"],"created_at":"2026-11-05T00:00:00Z","source":"human"}
```

Validate every file at startup and expose `code-triage-agent validate-config`.

### 2.4 Triage output schema

The model must end by calling `submit_triage` with:

```json
{
  "category": "flaky_test | infra_issue | dependency_or_config | real_bug | unknown",
  "confidence": 0.0,
  "summary": "one paragraph",
  "evidence": [{"source": "log | diff | memory | code", "quote": "exact substring", "why": "..."}],
  "memory_note": "optional note to persist for this signature",
  "recommended_action": {"type": "comment | rerun | fix_pr | issue | none", "params": {}},
  "needs_human": false
}
```

Validation rule: every `evidence.quote` must be a substring of the supplied snapshot or a tool result. If it is not, reject the output and retry once at lower temperature; if it fails again, output `unknown` and `needs_human: true`. This is the main anti-hallucination guard.

### 2.5 Tool contracts

All tool inputs are Pydantic models; JSON schemas are generated for the model. All calls are recorded in `steps`.

| Tool | Kind | Contract |
|---|---|---|
| `read_file(path, ref)` | read | Repo-relative only, max 100 KB, deny `var/`, `.env*`, private keys |
| `git_log(ref, path?, limit)` | read | Max 50 entries |
| `git_diff(base, head)` | read | Snapshot diff when replaying |
| `search_memory(signature)` | read | Returns matching notes only |
| `comment_on_pr(pr_number, body)` | act | Idempotent marker per run |
| `rerun_failed_jobs(run_id)` | act | At most once per run; refuses if a newer attempt exists |
| `open_issue(title, body, labels)` | act | Dedupe by signature |
| `open_fix_pr(branch, files, title, body)` | act | New branch only; never `main`; diff-size cap |

`dry_run` mode makes act tools return a simulated result but still records the call. `replay` mode replaces GitHub reads with snapshot data.

---

## 3. Step-by-step build plan

Each step is intended to be one commit or a small PR. "Done when" is the acceptance test.

### Phase 0 — Foundation (2–3 days)

**Exit criteria:** the package installs, lint/type/tests run in CI, config validates, and `/healthz` responds.

1. **Write the stack ADR.** `docs/adr/0001-v1-stack.md`: GitHub App (not Actions-only), FastAPI, SQLite, one worker, one LLM provider, single-repo deployment. Record alternatives rejected.
2. **Set up the toolchain.** Update `pyproject.toml` with dependencies: `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pydantic-settings`, `pyyaml`, an LLM SDK, and dev deps `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`, `pre-commit`. Add pytest/ruff/mypy config and a `Makefile` with `make check`.
3. **Add CI.** GitHub Actions: `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy`, `pytest`. Cache the uv environment.
4. **Implement `settings.py`.** Pydantic settings for GitHub App ID/private key/webhook secret, LLM API key/model, DB path, snapshot root, budget defaults, `DRY_RUN`, `REPLAY_MODE`, and per-action feature flags. Fail fast with a readable error on missing values. Commit `.env.example`; never commit real keys.
5. **Add logging + config validation + health.** JSON structured logs with `delivery_id`/`run_id` context; `code-triage-agent validate-config`; FastAPI app with `GET /healthz` that checks the DB and config.

Done when: `make check` passes locally and in CI; `validate-config` rejects a malformed trigger; `/healthz` returns 200.

---

### Phase 1 — Plumbing, no LLM (1.5–2 weeks)

**Exit criteria:** a real failed workflow run can be received, fetched, trimmed, redacted, signed, and stored as an input snapshot — with no model call anywhere in the path.

6. **DB layer.** Add `schema.sql`, migration runner (`PRAGMA user_version`), connection helper with WAL, and repository functions for `events`, `runs`, `steps`, `signatures`. Test dedupe inserts and concurrent claim behavior.
7. **GitHub App auth.** `github/auth.py`: sign a short-lived App JWT, exchange it for an installation token, cache until expiry, refresh on 401. Unit-test with fixed clocks.
8. **Typed GitHub client.** `github/client.py` with `httpx`: get workflow run, list jobs, download job logs (handle the redirect/zip), get PR diff, post comment, rerun failed jobs, get PR state, create branch/commit/PR. Add retries with jitter, rate-limit handling, and request logging. Mock with `respx`; record fixtures for offline tests.
9. **Webhook receiver.** `POST /webhooks/github`: read the raw body, verify `X-Hub-Signature-256` with `hmac.compare_digest`, ignore non-`workflow_run` events, filter to completed failures, persist the raw payload to `var/events/`, insert the event with a dedupe key, enqueue work, return `202` in under 5 seconds. Test duplicate delivery, bad signature, success conclusion, and malformed payload.
10. **Worker loop.** Poll the `events` table, claim a row atomically (`BEGIN IMMEDIATE`), process it, mark done/failed, retry with exponential backoff, and move exhausted jobs to a dead-letter status. Support graceful shutdown and an idempotent restart. Test crash-after-claim recovery.
11. **Collector metadata.** From the payload and API, resolve failed jobs, failed steps, PR number, head/base SHAs, attempt number, and workflow name. Persist the collector result as part of the run context.
12. **Log trimmer.** `collector/trim.py`: unzip logs, parse `##[group]`/`##[endgroup]`, find the failed step group (fall back to groups containing error keywords), keep ~300 lines before the first error and ~50 after, always include the job header/tail context, cap at ~200 KB total and 3 failed jobs. Return a structured `TrimResult` with the selected line ranges.
13. **Secret redaction.** `collector/redact.py`: regexes for GitHub tokens, bearer headers, `*_TOKEN=`, `*_PASSWORD=`, AWS/GCP keys, private key blocks, URL credentials, and configured repo-specific patterns. Redact before writing the snapshot and before building any prompt. Add canary tests that assert the secret never appears in the snapshot or prompt.
14. **Signature generator.** `collector/signature.py`: input is job name + failed step + trimmed log. Strip ANSI, timestamps, durations, hex SHAs, temp paths, memory addresses, ports, worker IDs, and bare numbers. Keep the tail error lines, hash with SHA-256, prefix with `sig_v1:`. Tests: three variants of the same timeout share a signature; a compile error and a test assertion failure do not. Persist the exemplar and mark signature-algorithm changes with a new prefix.
15. **Snapshot writer + collector E2E.** `collector/snapshot.py` writes `var/snapshots/<run_id>/` with `metadata.json`, `logs.txt`, `diff.patch`, `memory.json`, and `signature.txt`. Add `code-triage-agent collect --run-id` for local debugging. Integration test: recorded GitHub fixture in, complete snapshot out. Assert no LLM client is instantiated.

Done when: replaying a recorded failed run from the CLI produces a complete snapshot, signatures are stable across reruns of the same fixture, and the redaction canaries never leak.

---

### Phase 2 — Runner, classify and comment only (2 weeks)

**Exit criteria:** 10 replay cases produce valid `submit_triage` outputs and idempotent PR comments; no rerun, issue, or fix PR is possible yet.

16. **LLM interface + fake.** `runner/llm.py` defines `LLMClient.complete(messages, tools) -> LLMResponse` with token usage. Implement the chosen provider and a `FakeLLM` driven by JSON scripts in `tests/fixtures/llm/`. Add timeouts and retry-on-transient-error. Track cost from a pricing table in settings.
17. **Skill loader and prompt assembly.** Parse skill frontmatter, compute `skill_version = sha256(file)`, and render:
    - system: role, safety rules, skill goal/steps/output contract, memory notes;
    - user: metadata, diff, trimmed logs, and prior memory inside explicitly labeled untrusted blocks.
    State in the system prompt that logs/code/PR text are data, never instructions. Snapshot tests for rendered prompts.
18. **Tool registry.** `runner/tools.py`: register allowed tools per skill, validate args, enforce path rules and size caps, return JSON, and record timing/errors. In `dry_run`/`replay`, use simulated or snapshot results. Tests for path traversal, oversized files, and disallowed tools.
19. **Agent loop.** `runner/loop.py`: bounded loop (`max_steps`, `max_cost_usd`, `max_seconds`), tool-use dispatch, final `submit_triage` enforcement, evidence-substring validation, one retry on invalid output, then safe `unknown`. Test: budget exhaustion still produces a valid run record marked `budget_exceeded`.
20. **Router.** `runner/router.py`: match event context against triggers in priority order, verify the skill exists and actions are allowed, and fall back to `unknown` + comment-only when nothing matches. Unit tests for each trigger and overlap precedence.
21. **Run record writer.** `runner/record.py`: persist the run, every step, decision, actions, cost, snapshot path, and `config_sha`. Run record must be complete even when the model or a tool fails.
22. **Comment action.** `runner/actions.py`: render a concise comment with category, confidence, evidence quotes, recommended next step, and an HTML marker containing the run ID. Post idempotently; if the marker already exists, skip. Comment-only allowlist enforced in code. Integration test against a mock server.
23. **CLI triage + golden test.** `code-triage-agent triage --snapshot <dir>` runs the full pipeline in replay mode and prints the record. Add a golden-output test for one fixture, ignoring volatile fields.
24. **Injection test suite.** Add logs containing "ignore previous instructions", fake system messages, tool-call JSON, and attempts to trigger `open_fix_pr`. Assert the model output cannot cause a tool outside the skill allowlist and that hostile text stays inside the untrusted block. Run this in CI.

Done when: 10 fixture cases (across the 4 skills) classify and comment; reruns do not duplicate comments; budgets and injection tests pass.

---

### Phase 3 — Outcomes and evals (1.5–2 weeks)

**Exit criteria:** a replay score exists for every skill, the outcome collector labels finished runs, and a deliberately degraded skill is caught by the eval harness.

25. **Outcome collector.** `background/outcomes.py`: scheduled job for runs with `outcome IS NULL`. Poll rerun conclusion, fix-PR merge/close/revert state, signature recurrence in the next 7 days, and optional human reaction/reply. Respect GitHub rate limits and do not re-poll forever.
26. **Label decision table.** `background/score.py` maps signals to `correct`/`incorrect`/`unknown` with precedence: explicit human feedback → fix PR outcome → rerun result → recurrence. Keep `signals_json` and `evidence_json` for audit. Unit-test every row and ambiguous combinations.
27. **Eval case format and importer.** `case.json` holds `case_id`, `snapshot_path`, `expected_category`, `expected_action`, `labeler`, `notes`. Add `code-triage-agent eval import --run-id` to copy a snapshot + decision into a case for labeling.
28. **Hand-label 30 cases.** Minimum 6 per skill and at least 10 deliberately hard `real_bug` vs `flaky_test` boundary cases, plus 3 injection cases. Have a second person review at least 10. Freeze them under `evals/frozen/`; write the labeling guide in `docs/`.
29. **Replay harness.** `background/replay.py`: load a case, force `replay` mode and temperature 0, run the agent with a specified config SHA, and emit a JSON + Markdown report. Never write to `evals/frozen/`. Support a subset selector for PR CI.
30. **Metrics and score.** Compute accuracy, per-category confusion matrix, false-flaky rate, action precision, median cost, and failure reasons. Composite score:
    `0.45*accuracy + 0.25*(1-false_flaky_rate) + 0.20*action_precision + 0.10*cost_efficiency`.
    Hard gates are separate: no new false-flaky on frozen cases, no accuracy regression, cost within +10%.
31. **Baseline + CI eval.** Record the baseline report for v1. Run a 5-case smoke subset on PRs and the full frozen set nightly. Deliberately break a skill locally to prove the harness fails.

Done when: `eval run --set frozen` produces a reproducible report; outcome labels appear automatically; the harness catches a seeded regression.

---

### Phase 4 — Background agent, memory only (1 week)

**Exit criteria:** one real memory-note PR is proposed from real runs with evidence and an eval delta; every rejected proposal is logged with a reason; auto-merge is off by default.

32. **Scorer job.** Aggregate the last N labeled runs per skill/trigger: accuracy, bad-run IDs, cost, false-flaky cases. Persist a snapshot for the proposer.
33. **Pattern finder.** Deterministic grouping by trigger, signature, confusion pair, failed step, and repo. Example output: "7 of 9 false-flaky calls came from integration-test timeouts, signature `sig_v1:9f3e`." No LLM required; use one only to phrase the summary if desired.
34. **Memory proposer.** `background/propose.py`: for clusters with at least 3 supporting runs, draft a memory note (signature, claim, rule, confidence, evidence run IDs). Clone config to a scratch worktree and apply only `config/memory/**`.
35. **Validation pipeline.** Run the old and proposed config over the frozen set and a rolling sample. Use dry-run tools and snapshots only. Store both reports and a per-case diff on the proposal. Reject on any hard-gate failure.
36. **PR bot.** `background/apply.py`: branch `agent/memory-<date>-<id>`, PR body with claim, supporting run IDs, eval table, and rollback plan; labels `agent-proposal`, `auto-merge-candidate` when eligible. Auto-merge only memory notes with ≥3 runs, high confidence, and no regressions, behind `AUTO_MERGE_MEMORY=false` by default.
37. **Nightly scheduler.** Run outcomes → score → propose, max 3 proposals per night, with a global kill switch, dry-run default, and an alert on failure. Add a runbook.

Done when: a real cluster becomes a reviewed memory PR; the same input is reproducible; a forced regression prevents the PR.

---

### Phase 5 — Skill and trigger edits with review (1 week)

**Exit criteria:** the agent can propose a skill or trigger edit but can never merge it; guard tests block edits outside the allowlist.

38. **Extend the proposer.** Generate minimal unified diffs for `config/skills/**` and `config/triggers.yaml`, with a written hypothesis, expected effect, and supporting run IDs. Cap at one logical change per proposal.
39. **Config guard.** `config/guard.py` plus a CI workflow: reject diffs outside `config/skills/`, `config/triggers.yaml`, and `config/memory/`; reject writes to `evals/`, `src/`, tool permissions, prompts, and the agent's own instructions. Check both the proposer and CI (defense in depth). Unit-test path traversal and symlinks.
40. **Human review workflow.** PR template with evidence, eval before/after, risk, and rollback. Add a reviewer checklist. Skill/trigger PRs require a CODEOWNER approval.
41. **Config SHA plumbing.** Every run and eval stores the config git SHA; replay checks out that exact config. Test that changing a skill after a run changes the version recorded in new runs but not old ones.

Done when: a skill change is proposed, reviewed, and merged; an attempted eval-set edit is blocked by both the proposer and CI.

---

### Phase 6 — Actions and rollback (1.5–2 weeks)

**Exit criteria:** each action works in a test repo with an audit trail, and an automatic rollback PR has been demonstrated.

42. **`rerun_failed_jobs`.** Allow only when the skill permits it and this run has not already rerun. Refuse if a newer attempt exists. Record the rerun ID and let the outcome collector judge it.
43. **`open_issue`.** For main/nightly failures, dedupe by signature, link the run record, and apply labels. Do not open issues for PR failures unless configured.
44. **`open_fix_pr`.** Create `triage/fix-<run_id>` from the PR head, apply a minimal patch through the contents API, cap changed files/lines, run no repo code, and open a PR that links the failure. Never target `main`; require human merge.
45. **Rollback job.** Weekly, compare metrics per `config_sha` for changes older than 7 days. If accuracy drops >5 points or false-flaky rises >2 points over at least 20 runs, open a revert PR and alert.
46. **Metrics endpoint/view.** Read-only `/metrics` (JSON) and `/runs` (simple HTML or JSON) showing accuracy, false-flaky rate, cost, action outcomes, proposal history, and config versions. No separate frontend service.
47. **Gradual enablement.** Per-repo/per-action flags. Canary on one repo, then expand. Add a documented kill switch that disables all act tools without redeploying.

Done when: a rerun, issue, and fix PR have each been exercised end-to-end in the test repo; rollback has been tested with synthetic metrics.

---

### Phase 7 — Hardening and deployment (1 week)

**Exit criteria:** a clean deployment from scratch, a restore drill, and an on-call runbook.

48. **Security review.** Least-privilege GitHub App permissions, secret handling, prompt-injection regression suite, audit logs, SSRF/path checks, dependency review.
49. **Reliability tests.** Duplicate webhook bursts, GitHub 5xx/timeouts, rate limits, worker crash mid-run, DB lock contention, log-size extremes, and clock skew. Add DB backup/restore and run a restore drill.
50. **Deployment.** Multi-stage `Dockerfile`, `compose.yaml` with `server`, `worker`, `scheduler`, migration-on-start, health checks, log rotation, and a documented GitHub App setup. Secrets come from the environment/secret manager, never the image.
51. **Observability and alerts.** Alert on dead-letter growth, action failure rate, cost spikes, eval accuracy drop, and stale outcome collection. Document triage steps in `docs/runbooks/`.
52. **V1 sign-off.** Re-run the full frozen eval, verify every success criterion in section 0, and tag `v0.1.0`.

---

## 4. Suggested timeline

Estimates for one developer, assuming part-time review:

| Phase | Duration | Cumulative |
|---|---|---|
| 0. Foundation | 2–3 days | week 1 |
| 1. Plumbing | 1.5–2 weeks | weeks 2–3 |
| 2. Runner (comment only) | 2 weeks | weeks 4–5 |
| 3. Outcomes + evals | 1.5–2 weeks | weeks 6–7 |
| 4. Memory-only background agent | 1 week | week 8 |
| 5. Skill/trigger proposals | 1 week | week 9 |
| 6. Actions + rollback | 1.5–2 weeks | weeks 10–11 |
| 7. Hardening + deploy | 1 week | week 12 |

If time is tight, stop after Phase 3. A classify-and-comment agent with a real eval loop is already useful; self-modification without a trustworthy eval set is not.

---

## 5. Eval and acceptance rules

Use these consistently in the runner, replay harness, and proposer.

**Per-run signals**

- Predicted `flaky_test` + rerun passed → `correct`.
- Predicted `flaky_test` + rerun failed with the same signature → `incorrect`.
- Fix PR merged → `correct`; closed or reverted → `incorrect`.
- Same signature recurs within 7 days after a claimed fix → `incorrect` unless a human overrides.
- Human reaction/reply → weak evidence; never overrides a frozen label.

**Hard gates for any config change**

1. No regression on any frozen case that was previously correct.
2. No increase in frozen false-flaky count.
3. At least 3 supporting runs (for memory notes) and a stated hypothesis.
4. Cost increase ≤ 10%.
5. Config guard passes.
6. At most 3 proposals per night; one logical change per PR.

**Metric definitions**

- `false_flaky_rate = false-flaky predictions / all non-flaky labeled cases`.
- `action_precision = successful actions / actions attempted`.
- `cost_efficiency = min(1, budget / actual_cost)`.
- Accuracy is reported overall and per skill/category; the confusion matrix is mandatory in every report.

---

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Prompt injection from logs, code, or PR text | Untrusted-block formatting, tool allowlist, no free-form shell, injection fixtures in CI, evidence-quote validation |
| Secret leakage into snapshots or prompts | Redact before persist and before prompt; canary tests; no raw log retention by default |
| Real bug called flaky (the dangerous error) | Dedicated metric with heavy weight, hard gate, rerun evidence required before `flaky_test`, human review for borderline confidence |
| Runaway LLM cost | Step/time/cost caps per run, per-day cap, pricing table, alerts |
| Webhook loss or duplicate processing | Fast 202, durable event row, dedupe index, retries, dead-letter, reconciliation job |
| GitHub rate limits/log download failures | Backoff, conditional requests, cache fixtures, skip with `unknown` rather than crash |
| Agent edits something it should not | Path allowlist enforced in proposer and CI, separate branch, human review, no write credentials in the proposer beyond PR creation |
| Eval set contamination/drift | Frozen set is read-only and CODEOWNER-protected; rolling set is separate; config SHA recorded |
| SQLite contention | WAL, short transactions, one worker, claim-by-update; migrate to Postgres only if measurements demand it |
| Model/provider changes break prompts | Pin model version in settings; store model ID with each run; keep `FakeLLM` and golden prompt tests |

---

## 7. Open decisions (resolve in Phase 0)

1. **LLM provider and model.** Pick one, pin the version, record it per run. The interface must allow swapping.
2. **GitHub App permissions.** Minimum: Actions read, Checks read, Contents read/write (fix PR), Pull requests read/write, Issues read/write. Justify each.
3. **Where config lives.** v1 in this repo. Revisit a separate config repo only if the agent's PRs create too much noise.
4. **Deployment target.** One VM with Docker Compose is the default; the plan should not depend on a cloud vendor.
5. **Scheduler.** APScheduler inside the worker for v1; external cron only if the worker is split.
6. **Retention.** Snapshots and logs retained for 90 days by default; define the deletion job.

---

## 8. First week checklist

- [ ] ADR for stack and provider written.
- [ ] Dependencies, lint, types, tests, CI, `.env.example` in place.
- [ ] Settings and config validation working.
- [ ] `schema.sql` + migration runner + repository tests.
- [ ] GitHub App registered in the test repo; webhook delivery verified with `gh` or smee.io.
- [ ] `POST /webhooks/github` verifies signatures and returns 202 for a real failure.
- [ ] One recorded failure payload committed as a test fixture.

When those are checked, start Phase 1 step 6 (DB/worker) and do not touch the LLM yet.
