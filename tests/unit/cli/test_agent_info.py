"""Agent info summarizes resources from the unified setup accessors."""

from pathlib import Path

import pytest
from toolang.up import source as agent_sources

from toolang.cli.toolang.main import main
from toolang.up.types import AgentStatus

from tests.support.setup import materialized_setup
from toolang.base.types.model import Model, ModelToolang, Provider
from toolang.cli.toolang.commands import agent
from toolang.common.layout import AgentLayout
from toolang.up.allocation import allocate_roaming
from toolang.plugin.models import query
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentSetup


@pytest.mark.parametrize("arguments", [("info", "huk"), ("huk", "info")])
def test_info_reports_a_missing_agent_without_an_info_card(tmp_path, capsys, arguments):
    assert main(["--root", str(tmp_path), *arguments]) == 1

    output = capsys.readouterr()
    assert output.err.strip() == "Error: Agent huk not found"
    assert not output.out.strip()
    assert not (tmp_path / "agents" / "huk").exists()


def test_info_preserves_details_when_the_runtime_reference_is_invalid(
    tmp_path, monkeypatch, capsys
):
    from toolang.up.instance import update_instance
    from toolang.up.workload import ForegroundWorkload

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow main():\n  pass\n")
    monkeypatch.setattr(
        agent,
        "_local_resources",
        lambda *args, **kwargs: ([("Tools", "configured")], None),
    )
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as active:
        update_instance(
            layout.runtime_status,
            lambda entry: entry["control"].update(ref={"runtime_id": "broken"}),
        )
        before = layout.runtime_status.read_bytes()
        assert main(["--root", str(tmp_path), "alice", "info"]) == 0
        text = capsys.readouterr().out
        assert "Home:" in text and "Tools: configured" in text
        assert "Status: unknown" in text and "PID:" not in text
        assert active.path.exists() and layout.runtime_status.read_bytes() == before


@pytest.mark.parametrize("name", ["alice", "info", "top", "run", "more"])
@pytest.mark.parametrize("width", [44, 80])
def test_info_shows_home_without_redundant_identity_or_footer(
    tmp_path, monkeypatch, capsys, name, width
):
    from rich.console import Console
    from toolang.catalog.agent import LocalAgents
    from toolang.cli.common import output

    root = tmp_path / "toolang root"
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "other root"))
    LocalAgents(root / "agents").create(name, content="flow main():\n  pass\n")
    monkeypatch.setattr(
        output, "_INFO_CONSOLE", Console(width=width, color_system=None)
    )

    assert main(["--root", str(root), f"agent:{name}", "info"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[1].strip() == name.upper()
    assert "Home:" in output
    assert "Root:" not in output
    assert "Placement:" not in output
    assert "Agent:" not in output
    assert "Toolang root:" not in output
    assert "Agent root:" not in output
    assert "Command:" not in output


def _setup(tmp_path: Path, refs: tuple[str, ...]) -> AgentSetup:
    return materialized_setup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers=tuple(
            Provider(id=name, name=name)
            for name in dict.fromkeys(ref.partition("/")[0] for ref in refs)
        ),
        adapters={},
        models=tuple(
            Model(
                id=ref.partition("/")[2],
                name=ref,
                _toolang=ModelToolang(ready=True),
                provider=ref.partition("/")[0],
            )
            for ref in refs
        ),
        tools=ToolCollection(),
        envs={},
    )


def test_large_model_summary_does_not_use_query_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = tuple(f"provider{index % 5}/model{index}" for index in range(1000))
    setup = _setup(tmp_path, refs)
    monkeypatch.setattr(
        query,
        "match_model_branches",
        lambda *args, **kwargs: pytest.fail("runtime model refs are not queries"),
    )

    assert agent._models_summary(setup) == "1000 models, 5 providers"


@pytest.mark.parametrize(
    ("refs", "expected"),
    [
        ((), "0 models, 0 providers"),
        (("one/a",), "1 model, 1 provider"),
        (("one/a", "one/b"), "2 models, 1 provider"),
        (("one/a", "two/b"), "2 models, 2 providers"),
    ],
)
def test_model_summary_counts_effective_setup_resources(
    tmp_path: Path, refs: tuple[str, ...], expected: str
) -> None:
    assert agent._models_summary(_setup(tmp_path, refs)) == expected


@pytest.mark.parametrize("placement", ["resident", "roaming", "visiting"])
@pytest.mark.parametrize("running", [False, True])
def test_info_prepares_current_resources_for_all_placements(
    tmp_path, monkeypatch, capsys, placement, running
):
    from toolang.up.workload import ForegroundWorkload
    from contextlib import nullcontext
    from rich.console import Console
    from toolang.cli.common import output

    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(output, "_INFO_CONSOLE", Console(width=300, color_system=None))
    content = "flow main():\n  pass\n"
    if placement == "resident":
        layout = AgentLayout.resident(tmp_path, "alice")
        layout.home.mkdir(parents=True)
        layout.program.write_text(content)
        selector = "alice"
    elif placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text(content)
        layout = allocate_roaming(source, toolang_root=tmp_path, register=False)
        selector = str(source)
    else:
        selector = "https://example.com/alice.too"
        layout = agent_sources.visiting_layout(selector, toolang_root=tmp_path)
        monkeypatch.setattr(
            agent_sources, "fetch_agent_ref", lambda *args, **kwargs: content
        )
    with (
        ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset({"talk"}))
        if running
        else nullcontext()
    ):
        assert main([selector, "info"]) == 0
        text = capsys.readouterr().out
        if running:
            assert "Resources: unavailable" in text
            assert "Status: running" in text
            assert not layout.run_store.exists()
            return
        assert "Tools:" in text and "0 tools" not in text
        assert "Models:" in text and "models" in text
        assert "Caps:" in text
        labels = ("Home", "Caps", "Tools", "Models", "Chores", "Workspaces", "Status")
        positions = [text.index(f"{label}:") for label in labels]
        assert positions == sorted(positions)
        title = layout.name.upper()
        if layout.place_code:
            title += f"@{layout.place_code}"
        assert text.splitlines()[1].strip() == title
        assert ("Status: running" in text) is running
        assert layout.agent_state.exists()
        assert not layout.run_store.exists()
        assert "Root:" not in text and "Agent:" not in text and "Placement:" not in text


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"drivers": ["chore", "talk", "chat"]}, "chat,chore,talk"),
        ({"drivers": ["chore", "chat"]}, "chat,chore"),
        ({"drivers": []}, "none"),
        ({}, "-"),
        ({"drivers": None}, "-"),
        ({"drivers": "chat"}, "-"),
        ({"drivers": ("chat",)}, "-"),
        ({"drivers": ["chat", "chat"]}, "-"),
        ({"drivers": ["unknown"]}, "-"),
        ({"drivers": [1]}, "-"),
        ({"drivers": [["chat"]]}, "-"),
    ],
)
def test_running_info_formats_driver_metadata(
    tmp_path, monkeypatch, capsys, metadata, expected
):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow main():\n  pass\n")
    monkeypatch.setattr(
        agent,
        "runtime_status",
        lambda *_args, **_kwargs: AgentStatus(
            name="alice",
            status="running",
            endpoint="http://localhost:8123",
            api_url=None,
            webui_url=None,
            sandbox="host",
            entry={"control": metadata, "workload": {}, "launch_id": "launch"},
        ),
    )
    monkeypatch.setattr(agent, "_local_resources", lambda *_args, **_kwargs: ([], None))

    assert main(["--root", str(tmp_path), "alice", "info"]) == 0
    assert f"Drivers: {expected}" in capsys.readouterr().out
