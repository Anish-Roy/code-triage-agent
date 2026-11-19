# Replay report: `frozen`

- Run at: 2026-11-19T22:53:07.997157+05:30
- Config SHA: `31e886dc03ff2e61ddb94f363855ab1f59c158809d1946557929184a5c138cd5`
- Model: `fake-llm`
- Cases: 30
- Composite score: **1.0000**

## Metrics

| Metric | Value |
|---|---|
| Accuracy | 100.00% (30/30) |
| False-flaky rate | 0.00% (0/24 non-flaky cases) |
| Action precision | 100.00% (23/23) |
| Median cost | $0.0000 |
| Total cost | $0.0000 |
| Cost efficiency | 100.00% |

### Accuracy by skill

| Skill | Accuracy |
|---|---|
| `find-culprit` | 100.00% |
| `propose-fix` | 100.00% |
| `triage-nightly` | 100.00% |
| `triage-pr-failure` | 100.00% |

### Accuracy by expected category

| Category | Accuracy |
|---|---|
| `dependency_or_config` | 100.00% |
| `flaky_test` | 100.00% |
| `infra_issue` | 100.00% |
| `real_bug` | 100.00% |
| `unknown` | 100.00% |

## Confusion matrix

| expected \ predicted | `dependency_or_config` | `flaky_test` | `infra_issue` | `real_bug` | `unknown` |
|---|---|---|---|---|---|
| `dependency_or_config` | 5 | 0 | 0 | 0 | 0 |
| `flaky_test` | 0 | 6 | 0 | 0 | 0 |
| `infra_issue` | 0 | 0 | 3 | 0 | 0 |
| `real_bug` | 0 | 0 | 0 | 9 | 0 |
| `unknown` | 0 | 0 | 0 | 0 | 7 |

## Mismatches

_Every case matched its expected category._

## Hard gates

**Passed:** yes
- `baseline_available`: pass
- `no_new_false_flaky`: pass
- `no_accuracy_regression`: pass
- `cost_within_10_percent`: pass
- `within_budget`: pass
- `config_guard`: pass
