# 🚧 Under construction

# Code Triage agent

## What the system does

When a CI build fails, the **runner agent** reads the logs and decides what kind of failure it is. It then acts: re-runs a flaky test, comments on the PR with the likely cause, or opens a fix PR.

A **background agent** checks how well the runner did and edits the runner's skills, triggers and memory to improve it.

This system helps align the agent's behavior with the associated repositories conventions and styles, makes codes fixes easier to review and work better than a generic code review bot.

(This is inspired from Mintlify's self-updated documentation agent. Read more [here](https://x.com/patrickrfos/status/2116194642417037766))

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

## Development

Requires [uv](https://docs.astral.sh/uv/) (Python 3.13 is managed by uv).

```bash
cp .env.example .env   # fill in GitHub App values when testing webhooks
uv sync

make check             # ruff + mypy + pytest
make validate          # validate config/triggers.yaml, skills, memory
make serve             # http://localhost:8000/healthz
```

The webhook receiver, collector, and runner arrive in Phases 1-2; see
[`IMPLEMENTATION.md`](./IMPLEMENTATION.md) for the ordered build plan and
[`plan.md`](./plan.md) for the system design.