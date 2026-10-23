"""Chore tables shrink prose before dropping optional columns."""

from copy import deepcopy
from dataclasses import replace
from io import StringIO

import pytest
from rich.console import Console
from typer._click.utils import strip_ansi
from wcwidth import wcswidth

from toolang.cli.common import output
from toolang.cli.toolang.commands.chore import _echo_chores
from toolang.driver.chore.schemas import ChoreInfo, ChoreRuntimeInfo, LastRunInfo


def _entry():
    return ChoreInfo(
        id="abcdefgh",
        name="cleanup",
        ref="chore/cleanup",
        title="Review repository changes and prepare the daily maintenance report",
        path="chores/cleanup.md",
        line=None,
        schedule="FREQ=DAILY;BYHOUR=9;BYMINUTE=30;BYSECOND=0",
        source_error=None,
        runtime=ChoreRuntimeInfo(
            thread_id="chore_abcdefgh",
            synced=False,
            status="pending",
            active_run_id=None,
            pending_at=None,
            next_scheduled_at="2026-10-22T09:30:00+00:00",
            last_run=LastRunInfo(
                id="run_previous",
                status="succeeded",
                started_at=None,
                finished_at=None,
                error=(
                    "Provider request failed: model credits exhausted; "
                    "replenish the configured account before retrying."
                ),
            ),
            scheduler_error=None,
            updated_at=None,
        ),
    )


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "width,headers",
    [
        (240, "CHORE TITLE STATUS LAST RUN NEXT RUN SCHEDULE ERROR LOCATION"),
        (152, "CHORE TITLE STATUS LAST RUN NEXT RUN SCHEDULE ERROR LOCATION"),
        (151, "CHORE STATUS LAST RUN NEXT RUN SCHEDULE ERROR LOCATION"),
        (138, "CHORE STATUS LAST RUN NEXT RUN SCHEDULE ERROR LOCATION"),
        (137, "CHORE STATUS NEXT RUN SCHEDULE ERROR LOCATION"),
        (80, "CHORE STATUS NEXT RUN SCHEDULE ERROR LOCATION"),
    ],
)
def test_column_omission_is_independent_of_display_order(
    monkeypatch, set_local_timezone, terminal, width, headers
):
    set_local_timezone("Asia/Shanghai")
    stream = StringIO()
    monkeypatch.setattr(
        output,
        "_TABLE_CONSOLE",
        Console(file=stream, width=4096, force_terminal=terminal, _environ={}),
    )
    entries = [_entry()]
    before = deepcopy(entries)

    _echo_chores(entries, width=width)

    lines = strip_ansi(stream.getvalue()).splitlines()
    assert len(lines) == 3
    assert " ".join(lines[0].split()) == headers
    assert lines[0].startswith("CHORE")
    assert lines[2].split()[0] == entries[0].runtime.thread_id
    assert set(lines[1]) == {"─"}
    assert max(map(wcswidth, lines)) == max(width, 112)
    assert "unsynced" in lines[2]
    assert "2026-10-22T17:30:00+08:00" in lines[2]
    assert entries[0].schedule in lines[2]
    assert ("succeeded" in lines[2]) == ("LAST RUN" in headers)
    assert entries == before


def test_prose_shrinks_before_locations_and_refits_after_omission(capsys):
    entry = _entry()
    _echo_chores([entry], width=400)
    natural_width = wcswidth(capsys.readouterr().out.splitlines()[1])
    for width, complete_error in (
        (natural_width - 1, True),
        (natural_width - (len(entry.title) - len("TITLE")) - 1, False),
    ):
        _echo_chores([entry], width=width)
        lines = capsys.readouterr().out.splitlines()
        assert wcswidth(lines[1]) == width
        assert entry.title not in lines[2]
        assert (entry.runtime.last_run.error in lines[2]) is complete_error
        assert lines[2].rstrip().endswith(entry.path)


def test_optional_columns_yield_before_diagnostics_become_header_sized(capsys):
    _echo_chores([_entry()], width=120)

    lines = capsys.readouterr().out.splitlines()
    assert "TITLE" not in lines[0] and "LAST RUN" not in lines[0]
    assert "Provider…" in lines[2]
    assert "chore…nup.md" in lines[2]
    assert wcswidth(lines[1]) == 120


@pytest.mark.parametrize("width", [80, 108, 112, 115, 119, 120])
def test_diagnostics_shrink_further_after_all_optional_columns_yield(capsys, width):
    _echo_chores([_entry()], width=width)

    lines = capsys.readouterr().out.splitlines()
    assert "TITLE" not in lines[0] and "LAST RUN" not in lines[0]
    assert max(map(wcswidth, lines)) == max(width, 112)
    assert "Prov" in lines[2]


def test_short_values_keep_all_columns_without_extra_minimum_padding(capsys):
    entry = _entry()
    entry = replace(
        entry,
        title="Clean",
        schedule=None,
        runtime=replace(
            entry.runtime, synced=True, next_scheduled_at=None, last_run=None
        ),
    )

    _echo_chores([entry], width=90)

    lines = capsys.readouterr().out.splitlines()
    assert "TITLE" in lines[0] and "LAST RUN" in lines[0]
    assert "Clean" in lines[2] and "…" not in lines[2]
    assert wcswidth(lines[1]) == 86


@pytest.mark.parametrize("identity", ["x" * 5000, "运维e\u0301👨‍👩‍👧‍👦" * 40])
def test_fixed_fields_and_complete_line_suffix_can_exceed_width(capsys, identity):
    entry = _entry()
    entry = replace(
        entry,
        id=None,
        name=identity,
        path="chores/" + "维护e\u0301/" * 30,
        line=123456789,
        source_error="invalid chore name",
        runtime=replace(entry.runtime, thread_id=None),
    )

    _echo_chores([entry], width=80)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[2].startswith(identity)
    assert entry.schedule in lines[2] and "unsynced" in lines[2]
    assert lines[2].rstrip().endswith(":123456789")
    assert wcswidth(lines[1]) > 80
    assert "TITLE" not in lines[0] and "LAST RUN" not in lines[0]


@pytest.mark.parametrize(
    "source,scheduler,run,expected",
    [
        ("source failure", "scheduler failure", "run failure", "source failure"),
        (None, "scheduler failure", "run failure", "scheduler failure"),
        (None, None, "run failure", "run failure"),
        (None, None, None, "-"),
    ],
)
def test_error_precedence_is_preserved(capsys, source, scheduler, run, expected):
    entry = _entry()
    entry = replace(
        entry,
        source_error=source,
        runtime=replace(
            entry.runtime,
            scheduler_error=scheduler,
            last_run=replace(entry.runtime.last_run, error=run),
        ),
    )

    _echo_chores([entry], width=400)

    lines = capsys.readouterr().out.splitlines()
    start, end = (lines[0].index(header) for header in ("ERROR", "LOCATION"))
    assert lines[2][start:end].strip() == expected


def test_display_controls_and_whitespace_are_safe_without_interpreting_markup(capsys):
    entry = _entry()
    entry = replace(
        entry,
        id=None,
        name="维护e\u0301👨‍👩‍👧‍👦\x1b[2J",
        title="[bold]Review[/bold]\n\tchanges.",
        path="chores/project  folder/维护e\u0301.md",
        source_error="\x1b]0;bad-title\x07Request\n\tfailed.",
        schedule=None,
        runtime=replace(entry.runtime, thread_id=None),
    )

    _echo_chores([entry], width=400)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[2].startswith("维护e\u0301👨‍👩‍👧‍👦")
    assert "[bold]Review[/bold] changes." in lines[2]
    assert "Request failed." in lines[2] and "once" in lines[2]
    assert entry.path in lines[2]
    assert "\x1b" not in "\n".join(lines) and "bad-title" not in lines[2]
