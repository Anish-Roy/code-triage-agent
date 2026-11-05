"""Command-line entrypoints."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from code_triage_agent import __version__
from code_triage_agent.config import ConfigError, load_config
from code_triage_agent.logging import configure_logging
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


if __name__ == "__main__":
    raise SystemExit(main())
