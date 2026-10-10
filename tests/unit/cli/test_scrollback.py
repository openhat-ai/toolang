"""Terminal padding and cursor recovery beneath native scrollback."""

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.styles import Style

from toolang.cli.common.scrollback import _PaddingOutput, _rewrapped_position


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.events = []

    def write(self, data):
        self.events.append(("text", data))

    def write_raw(self, data):
        self.events.append(("raw", data))

    def cursor_forward(self, amount):
        self.events.append(("forward", amount))


def test_padding_keeps_spaces_inside_text_and_erases_only_the_trailing_run():
    output = RecordingOutput()
    padding = _PaddingOutput(output)
    padding.write("hello")
    padding.write(" ")
    padding.write("world")
    padding.write(" " * 80)
    padding.write("\r\n")

    assert "".join(value for kind, value in output.events if kind == "text") == (
        "hello world\r\n"
    )
    assert ("raw", "\x1b[80X") in output.events
    assert ("forward", 80) in output.events


@pytest.mark.parametrize("style", ["underline", "reverse", "strike", "dim"])
def test_padding_preserves_attributes_that_erase_characters_cannot_paint(style):
    output = RecordingOutput()
    padding = _PaddingOutput(output)
    attrs = Style.from_dict({"padding": style}).get_attrs_for_style_str("class:padding")
    padding.set_attributes(attrs, ColorDepth.TRUE_COLOR)
    padding.write("   ")
    padding.reset_attributes()
    padding.write("\r\n")

    assert output.events == [("text", "   "), ("text", "\r\n")]


@pytest.mark.parametrize(
    "offset,expected",
    [(0, Point(0, 0)), (2, Point(0, 1)), (4, Point(2, 1)), (80, Point(2, 1))],
)
def test_cursor_reflow_keeps_wide_characters_whole_and_ignores_erased_tail(
    offset, expected
):
    row = {0: Char("中"), 1: Char(""), 2: Char("文"), 3: Char("")}
    assert _rewrapped_position(row, used=4, columns=3, offset=offset) == expected
