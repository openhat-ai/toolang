"""Compact resource tables use terminal cells and keep one line per record."""

from copy import deepcopy
from io import StringIO
import json
import os

import pytest
from rich.console import Console
from typer._click.utils import strip_ansi
from wcwidth import wcswidth

from toolang.cli.common import records as output
from toolang.cli.common.records import echo_compact_records


@pytest.mark.parametrize("width", [80, 120, 240])
@pytest.mark.parametrize("terminal", [False, True])
def test_resource_rows_stay_single_line_at_the_full_terminal_width(
    monkeypatch, width, terminal
):
    monkeypatch.setenv("COLUMNS", str(width))
    stream = StringIO()
    monkeypatch.setattr(
        output,
        "Console",
        lambda **kwargs: Console(
            file=stream,
            force_terminal=terminal,
            color_system="standard" if terminal else None,
            _environ={},
            **kwargs,
        ),
    )
    records = [
        {
            "cap": "skill/" + "开发e\u0301" * 24,
            "description": "(error) Review [bold]changes[/bold].\n" * 30,
            "location": "/project  folder/" + "nested/" * 30 + "SKILL.md",
            "tags": ["not_allowed", "local", "root", "authored"],
        },
        {"cap": "prompt/short", "description": None, "location": None, "tags": []},
    ]
    before = deepcopy(records)

    echo_compact_records(
        records,
        ("cap", "description", "location", "tags"),
        heading="CAP",
        json_=False,
    )

    rendered = strip_ansi(stream.getvalue())
    lines = rendered.splitlines()
    assert len(lines) == 4
    assert all(wcswidth(line) <= width for line in lines)
    assert wcswidth(lines[1]) == width
    assert set(lines[1]) == {"─"}
    assert not lines[0].startswith(" ")
    assert not lines[2].startswith(" ")
    assert records == before
    assert "not_allowed,local,root,authored" in lines[2]
    if width >= 240:
        assert "SKILL.md" in lines[2]


def test_compact_rows_use_fresh_width_and_unavailable_terminal_fallback(
    monkeypatch, capsys
):
    def no_terminal(*args):
        raise OSError("No terminal")

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(os, "get_terminal_size", no_terminal)
    record = {"ref": "shell/run", "description": "description " * 50, "tags": ["ready"]}
    widths = []
    for columns in (None, "40", "80", "240", "invalid", "0"):
        if columns is not None:
            monkeypatch.setenv("COLUMNS", columns)
        echo_compact_records(
            [record], ("ref", "description", "tags"), heading="TOOL", json_=False
        )
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 3
        assert lines[0].split() == ["TOOL", "DESCRIPTION", "TAGS"]
        assert lines[2].startswith("shell/run") and lines[2].endswith("ready")
        widths.append(wcswidth(lines[1]))
    assert widths == [120, 80, 80, 240, 120, 120]


def test_compact_output_sanitizes_controls_and_preserves_literal_markup(
    monkeypatch, capsys
):
    monkeypatch.setenv("COLUMNS", "240")
    record = {
        "cap": "skill/review\x1b[2J",
        "description": "[bold]Review[/bold]\n\t\x1b]0;bad title\x07changes.",
        "location": "/project  folder/skills/review/SKILL.md",
        "tags": ["ready"],
    }
    echo_compact_records(
        [record],
        ("cap", "description", "location", "tags"),
        heading="CAP",
        json_=False,
    )
    rendered = capsys.readouterr().out
    assert len(rendered.splitlines()) == 3
    assert "\x1b" not in rendered and "bad title" not in rendered
    assert "[bold]Review[/bold] changes." in rendered
    assert record["location"] in rendered


def test_compact_json_retains_complete_unmodified_records(capsys):
    record = {
        "ref": "shell/" + "x" * 150,
        "description": "Full description.\n" * 30,
        "location": "https://example.test/" + "long/" * 30,
        "tags": ["ready"],
        "parameters": ["command", "workdir"],
    }
    echo_compact_records(
        [record], ("ref", "description", "tags"), heading="TOOL", json_=True
    )
    assert json.loads(capsys.readouterr().out) == [record]
