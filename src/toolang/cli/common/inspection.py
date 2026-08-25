"""Inspect argument decisions shared by CLI routing and command execution."""

from __future__ import annotations

from collections.abc import Sequence


_VALUE_OPTIONS = {
    "--focus",
    "--input",
    "--arg",
    "--allow",
    "--default",
}
_FLAG_OPTIONS = {"--json", "--full", "--thread"}


def inspect_args_require_program(arguments: Sequence[str]) -> bool:
    """Return whether inspect arguments select a prospective model focus."""

    focus: str | None = None
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("--focus="):
            focus = argument.partition("=")[2]
        elif argument in _VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                return False
            if argument == "--focus":
                focus = arguments[index + 1]
            index += 1
        elif argument in _FLAG_OPTIONS:
            pass
        elif argument.startswith("-"):
            return False
        else:
            positional.append(argument)
        index += 1
    return not positional and focus in {"model_call", "model_request"}
