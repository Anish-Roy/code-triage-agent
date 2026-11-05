# Code Triage agent

## What the system does

When a CI build fails, the **runner agent** reads the logs and decides what kind of failure it is. It then acts: re-runs a flaky test, comments on the PR with the likely cause, or opens a fix PR.

A **background agent** checks how well the runner did and edits the runner's skills, triggers and memory to improve it.

## Architecture

```mermaid
flowchart TD
  A["GitHub: workflow_run completed"] --> B["Event gate: failures only, dedupe"]
  B --> C["Collector: jobs, logs, diff, signature"]
  C --> D["Router: match a trigger"]
  D --> E["Runner agent runs the skill"]
  E --> F["Actions: PR comment, rerun, fix PR, issue"]
  E --> G[("Run log store")]
  H["Outcome collector, no LLM"] --> G
  F -.-> H
  G --> I["Background agent"]
  I --> J["Replay on eval set"]
  J -->|better| K["PR to config repo"]
  J -->|worse| L["Reject and log reason"]
  K --> M[("Config: skills, triggers, memory")]
  M --> D
  M --> E
```

## Part 1: The runner agent

**Triggers** live in `config/triggers.yaml`. Each one maps to a skill file:

| Trigger | When | Skill |
|---|---|---|
| `pr_failure` | CI fails on a PR | `triage-pr-failure` |
| `main_failure` | CI fails on main | `find-culprit` |
| `nightly_failure` | Nightly run fails | `triage-nightly` |

```yaml
- id: pr_failure
  when: { event: workflow_run, conclusion: failure, source: pull_request }
  skill: triage-pr-failure
  budget: { max_steps: 12, max_cost_usd: 0.50 }
```

**Skills** are markdown files. Each one has a goal, the steps to follow, the output format, and rules like "never call something flaky unless you saw it pass on the same commit". Initially we will have 4 skills: `triage-pr-failure`, `find-culprit`, `triage-nightly`, `propose-fix`.

**Flow for one failure:**
1. **Gate.** Ignore successes. Dedupe by run ID and attempt number.
2. **Collect (plain code, no LLM).** Get the failed job and failed step. Download the logs. Cut them down to the lines around the failing step. Remove secrets. Get the commit diff.
3. **Make a signature.** Normalize the error lines (remove timestamps, paths, numbers) and hash them. This lets you recognize the same failure later.
4. **Look up memory** by signature. Past notes go into the prompt.
5. **Route** to the matching skill and run the agent loop.
6. **Act, then write a run record.**

**Categories:** `flaky_test`, `infra_issue`, `dependency_or_config`, `real_bug`, `unknown`.

**Tools the runner gets:**

| Tool | Notes |
|---|---|
| `read_file(path, sha)` | read-only |
| `git_log`, `git_diff` | read-only |
| `search_memory(signature)` | read-only |
| `rerun_failed_jobs` | at most once per run |
| `comment_on_pr` | |
| `open_fix_pr` | new branch only, never main |
| `open_issue` | |

**Safety rules for the runner:**
- Logs, PR text and code are **untrusted**. They could contain instructions meant to trick the agent. Pass them as data and never let them change the rules.
- It cannot push to protected branches.
- Every run has a step and cost cap.

## Part 2: The run record

Save one record per run. The background agent depends on this.

```json
{
  "run_id": "r_1234",
  "trigger_id": "pr_failure",
  "skill_version": "a1b2c3",
  "signature": "9f3e...",
  "input_snapshot": "path to trimmed logs, diff, memory used",
  "steps": ["tool calls and results"],
  "decision": { "category": "flaky_test", "confidence": 0.82 },
  "actions": ["rerun_failed_jobs", "comment"],
  "cost_usd": 0.12,
  "outcome": null
}
```

Saving the `input_snapshot` is important. It lets you replay the case later without calling GitHub.

**Outcome collector (no LLM).** A scheduled job fills in the `outcome` field using these signals:

| Signal | Meaning |
|---|---|
| Runner said flaky, and the re-run passed | label probably right |
| Runner said flaky, and the re-run failed again | label wrong |
| Same signature fails again within 7 days | earlier diagnosis probably wrong |
| Fix PR merged | good |
| Fix PR closed or reverted | bad |
| Human reply or reaction on the comment (optional) | direct feedback |

## Part 3: The background agent

It runs nightly, or after every N new labeled runs.

1. **Score.** Compute accuracy per skill and trigger, fix PR merge rate, and cost.
2. **Find patterns.** Group the bad runs. For example: "7 of 9 wrong `flaky_test` calls came from timeouts in the integration tests".
3. **Propose changes.** It can propose these kinds of edits:
   - Add or fix a memory entry for a signature.
   - Edit a skill, for example add the rule "timeouts in integration tests are infra, not flaky".
   - Add a new trigger or change a threshold.
4. **Validate.** Replay the eval set with the old and new config. Tools run in dry-run mode, so nothing really happens. Accept the change only if the score is no worse overall and key cases do not regress.
5. **Apply.** Open a PR against the config repo. Each change cites the run IDs that justify it.
6. **Watch.** If metrics drop in the week after a change, roll it back.

**Guardrails for the background agent:**
- It may edit only `config/skills/`, `config/triggers.yaml` and `config/memory/`.
- It may **not** edit the eval set, tool permissions, or its own prompt.
- A change needs at least 3 supporting runs.
- At most 3 changes per night.
- Memory entries with strong evidence can auto-merge. Skill and trigger edits need human review at first.

**The eval set** has two parts. A frozen set is hand-labeled, never touched by the agent, and catches regressions. A rolling set holds recent labeled runs and tracks drift.

**The most dangerous mistake** is calling a real bug "flaky". Track this as its own metric and weight it heavily in scoring.

## Repo layout

```
ci-triage/
  runner/        webhook or workflow entry, collector, agent loop, tools
  background/    outcome collector, scorer, proposer, replay harness
  config/
    triggers.yaml
    skills/      triage-pr-failure.md, find-culprit.md, ...
    memory/      known_failures.jsonl, repo_notes.md
  evals/
    frozen/      hand-labeled cases
    rolling/
  store/         run records (SQLite to start)
```

## Build order

1. **Plumbing, no LLM.** Receive the event, fetch logs, trim them, make signatures, store records.
2. **Runner, classify only.** Comment on PRs with a category and your own rating. No actions yet.
3. **Outcomes and evals.** Build the outcome collector, hand-label about 30 cases, and build the replay harness.
4. **Background agent, memory only.** It proposes signature notes. This is the safest edit type.
5. **Skill and trigger edits.** Add validation and human review.
6. **Actions and auto-apply.** Add re-run, fix PRs, rollback and a simple metrics page.
