from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from toolang import __version__
from toolang.agent_refs import ResolvedAgentRef, resolve_agent_ref
from toolang.errors import ToolangError
from toolang.layout import resolve_toolang_root
from toolang.parser import parse_program
from toolang.runtime import execute_thunk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolang", description="Toolang CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Resolve an agent and execute a thunk")
    _add_agent_argument(run_parser)
    run_parser.add_argument("--thunk", help="Thunk name to run")
    run_parser.add_argument("--input", help="User input for a thunk(user) entrypoint")
    run_parser.add_argument("--model", help="Override model selection")
    run_parser.set_defaults(handler=_handle_run)

    check_parser = subparsers.add_parser("check", help="Validate a Toolang agent")
    _add_agent_argument(check_parser)
    check_parser.set_defaults(handler=_handle_check)

    dump_ast_parser = subparsers.add_parser("dump-ast", help="Print parsed AST as JSON")
    _add_agent_argument(dump_ast_parser)
    dump_ast_parser.set_defaults(handler=_handle_dump_ast)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (FileNotFoundError, ToolangError) as exc:
        print(f"toolang error: {exc}", file=sys.stderr)
        return 1


def _handle_run(args: argparse.Namespace) -> int:
    agent = _resolve_cli_agent(args.agent)
    program_path = _resolve_program_path(agent)
    program = parse_program(program_path.read_text(encoding="utf-8"))
    thunk = program.get_thunk(args.thunk)

    user_input = args.input
    if thunk.input_name and user_input is None and not sys.stdin.isatty():
        user_input = sys.stdin.read()

    result = execute_thunk(
        program,
        thunk,
        program_path,
        user_input=user_input,
        model=args.model,
    )
    print(result)
    return 0


def _handle_check(args: argparse.Namespace) -> int:
    program_path = _resolve_program_path(_resolve_cli_agent(args.agent))
    parse_program(program_path.read_text(encoding="utf-8"))
    print("ok")
    return 0


def _handle_dump_ast(args: argparse.Namespace) -> int:
    program_path = _resolve_program_path(_resolve_cli_agent(args.agent))
    program = parse_program(program_path.read_text(encoding="utf-8"))
    print(json.dumps(program.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _resolve_program_path(agent: ResolvedAgentRef) -> Path:
    program_path = agent.source_path
    if not program_path.exists():
        if agent.agent_kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.agent_uri} -> {program_path}"
            )
        raise FileNotFoundError(f"Agent source not found: {program_path}")
    return program_path


def _resolve_cli_agent(raw: str) -> ResolvedAgentRef:
    toolang_root = resolve_toolang_root(os.environ.get("TOOLANG_ROOT", "~/.toolang"))
    guest_base_url = os.environ.get("TOOLANG_GUEST_BASE_URL", "").strip()
    guest_resolver = None
    if guest_base_url:
        base = guest_base_url.rstrip("/")
        guest_resolver = lambda name: f"{base}/{name.lstrip('/')}"
    return resolve_agent_ref(
        raw,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=guest_resolver,
    )


def _add_agent_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("agent", help="Agent reference, path, or URI")


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
