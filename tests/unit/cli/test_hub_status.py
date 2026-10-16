"""Hub instance snapshot layout, colors, wrapping, and terminal fitting."""

import io
import re

import pytest
from rich.console import Console

from toolang.common.layout import AgentPlacement
from toolang.cli.toolang.commands.hub_status import (
    HubSnapshot,
    _columns,
    _wrap,
    render_hub_snapshot,
)
from toolang.up.observation import RuntimeInstance, RuntimeInstanceSnapshot


def instance(
    name: str,
    *,
    identity: str | None = None,
    state: str = "running",
    drivers: tuple[str, ...] | None = ("chat", "talk"),
    diagnostic: str | None = None,
    observed_state: str | None = None,
    placement: AgentPlacement = "resident",
    runtime_instance: str | None = None,
) -> RuntimeInstance:
    return RuntimeInstance(
        name,
        placement,
        runtime_instance,
        identity or (name.encode().hex()[:16].ljust(16, "0")),
        state,
        drivers,
        observed_state,
        diagnostic,
    )


def render(snapshot, *, width=120, height=40, terminal=False):
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=width,
        height=height,
        force_terminal=terminal,
        color_system="standard" if terminal else None,
    )
    render_hub_snapshot(snapshot, console)
    return stream.getvalue()


def test_snapshot_matches_compact_tree_and_counts():
    agents = RuntimeInstanceSnapshot(
        (
            instance("alice"),
            instance("bob", drivers=("chat",)),
            instance("carol", state="not running", drivers=None),
            instance(
                "dave",
                state="unknown",
                drivers=None,
                observed_state="starting",
                diagnostic="observed starting",
            ),
        )
    )
    output = render(HubSnapshot("running", "v0.4.0a8", agents))
    assert output.startswith(
        "hub running · 4 agents · 2 running · 1 talk enabled · partial\n\n"
    )
    assert (
        "HUB" in output
        and "AGENT" in output
        and "STATUS" in output
        and "TALK" in output
    )
    assert "Hub" in output and "running · v0.4.0a8" in output
    assert "├─alice" in output
    assert "├╌bob" in output
    assert "not running" in output
    assert "-" in output
    assert "partial: dave: starting: observed starting" in output


def test_empty_snapshot_shows_no_agents_and_zero_counts():
    output = render(HubSnapshot("not running", None, RuntimeInstanceSnapshot(())))
    assert output.startswith(
        "hub not running · 0 agents · 0 running · 0 talk enabled\n\n"
    )
    assert "No agents" in output
    assert "Hub" in output and "not running" in output


@pytest.mark.parametrize("width", [120, 80, 40, 20])
def test_narrow_layout_wraps_without_losing_names_or_values(width):
    agents = RuntimeInstanceSnapshot(
        (
            instance(
                "alice" * 10,
                placement="visiting",
                identity="0123456789abcdef",
                state="running",
                drivers=("talk",),
            ),
            instance(
                "bob" * 8,
                placement="roaming",
                identity="fedcba9876543210",
                runtime_instance="temporary123",
                state="not running",
                drivers=None,
            ),
        )
    )
    output = render(HubSnapshot("unavailable", "v0.4.0a8", agents), width=width)
    if width >= 32:
        hub_width, agent_width, status_width, _ = _columns(width, 2)
        agent_start = hub_width + 2
        data_lines = output.splitlines()[3:]
        agent_columns = "".join(
            line[agent_start : agent_start + agent_width]
            for line in data_lines
            if len(line) >= agent_start
        ).replace(" ", "")
        status_start = agent_start + agent_width + 1
        status_columns = "".join(
            line[status_start : status_start + status_width]
            for line in data_lines
            if len(line) >= status_start
        ).replace(" ", "")
        assert (
            "alicealicealicealicealicealicealicealicealicealice@v01234567"
            in agent_columns
        )
        assert "bobbobbobbobbobbobbob@rfedcba98#temporary123" in agent_columns
    else:
        console = Console(width=width)
        for label in (agents.instances[0].label, agents.instances[1].label):
            for chunk in _wrap(console, label, width - 8):
                assert chunk in output
    assert "unavailable" in output
    if width >= 32:
        assert "notrunning" in status_columns
    else:
        assert "not running" in output
    assert "\x1b[" not in output


def test_running_row_is_normal_and_wrapped_continuation_is_dim():
    agents = RuntimeInstanceSnapshot(
        (
            instance(
                "alice",
                placement="visiting",
                identity="0123456789abcdef",
                drivers=("talk",),
            ),
        )
    )
    output = render(
        HubSnapshot("running", "v0.4.0a8", agents),
        width=40,
        height=30,
        terminal=True,
    )
    lines = output.splitlines()
    header = next(
        index
        for index, line in enumerate(lines)
        if re.sub(r"\x1b\[[0-9;]*m", "", line).startswith("HUB")
    )
    first_row, continuation = lines[header + 1 : header + 3]
    assert "└─" in first_row and "\x1b[2m" not in first_row
    assert "\x1b[2m" in continuation


def test_dumb_terminal_uses_ascii_branches_without_controls(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    agents = RuntimeInstanceSnapshot(
        (instance("enabled"), instance("disabled", drivers=("chat",)))
    )
    output = render(HubSnapshot("running", "unknown", agents))
    assert "+-enabled" in output
    assert "\\.disabled" in output
    assert "\x1b[" not in output


def test_ascii_encoding_escapes_unrepresentable_text_and_uses_ascii_tree():
    class ASCIIStream(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

    stream = ASCIIStream()
    console = Console(file=stream, width=80, color_system=None)
    snapshot = HubSnapshot(
        "running",
        "v0.4.0-é",
        RuntimeInstanceSnapshot(
            (
                instance(
                    "alice", state="unknown", drivers=None, diagnostic="probe — failed"
                ),
            )
        ),
    )
    render_hub_snapshot(snapshot, console)
    output = stream.getvalue()
    output.encode("ascii")
    assert "+.alice" in output or "\\.alice" in output
    assert r"\xe9" in output and r"\u2014" in output


def test_terminal_centering_is_limited_to_ten_rows_that_fit():
    for count in (0, 1, 6, 10):
        roster = RuntimeInstanceSnapshot(tuple(instance(f"a{i}") for i in range(count)))
        centered = render(
            HubSnapshot("running", None, roster), height=40, terminal=True
        )
        assert centered.splitlines()[2] == ""

    many = RuntimeInstanceSnapshot(tuple(instance(f"a{i}") for i in range(11)))
    top_aligned = render(HubSnapshot("running", None, many), height=40, terminal=True)
    clean_header = re.sub(r"\x1b\[[0-9;]*m", "", top_aligned.splitlines()[2])
    assert clean_header.startswith("HUB")

    small = RuntimeInstanceSnapshot(tuple(instance(f"a{i}") for i in range(2)))
    piped = render(HubSnapshot("running", None, small), height=24, terminal=False)
    assert piped.splitlines()[2].startswith("HUB")
    too_tall = render(HubSnapshot("running", None, small), height=3, terminal=True)
    assert re.sub(r"\x1b\[[0-9;]*m", "", too_tall.splitlines()[2]).startswith("HUB")


@pytest.mark.parametrize("count", [100, 300, 1000])
def test_large_roster_is_complete_and_deterministically_counted(count):
    agents = RuntimeInstanceSnapshot(
        tuple(instance(f"agent{i:04}") for i in range(count))
    )
    output = render(HubSnapshot("not running", None, agents), width=80)
    assert output.startswith(
        f"hub not running · {count} agents · {count} running · {count} talk enabled"
    )
    assert "agent0000" in output and f"agent{count - 1:04}" in output
    assert output.count("├─") + output.count("└─") == count
