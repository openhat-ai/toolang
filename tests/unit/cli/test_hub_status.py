"""Hub availability and three-column agent tree rendering."""

import io
import re

import pytest
from rich.cells import cell_len
from rich.console import Console

from toolang.common.layout import AgentPlacement
from toolang.cli.toolang.commands.hub_status import HubSnapshot, render_hub_snapshot
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


def plain(output: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


def test_snapshot_shows_hub_then_three_agent_columns_and_bottom_summary():
    roster = RuntimeInstanceSnapshot(
        (
            instance("gus"),
            instance("merkle", drivers=("chat",)),
            instance("hak", state="not running", drivers=None),
            instance("alice", state="not running", drivers=None),
            instance("bob", state="not running", drivers=None),
        )
    )
    output = render(HubSnapshot("running", "v0.3.4", roster))
    lines = output.splitlines()

    assert lines[0] == "hub v0.3.4"
    assert [line.split()[0] for line in lines[1:6]] == [
        "├──",
        "├──",
        "├──",
        "├──",
        "└──",
    ]
    assert lines[1].startswith("├── gus")
    assert lines[2].startswith("├── merkle")
    assert lines[5].startswith("└── bob")
    assert len({line.index("running") for line in lines[1:3]}) == 1
    assert lines[6] == ""
    assert lines[7] == "5 agents, 2 running, 1 talk enabled"
    assert "STATUS" not in output and "TALK" not in output


def test_first_column_is_roomy_and_last_column_is_right_aligned():
    roster = RuntimeInstanceSnapshot(
        (instance("gus"), instance("merkle", drivers=("chat",)))
    )
    output = render(HubSnapshot("running", "v0.3.4", roster), width=160)
    first, last = output.splitlines()[1:3]

    assert first.index("running") >= 26
    assert first.endswith("enabled")
    assert last.endswith("disabled")
    assert first.index("enabled") == last.index("disabled") + 1
    assert len(first) == len(last) < 160


@pytest.mark.parametrize(
    ("state", "version", "expected"),
    [
        ("running", "v0.3.4", "hub v0.3.4"),
        ("running", None, "hub running"),
        ("not running", None, "hub not running"),
        ("unavailable", "v0.3.4", "hub not running"),
        ("starting", None, "hub not running"),
    ],
)
def test_hub_line_uses_version_online_and_not_running_offline(state, version, expected):
    output = render(HubSnapshot(state, version, RuntimeInstanceSnapshot(())))

    assert output.startswith(expected + "\n")
    assert output.count("hub not running") == (1 if state != "running" else 0)
    assert (
        "\x1b[2m"
        not in render(
            HubSnapshot(state, version, RuntimeInstanceSnapshot(())), terminal=True
        ).splitlines()[0]
    )


def test_negative_states_are_dim_without_dimming_agent_names_or_positive_states():
    roster = RuntimeInstanceSnapshot(
        (
            instance("gus", drivers=("talk",)),
            instance("merkle", drivers=("chat",)),
            instance("hak", state="not running", drivers=None),
        )
    )
    output = render(HubSnapshot("running", "v0.3.4", roster), terminal=True)
    lines = output.splitlines()
    gus = next(line for line in lines if "gus" in plain(line))
    merkle = next(line for line in lines if "merkle" in plain(line))
    hak = next(line for line in lines if "hak" in plain(line))

    assert "\x1b[2m" not in lines[0]
    assert "\x1b[2mrunning\x1b[0m" not in gus
    assert "\x1b[2menabled\x1b[0m" not in gus
    assert "\x1b[2mrunning\x1b[0m" not in merkle
    assert "\x1b[2mdisabled\x1b[0m" in merkle
    assert "\x1b[2mnot running\x1b[0m" in hak
    assert "\x1b[2m-\x1b[0m" in hak
    assert "\x1b[2m└── hak" not in hak


def test_empty_roster_omits_summary_and_keeps_partial_diagnostics():
    snapshot = HubSnapshot(
        "not running",
        None,
        RuntimeInstanceSnapshot((), ("agent discovery failed",)),
        "record inspection failed",
    )
    output = render(snapshot)
    lines = output.splitlines()

    assert lines[0] == "hub not running"
    assert lines[1] == "no agents"
    assert "0 agents, 0 running, 0 talk enabled" not in output
    assert lines[2] == "partial: agent discovery failed"
    assert "partial: agent discovery failed" in output
    assert "partial: Hub: record inspection failed" in output


def test_width_is_content_sized_and_narrow_terminals_wrap_all_three_fields():
    label = "alice" * 8
    roster = RuntimeInstanceSnapshot(
        (instance(label, state="not running", drivers=None),)
    )
    wide = render(HubSnapshot("running", "v0.3.4", roster), width=160)
    wide_rows = wide.splitlines()[1:]
    assert max(map(len, wide_rows)) < 160

    narrow = render(HubSnapshot("running", "v0.3.4", roster), width=20)
    narrow_lines = plain(narrow).splitlines()
    agent_rows = [
        line
        for line in narrow_lines
        if line.startswith(("├── ", "└── ", "│   ", "    "))
    ]
    assert "".join(line[4:9] for line in agent_rows) == label
    assert "".join(line[11:15] for line in agent_rows).replace(" ", "") == "notrunning"
    assert "-" in plain(narrow)
    assert "\x1b[" not in narrow


@pytest.mark.parametrize("width", [20, 32, 40, 80, 160])
def test_wide_character_names_keep_columns_within_terminal(width):
    roster = RuntimeInstanceSnapshot(
        (
            instance("中文😀" * 8, drivers=("chat",)),
            instance("bob", state="not running", drivers=None),
        )
    )
    lines = render(HubSnapshot("running", "v0.3.4", roster), width=width).splitlines()
    tree = lines[1 : lines.index("")]

    assert tree[0].startswith("├── 中文")
    assert any(line.startswith("└── bob") for line in tree)
    assert all(cell_len(line) <= width for line in tree)


@pytest.mark.parametrize("count", [100, 300, 1000])
def test_large_roster_is_complete_and_deterministically_counted(count):
    roster = RuntimeInstanceSnapshot(
        tuple(instance(f"agent{i:04}") for i in range(count))
    )
    output = render(HubSnapshot("running", "v0.3.4", roster), width=80)

    assert output.startswith("hub v0.3.4\n")
    assert f"{count} agents, {count} running, {count} talk enabled" in output
    assert "agent0000" in output and f"agent{count - 1:04}" in output
    assert output.count("├──") + output.count("└──") == count


def test_dumb_terminal_uses_ascii_branches_without_controls(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    roster = RuntimeInstanceSnapshot((instance("gus"), instance("bob")))
    output = render(HubSnapshot("running", "v0.3.4", roster))

    assert "+-- gus" in output
    assert r"\-- bob" in output
    assert "\x1b[" not in output


def test_ascii_encoding_escapes_unrepresentable_text_and_uses_ascii_tree():
    class ASCIIStream(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

    stream = ASCIIStream()
    console = Console(file=stream, width=80, color_system=None)
    roster = RuntimeInstanceSnapshot((instance("alice-é"),))
    render_hub_snapshot(HubSnapshot("running", "v0.3.4-é", roster), console)
    output = stream.getvalue()

    output.encode("ascii")
    assert r"\xe9" in output
    assert r"\-- alice" in output
