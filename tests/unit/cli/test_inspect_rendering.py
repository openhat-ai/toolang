from __future__ import annotations

from io import StringIO

from rich.console import Console

from toolang.cli.toolang.commands.thread import _print_human_table


def test_human_table_never_truncates_a_pointer_in_a_narrow_terminal() -> None:
    output = StringIO()
    console = Console(file=output, width=20, force_terminal=False)
    pointer = f"run_{'x' * 80}/output/value"

    _print_human_table(console, ((pointer, '"complete"'),))

    rendered = output.getvalue()
    assert pointer in rendered
