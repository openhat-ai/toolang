"""Runtime listing scope, presentation, and client hub inspection."""

from dataclasses import replace
import io

import httpx
import pytest
from rich.console import Console
from rich.text import Text

from toolang.cli.common.output import runtime_status_label
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import ps
from toolang.common.layout import AgentLayout
from toolang.up.observation import RuntimeInventory, RuntimeSource
from toolang.up.types import AgentStatus, RuntimeInstance, RuntimeInstanceSnapshot
from toolang.up.workload import ForegroundWorkload


def inventory(tmp_path, *instances, complete=True, diagnostics=()):
    return RuntimeInventory(
        RuntimeInstanceSnapshot(tuple(instances), diagnostics, complete),
        tuple(
            RuntimeSource(
                item,
                AgentLayout(tmp_path, item.name, item.placement),
                "http://127.0.0.1:8123",
                None,
                None,
                AgentStatus(
                    item.name, item.state, "http://127.0.0.1:8123", None, None, "host"
                ),
            )
            for item in instances
        ),
    )


def render(
    snapshot, *, hub="hub http://127.0.0.1:7001, unavailable", width=120, terminal=False
):
    stream = io.StringIO()
    ps.render_snapshot(
        snapshot,
        hub_line=hub,
        console=Console(
            file=stream,
            width=width,
            force_terminal=terminal,
            color_system="standard" if terminal else None,
        ),
    )
    return stream.getvalue()


@pytest.fixture
def alice():
    return RuntimeInstance(
        "alice", "resident", None, "alice", "running", ("chat", "talk")
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("running", "up"),
        ("stopped", "down"),
        ("preparing", "down"),
        ("starting", "down"),
        ("failed", "down"),
        ("exited", "down"),
        ("not running", "down"),
        ("unknown", "unknown"),
        ("future", "unknown"),
    ],
)
def test_three_state_mapping(raw, expected):
    assert runtime_status_label(raw) == expected


def test_mixed_inventory_labels_drivers_ports_and_footer(tmp_path, alice):
    data = inventory(
        tmp_path,
        replace(alice, name="eve", identity="eve", state="unknown", drivers=None),
        alice,
        replace(alice, name="bob", identity="bob", state="stopped", drivers=None),
        replace(
            alice,
            placement="visiting",
            identity="alice@t0123456789#runtime",
            drivers=(),
        ),
        replace(
            alice,
            placement="roaming",
            identity="alice@s9876543210#runtime",
            drivers=("chore",),
        ),
    )
    data = replace(
        data,
        sources=tuple(
            replace(
                source,
                observation=replace(
                    source.observation,
                    sandbox={
                        "resident": "host",
                        "visiting": "docker:dev",
                        "roaming": "docker",
                    }[source.instance.placement],
                ),
            )
            for source in data.sources
        ),
    )
    text = render(data, hub="hub http://hub.example:7001, available, test")
    assert text.splitlines()[0].split() == [
        "AGENT",
        "STATUS",
        "SANDBOX",
        "DRIVERS",
        "PORT",
    ]
    assert "TYPE" not in text and "TALK" not in text and "├" not in text
    assert "alice@t0123456789" in text and "alice@s9876543210" in text
    assert "#runtime" not in text
    assert "chat,talk" in text and "none" in text and "chore" in text
    assert "8123" in text
    assert [line.split()[0] for line in text.splitlines()[2:7]] == [
        "alice",
        "alice@t0123456789",
        "alice@s9876543210",
        "bob",
        "eve",
    ]
    assert [line.split()[2] for line in text.splitlines()[2:7]] == [
        "host",
        "docker:dev",
        "docker",
        "-",
        "-",
    ]
    assert text.splitlines()[-2:] == [
        "5 agents, - running, 3 resident, 1 visiting, 1 roaming, partial",
        "hub http://hub.example:7001, available, test",
    ]
    assert data.snapshot.instances[0].state == "unknown"
    assert data.snapshot.instances[2].state == "stopped"


@pytest.mark.parametrize("command", ["ls", "ps"])
@pytest.mark.parametrize(
    "state,sandbox,expected",
    [
        ("running", "host", "host"),
        ("running", "docker", "docker"),
        ("running", "docker:dev", "docker:dev"),
        ("running", None, "-"),
        ("stopped", "docker:dev", "-"),
        ("starting", "docker:dev", "-"),
        ("unknown", "docker:dev", "-"),
    ],
)
def test_listings_show_observed_sandbox(
    tmp_path, monkeypatch, capsys, alice, command, state, sandbox, expected
):
    data = inventory(tmp_path, replace(alice, state=state))
    source = data.sources[0]
    assert source.observation is not None
    data = replace(
        data,
        sources=(
            replace(source, observation=replace(source.observation, sandbox=sandbox)),
        ),
    )
    monkeypatch.setattr(ps.RuntimeWatcher, "refresh", lambda _: data)
    monkeypatch.setattr(
        ps, "_hub_line", lambda _: "hub http://127.0.0.1:7001, unavailable"
    )

    assert cli.main(["--root", str(tmp_path), command]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["AGENT", "STATUS", "SANDBOX", "DRIVERS", "PORT"]
    assert lines[2].split() == [
        "alice",
        runtime_status_label(state),
        expected,
        "chat,talk" if state == "running" else "-",
        "8123",
    ]


@pytest.mark.parametrize(
    "state,drivers,complete,summary",
    [
        ("running", None, True, "1 agent, 1 running"),
        ("starting", None, True, "1 agent, 0 running"),
        ("unknown", None, True, "1 agent, - running"),
        ("running", (), False, "1 agent, - running"),
    ],
)
def test_counts_are_independent_of_driver_metadata(
    tmp_path, alice, state, drivers, complete, summary
):
    data = inventory(
        tmp_path, replace(alice, state=state, drivers=drivers), complete=complete
    )
    text = render(data)
    assert f"{summary}, 1 resident" in text
    assert (", partial" in text) is (not complete or state == "unknown")


def test_missing_port_and_partial_diagnostics_remain_visible(tmp_path, alice):
    data = inventory(
        tmp_path,
        replace(alice, diagnostic="drivers unavailable"),
        complete=False,
        diagnostics=("registry unreadable",),
    )
    data = replace(data, sources=())
    text = render(data)
    assert "1 agent, - running" in text and ", partial" in text
    assert "partial: registry unreadable" in text
    assert "partial: alice: drivers unavailable" in text
    assert "8123" not in text


def test_empty_lists_keep_their_own_summary(tmp_path):
    data = inventory(tmp_path)
    assert render(data) == "0 agents\nhub http://127.0.0.1:7001, unavailable\n"
    assert render(data, hub=None) == "0 agents\n"


@pytest.mark.parametrize("width", [20, 32, 40, 80, 120])
@pytest.mark.parametrize("terminal", [False, True])
def test_complete_addresses_survive_narrow_and_plain_output(
    tmp_path, alice, width, terminal
):
    address = "alice@t0123456789abcdefghjkmnpqrstvwxyz"
    data = inventory(tmp_path, replace(alice, placement="visiting", identity=address))
    text = render(data, width=width, terminal=terminal)
    plain = Text.from_ansi(text).plain
    lines = plain.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("a"))
    end = next(
        index
        for index in range(start, len(lines))
        if "─" in lines[index] and set(lines[index]) <= {"─", " "}
    )
    assert address == "".join(line.split()[0] for line in lines[start:end])
    assert "…" not in plain and "#" not in plain
    assert "─" in lines[start - 1]
    assert "\x1b" not in text


def test_ascii_stream_escapes_unicode_names(tmp_path, alice):
    class AsciiStream(io.StringIO):
        encoding = "ascii"

    stream = AsciiStream()
    ps.render_snapshot(
        inventory(tmp_path, replace(alice, diagnostic="/tmp/助手")),
        hub_line="hub http://127.0.0.1:7001, unavailable",
        console=Console(file=stream, width=80, color_system=None),
    )
    stream.getvalue().encode("ascii")
    assert "\\u52a9\\u624b" in stream.getvalue()


def test_ls_skips_hub_settings_and_network(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text('[hub]\nport = "bad"\n[teaming]\nhub = 42\n')
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    monkeypatch.setattr(
        ps, "_hub_line", lambda _: pytest.fail("ls must not inspect hub")
    )
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset({"talk"})):
        assert cli.main(["--root", str(tmp_path), "ls"]) == 0
    text = capsys.readouterr().out
    assert "up" in text and "talk" in text
    assert text.splitlines()[-1] == "1 agent, 1 running, 1 resident"
    assert "hub" not in text and "visiting" not in text and "roaming" not in text


def test_ls_filters_nonresidents_before_probing(tmp_path, monkeypatch, capsys):
    from toolang.up.allocation import allocate_roaming
    from toolang.up.observation import RuntimeProbe

    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    source = tmp_path / "worker.too"
    source.write_text("flow main():\n  pass\n")
    roaming = allocate_roaming(source, toolang_root=tmp_path, register=False)
    monkeypatch.setattr(
        ps, "_hub_line", lambda _: "hub http://127.0.0.1:7001, unavailable"
    )
    with (
        ForegroundWorkload(resident, toolang_root=tmp_path, drivers=frozenset()),
        ForegroundWorkload(roaming, toolang_root=tmp_path, drivers=frozenset()),
    ):
        assert cli.main(["--root", str(tmp_path), "ps"]) == 0
        text = capsys.readouterr().out
        assert roaming.address in text
        assert "2 agents, 2 running, 1 resident, 1 roaming" in text
        original = RuntimeProbe.status

        def inspect(self, **kwargs):
            assert self.layout.placement == "resident"
            return original(self, **kwargs)

        monkeypatch.setattr(RuntimeProbe, "status", inspect)
        assert cli.main(["--root", str(tmp_path), "ls"]) == 0
        text = capsys.readouterr().out
        assert "worker" not in text
        assert text.splitlines()[-1] == "1 agent, 1 running, 1 resident"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("healthy", "hub http://hub.example:7123, available, server"),
        ("offline", "hub http://hub.example:7123, unavailable"),
        ("unready", "hub http://hub.example:7123, unavailable"),
        ("health_false", "hub http://hub.example:7123, unavailable"),
        ("health_number", "hub http://hub.example:7123, error, invalid response"),
        ("health_extra", "hub http://hub.example:7123, error, invalid response"),
        ("health_json", "hub http://hub.example:7123, error, invalid response"),
        ("metadata", "hub http://hub.example:7123, error, invalid response"),
        ("invalid_json", "hub http://hub.example:7123, error, invalid response"),
    ],
)
def test_ps_probes_only_configured_hub(tmp_path, monkeypatch, capsys, mode, expected):
    (tmp_path / "config.toml").write_text(
        '[teaming]\nhub = "http://hub.example:7123"\n'
    )
    requests = []
    client_type = httpx.Client

    def respond(request):
        requests.append(request)
        assert request.url.host == "hub.example" and request.url.port == 7123
        if mode == "offline":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/info":
            if mode == "invalid_json":
                return httpx.Response(200, text="bad json")
            return httpx.Response(
                200,
                json={}
                if mode == "metadata"
                else {
                    "version": "server",
                    "api_version": 0,
                    "identity": "dataset",
                },
            )
        assert request.headers["X-Toolang-Backend"] == "dataset"
        if mode == "health_json":
            return httpx.Response(200, text="bad json")
        body = {
            "health_false": {"ok": False},
            "health_number": {"ok": 1},
            "health_extra": {"ok": True, "other": True},
        }.get(mode, {"ok": True})
        return httpx.Response(503 if mode == "unready" else 200, json=body)

    monkeypatch.setattr(
        ps.httpx,
        "Client",
        lambda **kw: client_type(**kw, transport=httpx.MockTransport(respond)),
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    # A corrupt server record must not influence the client view.
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "pid.json").write_text("bad record")
    assert cli.main(["--root", str(tmp_path), "ps"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == expected
    assert lines[-2] == "1 agent, 0 running, 1 resident"
    assert "alice" in "\n".join(lines)
    assert requests


@pytest.mark.parametrize("command", ["ps", "ls"])
def test_no_resident_filter_option(command, tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), command, "--resident"]) == 2
    assert "No such option" in capsys.readouterr().err


def test_invalid_client_settings_keep_agent_listing(tmp_path, capsys):
    (tmp_path / "config.toml").write_text("[teaming]\nhub = 42\n")
    AgentLayout.resident(tmp_path, "alice").home.mkdir(parents=True)
    assert cli.main(["--root", str(tmp_path), "ps"]) == 0
    text = capsys.readouterr().out
    assert "alice" in text and "1 agent, 0 running, 1 resident" in text
    assert text.splitlines()[-1].startswith("hub, error, ")


@pytest.mark.parametrize("command", ["ls", "ps"])
def test_malformed_root_configuration_keeps_agent_listing(tmp_path, capsys, command):
    (tmp_path / "config.toml").write_text("[teaming\n")
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
        assert cli.main(["--root", str(tmp_path), command]) == 0
    text = capsys.readouterr().out
    assert "alice" in text and "1 agent, 1 running, 1 resident" in text
    if command == "ps":
        assert text.splitlines()[-1].startswith("hub, error, ")
    else:
        assert text.splitlines()[-1] == "1 agent, 1 running, 1 resident"
        assert "hub" not in text


def test_listing_failure_keeps_unknown_count_and_diagnostic(
    tmp_path, monkeypatch, capsys
):
    def unreadable(_):
        raise OSError("registry unreadable")

    monkeypatch.setattr(ps.RuntimeWatcher, "refresh", unreadable)
    monkeypatch.setattr(
        ps, "_hub_line", lambda _: "hub http://127.0.0.1:7001, unavailable"
    )
    assert cli.main(["--root", str(tmp_path), "ps"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "0 agents, - running, partial",
        "hub http://127.0.0.1:7001, unavailable",
        "partial: agent discovery: registry unreadable",
    ]


def test_command_placement_and_root_list_removal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["too"])
    assert cli.main(["--root", str(tmp_path), "--help"]) == 0
    text = capsys.readouterr().out
    agent_section = text.split("Agent commands:\n", 1)[1].split("\n\n", 1)[0]
    inspection = text.split("Inspection commands:\n", 1)[1].split("\n\n", 1)[0]
    agent_names = [line.split()[0] for line in agent_section.splitlines()]
    inspection_names = [line.split()[0] for line in inspection.splitlines()]
    assert "ls" in agent_names and "ps" not in agent_names
    assert agent_names.index("info") < agent_names.index("ls")
    assert "List agents and their status" in agent_section
    assert "Show agents and hub status" in inspection
    assert inspection_names.index("top") < inspection_names.index("ps")
    assert not any(
        line.split()[0] == "list" for line in text.splitlines() if line.strip()
    )
    assert cli.main(["--root", str(tmp_path), "list"]) == 2
    assert "No such command 'list'" in capsys.readouterr().err
