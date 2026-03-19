from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from toolang import __version__
from toolang.errors import ToolangError
from toolang.parser import parse_program
from toolang.runtime import execute_thunk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolang", description="Toolang CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Parse a .too file and execute a thunk")
    _add_program_argument(run_parser)
    run_parser.add_argument("--thunk", help="Thunk name to run")
    run_parser.add_argument("--input", help="User input for a thunk(user) entrypoint")
    run_parser.add_argument("--model", help="Override model selection")
    run_parser.set_defaults(handler=_handle_run)

    check_parser = subparsers.add_parser("check", help="Validate a .too file")
    _add_program_argument(check_parser)
    check_parser.set_defaults(handler=_handle_check)

    dump_ast_parser = subparsers.add_parser("dump-ast", help="Print parsed AST as JSON")
    _add_program_argument(dump_ast_parser)
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
    program_path = _resolve_program_path(args.program)
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
    program_path = _resolve_program_path(args.program)
    parse_program(program_path.read_text(encoding="utf-8"))
    print("ok")
    return 0


def _handle_dump_ast(args: argparse.Namespace) -> int:
    program_path = _resolve_program_path(args.program)
    program = parse_program(program_path.read_text(encoding="utf-8"))
    print(json.dumps(program.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _resolve_program_path(raw_path: str) -> Path:
    program_path = Path(raw_path).resolve()
    if not program_path.exists():
        raise FileNotFoundError(f"Program not found: {program_path}")
    return program_path


def _add_program_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("program", help="Path to a Toolang .too file")


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
