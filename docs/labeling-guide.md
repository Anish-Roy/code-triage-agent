# Labeling guide: frozen triage cases

This guide is the contract for every `evals/frozen/<case_id>/case.json`. The
goal is not to make the agent look good; it is to make a wrong prediction
visible. Labels must be defensible from the snapshot alone.

## Case layout

```text
evals/frozen/<case_id>/
  case.json          # authoritative label and metadata
  snapshot/          # metadata.json, logs.txt, diff.patch, memory.json, ...
  llm_script.json    # optional recorded/cached turns for deterministic runs
```

`case.json` is authoritative. The SQLite `evals` table is only an index; if the
two disagree, the file wins.

### Required fields

| Field | Meaning |
|---|---|
| `case_id` | Lowercase directory-safe id; must match the directory name. |
| `snapshot_path` | `snapshot` (relative to the case). |
| `expected_category` | One of `flaky_test`, `infra_issue`, `dependency_or_config`, `real_bug`, `unknown`. |
| `expected_action` | One of `comment`, `rerun`, `fix_pr`, `issue`, `none`. |
| `labeler` | Who produced the first label. `seed-synthetic` means generated, not human-confirmed. |
| `expected_skill` | Force the skill when routing would not select it (especially `propose-fix`). |
| `notes` | Why the label is right, including what would falsify it. |
| `tags` | Free-form; `smoke` and `injection` are meaningful to CI. |
| `hard_boundary` | Marks a deliberately difficult `real_bug` vs `flaky_test` decision. |
| `reviewer` / `reviewed_at` | Second reviewer. At least 10 cases must carry both. |

## Category definitions

### `flaky_test`

The same test or assertion is known to pass without a code change: it passed on
retry at the same commit, or the exact signature has a documented flaky history
in memory with rerun evidence. "It looks slow" or "it failed once" is not enough.

### `real_bug`

A deterministic product/test-code defect is visible in the log or diff. The diff
touches the failing path, or the assertion is stable across reruns. A single
earlier rerun pass does **not** cancel a deterministic diff.

### `infra_issue`

The failure happens before product code is exercised, or is caused by the
environment: runner/network failures, toolchain downloads, database
unavailability, or suite-level timeouts with no assertion evidence. A timeout
with a deterministic assertion in the log is not infra.

### `dependency_or_config`

Install/resolution/configuration fails: an unavailable or conflicting version, a
missing module, a changed environment key, or a workflow/config path problem.

### `unknown`

The snapshot does not contain enough trustworthy evidence to choose. Use
`expected_action: none` and require human follow-up. Injection attempts,
truncated logs, and bare assertion failures without context belong here.

## Boundary rules (the hard 10+)

1. **Retry pass plus a deterministic diff:** `real_bug`. The retry pass is
   incidental; the diff explains the failure.
2. **Retry pass plus a trivial test-only diff:** `flaky_test`.
3. **Memory says flaky but the log shows a new assertion:** do not inherit the
   old flaky label. Check whether the signature actually matches.
4. **Timeout with an assertion:** `real_bug` or `flaky_test` depending on the
   assertion evidence; never `infra_issue` just because the job timed out.
5. **Timeout before tests run:** `infra_issue`.
6. **Config key typo in the diff:** `real_bug` when product code breaks, not
   `dependency_or_config`; use `dependency_or_config` for install/runtime
   configuration.
7. **A rerun that failed with a different signature:** `unknown` for a flaky
   prediction unless another signal decides it.
8. **A merged fix PR followed by recurrence inside 7 days:** `incorrect`.
9. **A closed or reverted fix PR:** `incorrect`, full stop.
10. **Human feedback after the agent's run marker:** authoritative, but never
    overrides a frozen label retroactively. Fix the label with a review instead.

## Injection cases

Three frozen cases carry hostile text in logs, diffs, or memory. Their expected
category is `unknown`/`none`. A model that follows the injected instruction is
wrong even if the instruction happens to name the right category. Do not copy
the hostile text into notes or comments.

## Human feedback protocol

Post a reply **after** the agent's comment containing its run marker:

```text
triage-label: correct
triage-label: incorrect
triage-label: unknown
```

Feedback before the marker is ignored. The collector never calls an LLM; the
label table applies this precedence:

1. explicit human feedback,
2. fix-PR outcome (merged / closed / reverted, plus recurrence),
3. rerun result (decisive only for a `flaky_test` prediction),
4. recurrence alone (usually `unknown`).

## Review workflow

1. Start from a real run with `code-triage-agent eval import --run-id r_...`.
   The importer copies the snapshot and uses the run's prediction as a starting
   label, marked `imported`.
2. Correct `case.json` from the snapshot, not from the run's summary.
3. Add `notes` that cite the exact log/diff line that decides the case.
4. Ask a second reviewer for at least 10 cases; add `reviewer` and `reviewed_at`.
5. Run `code-triage-agent eval run --set frozen --case <case_id>`.
6. Never edit `evals/frozen/` from the harness or CI. Reports go to
   `evals/reports/`.

## What not to label

- Do not label from the agent's own summary; it is the thing under test.
- Do not invent evidence quotes. Every quote must be an exact substring of the
  snapshot or a tool result.
- Do not lower a label to make a gate pass. Change the skill, not the truth.
