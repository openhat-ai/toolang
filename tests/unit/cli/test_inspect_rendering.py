from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from toolang.base.types.message import TextPart
from toolang.cli.toolang.commands.inspect import (
    _HumanValue,
    _human_type_label,
    _implicit_pointer_projector,
    _print_human_table,
    _render_human_rows,
)
from toolang.execution.records import ThreadPeer, ThreadRecord
from toolang.execution.schemas import RecordSelection
from toolang.execution.types import ControlRef, Pointer
from toolang.lang.types import Array


def test_human_type_labels_use_nullable_suffix() -> None:
    assert _human_type_label("StepRef | None") == "StepRef?"
    assert _human_type_label("str | int | None") == "(str | int)?"
    assert _human_type_label("RunRecord") == "RunRecord"


@pytest.mark.parametrize(
    ("value", "runtime", "render_type", "expected"),
    (
        ({"name": "user"}, {"name": "user"}, "ThreadPeer", "fields"),
        (["value"], ["value"], "str[]", "fields"),
        ({}, {}, "Json", "value"),
        ([], [], "str[]", "value"),
        ([{"type": "text"}], [TextPart("value")], "Part[]", "value"),
        ("term_target", Pointer.parse("term_target"), "ThreadRecord", "value"),
    ),
)
def test_implicit_pointer_projector_preserves_existing_browsing_rules(
    value: object,
    runtime: object,
    render_type: str,
    expected: str,
) -> None:
    record = ThreadRecord(
        id="term_render",
        origin="test",
        peer=ThreadPeer(),
        created_by=ControlRef.for_thread("term_render", 0),
        head=ControlRef.for_thread("term_render", 0),
        created_at="",
        updated_at="",
    )
    selected = RecordSelection(
        pointer=Pointer.parse("term_render/peer"),
        record=record,
        value=value,
        runtime=runtime,
        annotation=object,
        type_name=render_type,
        render_type=render_type,
    )

    assert _implicit_pointer_projector(selected) == expected


def test_human_table_never_truncates_a_pointer_in_a_narrow_terminal() -> None:
    output = StringIO()
    console = Console(file=output, width=20, force_terminal=False)
    pointer = f"run_{'x' * 80}/output/value"

    _print_human_table(console, ((pointer, "Text", "complete"),))

    rendered = output.getvalue()
    rules = [
        line
        for line in rendered.splitlines()
        if line.strip() and set(line.strip()) == {"─"}
    ]
    assert len(rules) == 3
    assert pointer in rendered
    assert "POINTER" in rendered
    assert "TYPE" in rendered
    assert rendered.index("TYPE") < rendered.index("VALUE")


def test_human_multiline_cell_never_wraps_its_pointer() -> None:
    output = StringIO()
    console = Console(file=output, width=20, force_terminal=False)
    pointer = Pointer.parse(f"term_{'x' * 80}/peer")
    record = ThreadRecord(
        id="term_render",
        origin="test",
        peer=ThreadPeer(),
        created_by=ControlRef.for_thread("term_render", 0),
        head=ControlRef.for_thread("term_render", 0),
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

    rendered = output.getvalue()
    assert str(pointer) in rendered
    assert "Text" in rendered


def test_human_resolved_pointer_marks_the_type_not_the_field() -> None:
    output = StringIO()
    console = Console(file=output, width=80, force_terminal=False)
    base = Pointer.parse("term_render")
    pointer = base.select("peer")
    record = ThreadRecord(
        id="term_render",
        origin="test",
        peer=ThreadPeer(),
        created_by=ControlRef.for_thread("term_render", 0),
        head=ControlRef.for_thread("term_render", 0),
        created_at="",
        updated_at="",
    )
    selected = RecordSelection(
        pointer=pointer,
        record=record,
        value="resolved",
        runtime="resolved",
        annotation=str,
        type_name="Pointer",
        render_type="Text",
    )

    _render_human_rows(
        console,
        ((selected, _HumanValue("resolved", "resolved", "Text", True)),),
        base=base,
    )

    rendered = output.getvalue()
    row = next(line for line in rendered.splitlines() if "/peer" in line)
    assert "/peer" in row
    assert "*Text" in row
    assert "→" not in row


def test_human_parts_align_in_the_value_cell_without_a_bullet() -> None:
    output = StringIO()
    console = Console(file=output, width=80, force_terminal=False)
    base = Pointer.parse("term_render")
    pointer = base.select("peer")
    record = ThreadRecord(
        id="term_render",
        origin="test",
        peer=ThreadPeer(),
        created_by=ControlRef.for_thread("term_render", 0),
        head=ControlRef.for_thread("term_render", 0),
        created_at="",
        updated_at="",
    )
    selected = RecordSelection(
        pointer=pointer,
        record=record,
        value=[{"type": "text", "text": "first\n\nsecond"}],
        runtime=Array("Part[]", (TextPart("first\n\nsecond"),)),
        annotation=object,
        type_name="Part[]",
        render_type="Part[]",
    )

    _render_human_rows(
        console,
        ((selected, _HumanValue(selected.value, selected.runtime, "Part[]", False)),),
        base=base,
    )

    rendered = output.getvalue()
    first = next(line for line in rendered.splitlines() if "first" in line)
    second = next(line for line in rendered.splitlines() if "second" in line)
    assert first.index("first") == second.index("second")
    assert "•" not in rendered
