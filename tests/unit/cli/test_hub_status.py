"""Hub availability and three-column agent tree rendering."""

import io
import re

import pytest
from rich.cells import cell_len
from rich.color import Color
from rich.console import Console
from rich.text import Text

from toolang.common.layout import AgentPlacement
from toolang.cli.toolang.commands.hub_status import HubSnapshot, render_hub_snapshot
from toolang.up.observation import RuntimeInstance, RuntimeInstanceSnapshot


@pytest.fixture(autouse=True)
def rendering_terminal(monkeypatch):
    """Keep this module's rendering inputs independent of the caller's terminal."""
    monkeypatch.setenv("TERM", "xterm-256color")
    for variable in ("NO_COLOR", "FORCE_COLOR", "COLORTERM"):
        monkeypatch.delenv(variable, raising=False)


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

    assert lines[0].split() == ["hub", "running", "v0.3.4"]
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
    assert len({line.index("running") for line in lines[:3]}) == 1
    assert lines[6] == ""
    assert lines[7] == "5 agents, 2 running, 1 talk enabled"
    assert "STATUS" not in output and "TALK" not in output


@pytest.mark.parametrize("width", [80, 120, 160])
def test_first_column_is_roomy_and_last_column_is_right_aligned(width):
    roster = RuntimeInstanceSnapshot(
        (instance("gus"), instance("merkle", drivers=("chat",)))
    )
    output = render(HubSnapshot("running", "v0.3.4", roster), width=width)
    hub, first, last = output.splitlines()[:3]

    assert hub.index("running") == first.index("running") == last.index("running") == 34
    assert first.endswith("enabled")
    assert last.endswith("disabled")
    assert first.index("enabled") == last.index("disabled") + 1
    assert len(hub) == len(first) == len(last) < width


def test_long_names_and_versions_expand_columns_when_space_permits():
    name = "alexandria" * 6
    version = "v0.4.0a11-" + "12345678" * 4 + "*"
    roster = RuntimeInstanceSnapshot((instance(name),))
    hub, agent = render(
        HubSnapshot("running", version, roster), width=160
    ).splitlines()[:2]

    assert name in agent and version in hub
    assert hub.index("running") == agent.index("running") == cell_len(name) + 6
    assert len(hub) == len(agent) < 160


@pytest.mark.parametrize(
    ("state", "version", "expected"),
    [
        ("running", "v0.3.4", "hub running v0.3.4"),
        ("running", None, "hub running unknown"),
        ("running", "unknown", "hub running unknown"),
        ("not running", None, "hub not running"),
        ("unavailable", "v0.3.4", "hub not running"),
        ("starting", None, "hub not running"),
        ("-", None, "hub not running"),
    ],
)
def test_hub_row_shows_aligned_status_and_version_or_empty_field(
    state, version, expected
):
    output = render(HubSnapshot(state, version, RuntimeInstanceSnapshot(())))

    assert output.splitlines()[0].split() == expected.split()
    assert (
        output.splitlines()[0].index("running" if state == "running" else "not") == 34
    )
    row = Text.from_ansi(
        render(
            HubSnapshot(state, version, RuntimeInstanceSnapshot(())), terminal=True
        ).splitlines()[0]
    )
    console = Console()
    hub_style = row.get_style_at_offset(console, 0)
    assert hub_style.color is not None
    assert hub_style.color.number == Color.parse("cyan").number
    assert not hub_style.dim
    for token in expected.split()[1:]:
        offset = row.plain.index(token)
        is_negative = state != "running" or token == "unknown"
        assert bool(row.get_style_at_offset(console, offset).dim) == is_negative


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


@pytest.mark.parametrize(
    ("placement", "color"),
    [
        ("resident", "bright_cyan"),
        ("roaming", "bright_magenta"),
        ("visiting", "bright_yellow"),
    ],
)
@pytest.mark.parametrize("canonical_identity", [False, True])
@pytest.mark.parametrize("state", ["running", "not running"])
@pytest.mark.parametrize("width", [20, 40, 120])
def test_agent_name_colors_and_default_dim_ids_survive_wrapping(
    placement, color, canonical_identity, state, width
):
    marker = "v" if placement == "visiting" else "r"
    identity = (
        f"alexandre@{marker}abcd1234deadbeef" if canonical_identity else "abcd1234"
    )
    agent = instance(
        "alexandre",
        placement=placement,
        identity=identity,
        runtime_instance="work",
        state=state,
    )
    output = render(
        HubSnapshot("running", "v0.3.4", RuntimeInstanceSnapshot((agent,))),
        width=width,
        terminal=True,
    )
    rows = Text.from_ansi(output).split("\n")
    agent_start = next(i for i, row in enumerate(rows) if row.plain.startswith("└──"))
    agent_end = next(i for i in range(agent_start, len(rows)) if not rows[i].plain)
    names, suffixes = [], []
    console = Console()
    name_color = Color.parse(color).number
    for row in rows[agent_start:agent_end]:
        assert row.cell_len <= width
        for offset, character in enumerate(row.plain):
            style = row.get_style_at_offset(console, offset)
            if style.color is not None and style.color.number == name_color:
                names.append(character)
                assert not style.dim and not style.bold
            elif style.color is not None and style.color.is_default:
                suffixes.append(character)
                assert style.dim and not style.bold
    assert "".join(names) == agent.name
    assert "".join(suffixes) == agent.label[len(agent.name) :]


@pytest.mark.parametrize("width", [20, 32, 40, 80, 120, 160])
@pytest.mark.parametrize("has_agents", [False, True])
def test_long_hub_versions_wrap_without_losing_values_or_column_alignment(
    width, has_agents
):
    version = "v0.4.0a11-" + "1234567890abcdef" * 8 + "*"
    roster = RuntimeInstanceSnapshot((instance("gus"),) if has_agents else ())
    output = render(HubSnapshot("running", version, roster), width=width)
    lines = output.splitlines()
    hub_end = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("└──") or line == "no agents"
    )
    hub_rows = lines[:hub_end]
    status_start = hub_rows[0].index("r")
    version_start = hub_rows[0].index("v")
    assert "".join(row[version_start:].strip() for row in hub_rows) == version
    assert (
        "".join(row[status_start:version_start].strip() for row in hub_rows)
        == "running"
    )
    assert all(cell_len(row) <= width for row in hub_rows)
    if has_agents:
        assert lines[hub_end].index("r") == status_start


@pytest.mark.parametrize("mode", ["pipe", "no_color", "dumb"])
def test_plain_output_keeps_values_without_ansi_controls(monkeypatch, mode):
    if mode == "no_color":
        monkeypatch.setenv("NO_COLOR", "1")
    elif mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    roster = RuntimeInstanceSnapshot(
        (
            instance("alex", placement="roaming", identity="abcd1234"),
            instance(
                "bob", placement="visiting", identity="deadbeef", drivers=("chat",)
            ),
            instance("gus", state="not running", drivers=None),
        )
    )
    output = render(
        HubSnapshot("not running", None, roster), terminal=mode == "no_color"
    )
    assert "\x1b" not in output
    assert "alex@rabcd1234" in output
    assert "bob@vdeadbeef" in output
    assert "not running" in output and "disabled" in output


def test_empty_roster_omits_summary_and_keeps_partial_diagnostics():
    snapshot = HubSnapshot(
        "not running",
        None,
        RuntimeInstanceSnapshot((), ("agent discovery failed",)),
        "record inspection failed",
    )
    output = render(snapshot)
    lines = output.splitlines()

    assert lines[0].split() == ["hub", "not", "running"]
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
    agent_start = next(
        i for i, line in enumerate(narrow_lines) if line.startswith("└──")
    )
    agent_rows = narrow_lines[agent_start : narrow_lines.index("")]
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
    agent_start = next(i for i, line in enumerate(lines) if line.startswith("├──"))
    tree = lines[agent_start : lines.index("")]

    assert tree[0].startswith("├── 中文")
    assert any(line.startswith("└── bob") for line in tree)
    assert all(cell_len(line) <= width for line in tree)


@pytest.mark.parametrize("count", [100, 300, 1000])
def test_large_roster_is_complete_and_deterministically_counted(count):
    roster = RuntimeInstanceSnapshot(
        tuple(instance(f"agent{i:04}") for i in range(count))
    )
    output = render(HubSnapshot("running", "v0.3.4", roster), width=80)

    assert output.splitlines()[0].split() == ["hub", "running", "v0.3.4"]
    assert f"{count} agents, {count} running, {count} talk enabled" in output
    assert "agent0000" in output and f"agent{count - 1:04}" in output
    assert output.count("├──") + output.count("└──") == count


@pytest.mark.parametrize(
    ("term", "first_branch", "last_branch"),
    [("xterm-256color", "├──", "└──"), ("dumb", "+--", r"\--")],
)
def test_terminal_type_selects_tree_branches_without_controls(
    monkeypatch, term, first_branch, last_branch
):
    monkeypatch.setenv("TERM", term)
    roster = RuntimeInstanceSnapshot((instance("gus"), instance("bob")))
    output = render(HubSnapshot("running", "v0.3.4", roster))

    assert output.splitlines()[1].startswith(f"{first_branch} gus")
    assert output.splitlines()[2].startswith(f"{last_branch} bob")
    assert "\x1b[" not in output


@pytest.mark.parametrize("width", [20, 32, 80])
def test_ascii_encoding_escapes_unrepresentable_text_and_uses_ascii_tree(width):
    class ASCIIStream(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

    stream = ASCIIStream()
    console = Console(file=stream, width=width, color_system=None)
    roster = RuntimeInstanceSnapshot((instance("alice-é"),))
    render_hub_snapshot(HubSnapshot("running", "v0.3.4-é", roster), console)
    output = stream.getvalue()

    output.encode("ascii")
    assert r"\xe9" in output
    assert r"\-- alice" in output
