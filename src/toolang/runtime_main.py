"""Slim runtime entrypoint for managed agent processes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import os
import sys

from .config.log import configure_logging
from .config.log_spec import PY_LOG_ENV_VAR
from . import up as agent_up


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    effective_log_spec = args.log if args.log is not None else os.environ.get(PY_LOG_ENV_VAR)
    try:
        configure_logging(spec=args.log, environ=os.environ)
    except ValueError as exc:
        print(f"toolang error: {exc}", file=sys.stderr)
        return 1
    try:
        return agent_up.up(
            toolang_root=Path(args.root),
            agent_name=args.agent,
            host=args.host,
            public_host=args.public_host,
            port=args.port,
            sandbox=args.sandbox,
            models=args.model or None,
            dev=Path(args.dev) if args.dev is not None else None,
            sandbox_child=args.sandbox_child,
            loop_names=args.loop or None,
            log_spec=effective_log_spec,
            environ=os.environ,
        )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"toolang error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolang-runtime",
        description="Run one Toolang agent runtime.",
    )
    parser.add_argument("--root", required=True, help="Root directory for all agents.")
    parser.add_argument("--agent", required=True, help="Agent name.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--public-host", help="Published host name.")
    parser.add_argument("--port", type=int, help="Port to listen on.")
    parser.add_argument("--sandbox", help="Sandbox to use: none or <driver>[:target].")
    parser.add_argument("--model", action="append", default=[], help="Allowed model selector.")
    parser.add_argument("--loop", action="append", default=[], help="Runtime loop to enable.")
    parser.add_argument(
        "--dev",
        help="Wheel file, or a directory tree containing wheels, for managed sandbox startup.",
    )
    parser.add_argument("--sandbox-child", action="store_true", help="Run as one sandbox child process.")
    parser.add_argument("--log", help="Set logging directives. Uses PY_LOG when omitted.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
