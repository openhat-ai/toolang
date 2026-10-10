"""Agent info summarizes resources from the unified setup accessors."""

from pathlib import Path

import pytest
from toolang.cli.toolang.main import main
from toolang.cli.common.client import RuntimeClient
from toolang.cli.common.errors import RuntimeClientError
from toolang.up.process import AgentProcess, AgentStatus

from tests.support.setup import materialized_setup
from toolang.base.types.model import Model, ModelToolang, Provider
from toolang.cli.toolang.commands import agent
from toolang.common.layout import AgentLayout
from toolang.plugin.models import query
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentSetup


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


@pytest.mark.parametrize("sandbox", ["host", "docker:python:3.13-slim"])
@pytest.mark.parametrize("placement", ["resident", "roaming", "visiting"])
@pytest.mark.parametrize("target_first", [False, True])
def test_running_info_uses_runtime_resources_without_loading_local_sources(
    tmp_path, monkeypatch, capsys, sandbox, placement, target_first
):
    from toolang.up import process

    monkeypatch.setenv("HOME", str(tmp_path))
    selector = "alice"
    global_args = ["--root", str(tmp_path)]
    layout = AgentLayout.resident(tmp_path, "alice")
    if placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text("invalid Toolang!!!")
        (tmp_path / "toolang.toml").write_text("invalid TOML!!!")
        layout = AgentLayout.roaming(source)
        selector, global_args = str(source), []
        monkeypatch.setattr(
            process,
            "materialize_roaming_program",
            lambda *_: pytest.fail("running info must not project sources"),
        )
    elif placement == "visiting":
        selector, global_args = "acme/alice", []
        layout = AgentLayout(
            root=tmp_path / "visiting", name="alice", placement="visiting"
        )
        monkeypatch.setattr(process, "visiting_layout", lambda _: layout)
        monkeypatch.setattr(
            process,
            "resolve_visiting_layout",
            lambda *a, **kw: pytest.fail("running info must not fetch sources"),
        )
    arguments = [
        *global_args,
        *([selector, "info"] if target_first else ["info", selector]),
    ]
    layout.home.mkdir(parents=True)
    layout.program.write_text("invalid Toolang!!!")
    monkeypatch.setattr(
        AgentProcess,
        "status",
        lambda *_args, **_kwargs: AgentStatus(
            name="alice",
            status="running",
            endpoint="http://localhost:8123",
            api_url=None,
            webui_url=None,
            sandbox=sandbox,
        ),
    )
    monkeypatch.setattr(AgentProcess, "state", lambda _self: {})
    payloads = {
        "/api/v1/models": {"items": [{"provider": "guest"}]},
        "/api/v1/tools": {"items": [{"toolset": "guest"}, {"toolset": "guest"}]},
        "/api/v1/caps": {"psyches": [], "skills": [{}], "services": [], "prompts": []},
        "/api/v1/tasks": [{}],
        "/api/v1/chores": [],
        "/api/v1/workspaces": {
            "revision": "a" * 64,
            "items": [{"name": "guest", "path": "/guest", "available": True}],
            "workdir": "guest://",
        },
    }
    calls = []

    def get(_self, path, **_kwargs):
        if not calls:
            assert "Loading runtime resources..." in capsys.readouterr().err
        calls.append(path)
        return payloads[path]

    monkeypatch.setattr(RuntimeClient, "get", get)
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert "1 model, 1 provider" in output.out
    assert "2 tools, 1 toolset" in output.out
    assert "1 skill" in output.out
    assert "0 chores, 1 task" in output.out
    assert "guest" in output.out
    assert set(calls) == set(payloads)
    assert not (layout.home / ".state").exists()

    calls.clear()
    assert main([*arguments, "--catalog", "other.json"]) == 1
    assert "--catalog" in capsys.readouterr().err
    assert not calls

    def failed(_self, _path, **_kwargs):
        raise RuntimeClientError("runtime request failed: unavailable")

    monkeypatch.setattr(RuntimeClient, "get", failed)
    assert main(arguments) == 1
    assert "runtime request failed: unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
@pytest.mark.parametrize("target_first", [False, True])
def test_offline_info_prepares_a_fresh_nonresident_target(
    tmp_path, monkeypatch, capsys, placement, target_first
):
    from toolang.up import process

    source = tmp_path / "alice.too"
    source.write_text("flow run():\n  pass\n")
    selector = str(source)
    layout = AgentLayout.roaming(source)
    if placement == "visiting":
        selector = "acme/alice"
        layout = AgentLayout(
            root=tmp_path / "visiting", name="alice", placement="visiting"
        )
        monkeypatch.setattr(process, "visiting_layout", lambda _: layout)

        def fetch(selected, *, progress):
            assert selected == selector
            assert progress is not None
            assert not layout.program.exists()
            layout.home.mkdir(parents=True)
            layout.program.write_text(source.read_text())
            return layout

        monkeypatch.setattr(process, "resolve_visiting_layout", fetch)
    setup = _setup(tmp_path, ())

    class Watcher:
        def __init__(self, selected):
            assert selected == layout

        async def refresh(self, *, progress):
            return setup

    monkeypatch.setattr(agent, "SetupWatcher", Watcher)
    assert not layout.home.exists()
    assert main([selector, "info"] if target_first else ["info", selector]) == 0
    output = capsys.readouterr()
    assert "not running" in output.out
    assert layout.program.is_file()
    assert layout.agent_state.is_dir()
