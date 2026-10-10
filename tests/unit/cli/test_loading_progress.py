"""Blocking resource preparation stays visible until the command can continue."""

from io import StringIO

import pytest

from toolang.cli.toolang.main import main
from toolang.common.layout import AgentLayout
from toolang.state import state as cap_state


@pytest.mark.parametrize("command", ["edit", "new"])
def test_cap_materialization_reports_progress_before_editing(
    tmp_path, monkeypatch, capsys, command
):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow run():\n  pass\n")
    layout.config.write_text('[prompts]\nremote = { ref = "acme/remote" }\n')
    (layout.home / "prompts").mkdir()
    (layout.home / "prompts" / "note.md").write_text("Local note.\n")
    monkeypatch.setattr("sys.stdin", StringIO())
    monkeypatch.setattr(cap_state, "_github_repo_default_branch", lambda *_: "main")
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda *_: True)
    monkeypatch.setattr("toolang.cli.caps.commands.edit_markdown", lambda _: None)
    observed = []

    def materialize(*, relative_entry_path, **kwargs):
        observed.append(kwargs.get("progress") is not None)
        assert "Preparing" in capsys.readouterr().err
        return {str(relative_entry_path): b"Remote content.\n"}

    monkeypatch.setattr(cap_state, "_remote_materialized_files", materialize)
    arguments = ["prompt", command, "note" if command == "edit" else "another"]
    assert main(["--root", str(tmp_path), "alice", *arguments]) == 0
    assert observed == [True]


@pytest.mark.parametrize("command", ["models", "providers", "tools"])
def test_setup_inspection_reports_loading_before_work_and_keeps_json_clean(
    tmp_path, monkeypatch, capsys, command
):
    import json
    from dataclasses import replace

    from tests.support.setup import materialized_setup
    from toolang.cli.toolang.commands import model_catalog, plugin
    from toolang.plugin.toolsets.collections import ToolCollection
    from toolang.setup.progress import setup_progress

    setup = materialized_setup(
        layout=AgentLayout.resident(tmp_path, "default"),
        providers=(),
        adapters={},
        models=(),
        tools=ToolCollection(),
        envs={},
    )
    loads = []
    original_models, original_tools = setup._load_models, setup._load_tools

    def models(current):
        assert "Loading models..." in capsys.readouterr().err
        loads.append("models")
        return original_models(current)

    def tools(plugins):
        assert "Loading tools..." in capsys.readouterr().err
        loads.append("tools")
        return original_tools(plugins)

    setup = replace(setup, _load_models=models, _load_tools=tools)

    async def load_setup(_layout, *, progress=None, **_kwargs):
        assert progress is not None
        with setup_progress(progress, target="default", resource="setup"):
            assert "Loading setup..." in capsys.readouterr().err
            return setup

    monkeypatch.setattr(model_catalog, "load_setup", load_setup)
    monkeypatch.setattr(plugin, "load_setup", load_setup)
    assert main(["--root", str(tmp_path), command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert loads == ["tools" if command == "tools" else "models"]
