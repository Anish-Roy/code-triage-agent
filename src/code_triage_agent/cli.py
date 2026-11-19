"""Command-line entrypoints."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from code_triage_agent import __version__
from code_triage_agent.config import ConfigError, load_config
from code_triage_agent.db import Database, get_event, insert_event
from code_triage_agent.github.webhook import (
    WebhookError,
    parse_payload,
    parse_workflow_run_event,
    write_event_payload,
)
from code_triage_agent.logging import bind_context, configure_logging
from code_triage_agent.models import EventRecord
from code_triage_agent.settings import Settings

if TYPE_CHECKING:
    from code_triage_agent.github.client import GitHubClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-triage-agent")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("validate-config", help="validate config/ and exit")

    serve = subparsers.add_parser("serve", help="run the webhook server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    collect = subparsers.add_parser("collect", help="collect a stored event into an input snapshot")
    collect.add_argument("--event-id", type=int, help="id of an events row")
    collect.add_argument("--payload", type=Path, help="path to a raw webhook payload")
    collect.add_argument("--delivery-id", help="delivery id to use with --payload")
    collect.add_argument("--repo", help="owner/name, required with --payload")
    collect.add_argument("--run-id", type=int, help="GitHub workflow run id")
    collect.add_argument(
        "--attempt", type=int, default=None, help="run attempt number (default: payload value)"
    )

    worker = subparsers.add_parser("worker", help="process queued webhook events")
    worker.add_argument("--once", action="store_true", help="process at most one event")
    worker.add_argument("--poll-interval", type=float, default=2.0)

    triage = subparsers.add_parser("triage", help="triage an input snapshot in replay mode")
    triage.add_argument("--snapshot", type=Path, required=True, help="snapshot directory")
    triage.add_argument(
        "--script",
        type=Path,
        help="FakeLLM script (default: <snapshot>/llm_script.json)",
    )
    triage.add_argument("--skill", help="force a skill instead of routing the trigger")
    triage.add_argument(
        "--persist",
        action="store_true",
        help="write the run record when its event exists in the database",
    )

    outcomes = subparsers.add_parser("outcomes", help="collect outcome signals for finished runs")
    outcomes_sub = outcomes.add_subparsers(dest="outcomes_command")
    outcomes_collect = outcomes_sub.add_parser("collect", help="label finished runs")
    outcomes_collect.add_argument("--limit", type=int, default=50)
    outcomes_collect.add_argument("--repo", help="only poll runs for this owner/name")
    outcomes_collect.add_argument("--json", action="store_true")

    evals = subparsers.add_parser("eval", help="import and replay eval cases")
    eval_sub = evals.add_subparsers(dest="eval_command")

    eval_import = eval_sub.add_parser("import", help="import a run into a new eval case")
    eval_import.add_argument("--run-id", required=True)
    eval_import.add_argument("--case-id", help="case id (default: derived from the run)")
    eval_import.add_argument(
        "--set-dir",
        type=Path,
        default=Path("evals/frozen"),
        help="target case set directory (default: evals/frozen)",
    )
    eval_import.add_argument("--labeler", default="unlabeled")
    eval_import.add_argument("--reviewer")
    eval_import.add_argument("--notes")

    eval_run = eval_sub.add_parser("run", help="replay a case set and write reports")
    eval_run.add_argument(
        "--set",
        default="frozen",
        help="eval set name under evals/ (default: frozen)",
    )
    eval_run.add_argument("--set-dir", type=Path, help="explicit case set directory")
    eval_run.add_argument("--config-dir", type=Path, help="config directory to evaluate")
    eval_run.add_argument("--report-dir", type=Path, help="writable report directory")
    eval_run.add_argument("--case", action="append", default=[], help="case id (repeatable)")
    eval_run.add_argument("--smoke", action="store_true", help="run the small PR subset")
    eval_run.add_argument(
        "--live",
        action="store_true",
        help="allow live provider calls on cache miss (requires CTA_LLM_API_KEY)",
    )
    eval_run.add_argument("--baseline", type=Path, help="baseline report for hard gates")
    eval_run.add_argument("--json", action="store_true")

    eval_report = eval_sub.add_parser("report", help="print a saved replay report")
    eval_report.add_argument("--report", type=Path, required=True)
    eval_report.add_argument("--json", action="store_true")

    eval_gates = eval_sub.add_parser("gates", help="evaluate hard gates against a baseline")
    eval_gates.add_argument("--report", type=Path, required=True)
    eval_gates.add_argument("--baseline", type=Path, required=True)
    eval_gates.add_argument("--budget-usd", type=float)
    eval_gates.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "validate-config":
        return _validate_config(settings)
    if args.command == "serve":
        configure_logging(settings.log_level)
        import uvicorn

        uvicorn.run(
            "code_triage_agent.server.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0
    if args.command == "collect":
        configure_logging(settings.log_level)
        return _collect(settings, args)
    if args.command == "worker":
        configure_logging(settings.log_level)
        return _worker(settings, args)
    if args.command == "triage":
        configure_logging(settings.log_level)
        return _triage(settings, args)
    if args.command == "outcomes":
        configure_logging(settings.log_level)
        return _outcomes(settings, args)
    if args.command == "eval":
        configure_logging(settings.log_level)
        return _eval(settings, args)

    parser.print_help()
    return 2


def _validate_config(settings: Settings) -> int:
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(config.triggers)} triggers, {len(config.skills)} skills, "
        f"{len(config.memory_notes)} memory notes, config_sha={config.config_sha[:12]}"
    )
    return 0


def _collect(settings: Settings, args: argparse.Namespace) -> int:
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()

    try:
        event = _resolve_event(database, settings, args)
    except (WebhookError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if event is None:
        print(f"ERROR: event {args.event_id} not found", file=sys.stderr)
        return 1

    from code_triage_agent.jobs.processor import build_collect_processor

    try:
        processor = build_collect_processor(settings, config, database)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        with bind_context(delivery_id=event.delivery_id, event_id=event.id):
            processor(event)
    except Exception as exc:
        print(f"ERROR: collection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        processor.close()

    print(f"OK: event {event.id} collected into {settings.snapshot_root}")
    return 0


def _resolve_event(
    database: Database, settings: Settings, args: argparse.Namespace
) -> EventRecord | None:
    if args.event_id is not None:
        with database.connection() as connection:
            return get_event(connection, args.event_id)

    if args.payload is None or args.run_id is None or args.repo is None:
        raise ValueError("--payload, --repo and --run-id are required without --event-id")

    body = args.payload.read_bytes()
    wrapped = parse_workflow_run_event(parse_payload(body))
    if wrapped.workflow_run.id != args.run_id:
        raise ValueError(
            f"--run-id {args.run_id} does not match payload run id {wrapped.workflow_run.id}"
        )
    run_attempt = args.attempt if args.attempt is not None else wrapped.workflow_run.run_attempt
    delivery_id = args.delivery_id or uuid.uuid4().hex
    payload_path = write_event_payload(settings.event_root, delivery_id, body)

    with database.connection() as connection:
        event = insert_event(
            connection,
            delivery_id=delivery_id,
            repo=args.repo,
            workflow_run_id=args.run_id,
            run_attempt=run_attempt,
            conclusion=str(wrapped.workflow_run.conclusion or "failure"),
            payload_path=str(payload_path),
        )
        if event is None:
            from code_triage_agent.db import get_event_by_dedupe

            return get_event_by_dedupe(
                connection,
                repo=args.repo,
                workflow_run_id=args.run_id,
                run_attempt=run_attempt,
            )
    return event


def _triage(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from code_triage_agent.runner.llm import Pricing, load_script
    from code_triage_agent.runner.pipeline import triage_snapshot

    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    script_path = args.script or (args.snapshot / "llm_script.json")
    if not script_path.is_file():
        print(
            f"ERROR: replay script not found: {script_path}. "
            "Phase 2 has no live provider client; pass --script.",
            file=sys.stderr,
        )
        return 1

    try:
        llm = load_script(
            script_path,
            pricing=Pricing(
                input_per_mtok=settings.llm_input_cost_per_mtok,
                output_per_mtok=settings.llm_output_cost_per_mtok,
            ),
        )
    except Exception as exc:
        print(f"ERROR: could not load replay script: {exc}", file=sys.stderr)
        return 1

    replay_settings = settings.model_copy(update={"replay_mode": True, "dry_run": True})
    try:
        result = triage_snapshot(
            snapshot_dir=args.snapshot,
            settings=replay_settings,
            config=config,
            llm=llm,
            persist=args.persist,
            skill_override=args.skill,
        )
    except Exception as exc:
        print(f"ERROR: triage failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.artifact.to_dict(), indent=2, sort_keys=True))
    return 0 if result.artifact.status == "succeeded" else 1


def _outcomes(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from code_triage_agent.background.outcomes import collect_outcomes

    if getattr(args, "outcomes_command", None) != "collect":
        print("usage: code-triage-agent outcomes collect", file=sys.stderr)
        return 2

    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()

    client = _build_github_client(settings)
    if client is None:
        print(
            "ERROR: GitHub App credentials are required to collect outcomes: set "
            "CTA_GITHUB_APP_ID, CTA_GITHUB_APP_PRIVATE_KEY and CTA_GITHUB_INSTALLATION_ID",
            file=sys.stderr,
        )
        return 1

    try:
        summary = collect_outcomes(
            database,
            client,
            limit=args.limit,
            repo=args.repo,
        )
    except Exception as exc:
        print(f"ERROR: outcome collection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    else:
        print(
            "OK: "
            f"checked={summary.checked} labeled={summary.labeled} "
            f"unknown={summary.unknown} terminal={summary.terminal} "
            f"errors={summary.errors} skipped={summary.skipped}"
        )
    return 0


def _build_github_client(settings: Settings) -> GitHubClient | None:
    from code_triage_agent.github.auth import GitHubAppAuth
    from code_triage_agent.github.client import GitHubClient

    if not settings.github_app_configured():
        return None
    assert settings.github_app_private_key is not None
    assert settings.github_app_id is not None
    assert settings.github_installation_id is not None
    auth = GitHubAppAuth(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key.get_secret_value(),
        installation_id=settings.github_installation_id,
    )
    return GitHubClient(auth=auth)


def _eval(settings: Settings, args: argparse.Namespace) -> int:
    if args.eval_command == "import":
        return _eval_import(settings, args)
    if args.eval_command == "run":
        return _eval_run(settings, args)
    if args.eval_command == "report":
        return _eval_report(args)
    if args.eval_command == "gates":
        return _eval_gates(args)
    print("usage: code-triage-agent eval {import,run,report,gates}", file=sys.stderr)
    return 2


def _eval_import(settings: Settings, args: argparse.Namespace) -> int:
    from code_triage_agent.evals.cases import CaseError
    from code_triage_agent.evals.import_run import import_run

    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()
    try:
        target = import_run(
            database,
            run_id=args.run_id,
            target_dir=args.set_dir,
            case_id=args.case_id,
            labeler=args.labeler,
            reviewer=args.reviewer,
            notes=args.notes,
        )
    except CaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: imported {args.run_id} into {target}")
    return 0


def _eval_run(settings: Settings, args: argparse.Namespace) -> int:
    import json

    from code_triage_agent.background.replay import run_replay
    from code_triage_agent.config import ConfigError, load_config
    from code_triage_agent.evals.cases import CaseError

    set_dir = (args.set_dir or (Path("evals") / args.set)).resolve()
    config_dir = (args.config_dir or settings.config_dir).resolve()
    try:
        load_config(config_dir)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    baseline = args.baseline
    if baseline is None:
        default_baseline = Path("evals/baselines") / f"{args.set}.json"
        baseline = default_baseline if default_baseline.is_file() else None

    try:
        outcome = run_replay(
            set_dir=set_dir,
            settings=settings,
            config_dir=config_dir,
            report_dir=args.report_dir,
            case_ids=args.case,
            smoke=args.smoke,
            allow_live=args.live,
            baseline_path=baseline,
            project_root=Path.cwd(),
        )
    except CaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: eval run failed: {exc}", file=sys.stderr)
        return 1

    gates = outcome.report.gates
    gate_failed = gates is not None and not gates.passed

    if args.json:
        from code_triage_agent.background.replay import summarize

        print(json.dumps(summarize(outcome.report), indent=2, sort_keys=True))
    else:
        metrics = outcome.report.metrics
        print(f"OK: {metrics.correct_cases}/{metrics.total_cases} correct")
        print(f"  composite={metrics.composite_score:.4f} accuracy={metrics.accuracy:.2%}")
        print(f"  report={outcome.markdown_path}")

    if gate_failed:
        assert gates is not None
        for failure in gates.failures:
            print(f"  GATE FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


def _eval_report(args: argparse.Namespace) -> int:
    import json

    from code_triage_agent.background.metrics import render_markdown
    from code_triage_agent.background.replay import load_baseline, summarize

    try:
        report = load_baseline(args.report)
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read report: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summarize(report), indent=2, sort_keys=True))
    else:
        print(render_markdown(report), end="")
    return 0


def _eval_gates(args: argparse.Namespace) -> int:
    import json

    from code_triage_agent.background.metrics import evaluate_gates
    from code_triage_agent.background.replay import load_baseline

    try:
        report = load_baseline(args.report)
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read report: {exc}", file=sys.stderr)
        return 1

    budget = args.budget_usd if args.budget_usd is not None else report.budget_usd
    gates = evaluate_gates(report, baseline=baseline.metrics, budget_usd=budget)
    if args.json:
        print(json.dumps(gates.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(f"passed: {'yes' if gates.passed else 'no'}")
        for name, passed in gates.checks.items():
            print(f"  {name}: {'pass' if passed else 'FAIL'}")
        for failure in gates.failures:
            print(f"  FAILED: {failure}")
    return 0 if gates.passed else 1


def _worker(settings: Settings, args: argparse.Namespace) -> int:
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    from code_triage_agent.jobs.processor import build_collect_processor
    from code_triage_agent.jobs.worker import Worker

    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()
    try:
        processor = build_collect_processor(settings, config, database)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    worker = Worker(
        database=database,
        processor=processor,
        poll_interval=args.poll_interval,
    )
    try:
        if args.once:
            processed = worker.run_once()
            print(f"OK: processed={processed}")
        else:
            worker.run_forever()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        processor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
