from __future__ import annotations

from io import StringIO

from rich.console import Console

from toolang.cli.toolang.commands.thread import (
    _HumanValue,
    _print_human_table,
    _render_human_rows,
)
from toolang.execution.records import ThreadPeer, ThreadRecord
from toolang.execution.schemas import RecordSelection
from toolang.execution.types import ControlRef, Pointer


def test_human_table_never_truncates_a_pointer_in_a_narrow_terminal() -> None:
    output = StringIO()
    console = Console(file=output, width=20, force_terminal=False)
    pointer = f"run_{'x' * 80}/output/value"

    _print_human_table(console, ((pointer, '"complete"'),))

    rendered = output.getvalue()
    assert pointer in rendered


def test_human_block_never_wraps_its_pointer_heading() -> None:
    output = StringIO()
    console = Console(file=output, width=20, force_terminal=False)
    pointer = Pointer(f"term_{'x' * 80}/peer")
    record = ThreadRecord(
        thread_id="term_render",
        origin="test",
        peer=ThreadPeer(),
        created_by=ControlRef("term_render", 0),
        head=ControlRef("term_render", 0),
        created_at="",
        updated_at="",
    )
    selected = RecordSelection(
        pointer=pointer,
        record=record,
        value="first\nsecond",
        runtime="first\nsecond",
        annotation=str,
        type_name="str",
        render_type="Text",
    )

    _render_human_rows(
        console,
        ((selected, _HumanValue("first\nsecond", "first\nsecond", "Text", False)),),
    )

    assert str(pointer) in output.getvalue()
