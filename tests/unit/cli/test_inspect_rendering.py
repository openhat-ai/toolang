from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from toolang.base.types.message import ImagePart, ReasoningPart, TextPart
from toolang.cli.toolang.commands.inspect import (
    _HumanValue,
    _human_type_label,
    _implicit_pointer_projector,
    _print_human_table,
    _render_human_rows,
)
from toolang.execution.records import ThreadPeer, ThreadRecord
from toolang.execution.schemas import RecordSelection
from toolang.execution.types import Local, Pointer
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
    pointer = f"run_{'x' * 80}/output/local/value"

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


@pytest.mark.parametrize("terminal", (True, False))
def test_output_json_rich_highlighting_preserves_long_values(terminal: bool) -> None:
    import json
    from rich.text import Text
    from toolang.cli.toolang.commands.inspect import _print_output_json

    stream = StringIO()
    console = Console(
        file=stream,
        width=12,
        force_terminal=terminal,
        no_color=False,
        color_system="standard" if terminal else None,
    )
    value = {"long field": "中文 " * 100, "nested": [False, None, 12]}
    _print_output_json(console, value)
    rendered = stream.getvalue()
    assert ("\x1b[" in rendered) is terminal
    assert json.loads(Text.from_ansi(rendered).plain) == value
    assert "中文 " * 100 in Text.from_ansi(rendered).plain


@pytest.mark.parametrize(
    ("local", "expected"),
    (
        (Local("# Heading\n\n**bold**"), True),
        (Local(""), True),
        (Local.typed("Part[]", ()), True),
        (Local.typed("TextPart", TextPart("")), True),
        (Local.typed("ReasoningPart", ReasoningPart("reasoning")), False),
        (
            Local.typed(
                "Part[]", (ImagePart(image_url="https://example.com/image.png"),)
            ),
            False,
        ),
        (Local.typed("Json", {}), False),
    ),
)
def test_output_markdown_accepts_only_textual_content(
    local: Local, expected: bool
) -> None:
    from toolang.cli.toolang.commands.inspect import _run_output_text

    assert (_run_output_text(local, markdown=True) is not None) is expected
