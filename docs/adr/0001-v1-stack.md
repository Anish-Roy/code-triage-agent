# ADR 0001: V1 stack and architecture

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Project maintainers

## Context

The code triage agent receives GitHub `workflow_run` events, collects failure
evidence, runs an LLM agent against a skill, performs a small set of GitHub
actions, and stores a run record that a background agent later uses to improve
configuration. V1 targets one repository plus one test repository.

The system needs durable event handling, an auditable record of every model and
tool step, and a configuration surface that can be changed safely.

## Decision

### Language and tooling

- Python 3.13, managed with `uv` and the `uv_build` backend.
- `ruff` for linting/formatting, `mypy` for types, `pytest` for tests.
- GitHub Actions runs the same checks as `make check`.

### Runtime

- **FastAPI + uvicorn** for the webhook receiver and health/metrics endpoints.
- **SQLite in WAL mode** for events, runs, steps, outcomes, proposals, and eval
  metadata. No ORM in v1; plain `sqlite3` plus `PRAGMA user_version` migrations.
- **One worker process** that claims events from the database. No Redis, no
  Celery, no Postgres until measurements show they are needed.
- **APScheduler** inside the worker for scheduled outcome collection, nightly
  proposals, and rollback checks. Move the scheduler out only if the worker is
  split.
- **One container** deployed on a single VM with Docker Compose. Components are
  split into `server`, `worker`, and `scheduler` services, but share a database
  file/volume.

### GitHub integration

- A **GitHub App**, not an Actions-only workflow, because the runner needs to
  post comments, rerun jobs, and open PRs across workflows.
- Minimum permissions: Actions read, Checks read, Contents read/write (fix PR),
  Pull requests read/write, Issues read/write.
- Webhook handling returns `202` quickly; work is persisted before processing.
- Dedupe key: `(repository.full_name, workflow_run.id, workflow_run.run_attempt)`.

### LLM

- All model access goes through a small `LLMClient` interface so the provider and
  model can be swapped, faked in tests, and pinned per run.
- The concrete provider and model are deliberately deferred to Phase 2, when the
  first model call is built. Phase 0 and Phase 1 must remain runnable with no
  provider SDK installed. This avoids locking the project to a vendor before the
  tool and prompt contracts exist.
- Temperature is `0` for replay and evaluation.

### Configuration

- `config/` lives in this repo for v1: `triggers.yaml`, `skills/*.md`, and
  `memory/*`.
- The background agent may only write to those paths, only on a branch, and only
  through a PR. A code-level guard plus CI enforce the allowlist.
- `evals/frozen/` is never writable by the agent.

## Consequences

Positive:

- Small operational surface: one language, one database, one deployable.
- Every decision and tool call can be audited from SQLite and snapshots.
- Provider-agnostic from day one; tests never need network access.
- Config changes are reviewable diffs.

Negative:

- SQLite and a single worker limit throughput and require care around locks.
  Acceptable for one to a few repositories.
- APScheduler inside the worker means only one scheduler instance may run. This
  is enforced by deployment (one `scheduler` service).
- Deferring the LLM provider means Phase 0 cannot test prompt construction yet.
  Prompt tests arrive with the interface in Phase 2.

## Alternatives considered

- **GitHub Actions only:** cannot reliably rerun jobs, comment, and open fix PRs
  with the same permissions and audit trail.
- **Postgres + Redis + Celery:** unnecessary operational cost at v1 scale.
- **ORMs (SQLModel/SQLAlchemy):** hides the small number of queries and adds
  migration complexity; revisit if the schema grows.
- **Separate config repository:** safer isolation, but too much friction before
  the proposal workflow is proven. Revisit after Phase 5.
