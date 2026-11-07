"""Command-line entrypoints."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

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
