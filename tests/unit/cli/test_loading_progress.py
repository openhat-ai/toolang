"""Blocking resource preparation stays visible until the command can continue."""

from io import StringIO

import pytest

from toolang.cli.toolang.main import main
from toolang.common.layout import AgentLayout
from toolang.state import state as cap_state


@pytest.mark.parametrize("command", ["edit", "new"])
@pytest.mark.parametrize("save", [False, True])
def test_cap_materialization_only_starts_after_saving(
    tmp_path, monkeypatch, capsys, command, save
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
    edited = []

    def edit(text):
        assert "Preparing" not in capsys.readouterr().err
        edited.append(text)
        return "Updated local content.\n" if save else None

    monkeypatch.setattr("toolang.cli.caps.commands.edit_markdown", edit)
    observed = []

    def materialize(*, relative_entry_path, **kwargs):
        assert edited
        observed.append(kwargs.get("progress") is not None)
        assert "Preparing" in capsys.readouterr().err
        return {str(relative_entry_path): b"Remote content.\n"}

    monkeypatch.setattr(cap_state, "_remote_materialized_files", materialize)
    arguments = ["prompt", command, "note" if command == "edit" else "another"]
    assert main(["--root", str(tmp_path), "alice", *arguments]) == 0
    assert observed == ([True] if save else [])


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
    monkeypatch.setattr(plugin, "load_tool_setup", load_setup)
    assert main(["--root", str(tmp_path), command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert loads == ["tools" if command == "tools" else "models"]


@pytest.mark.parametrize("target", [None, "alice"])
def test_tools_does_not_load_invalid_model_catalog(
    tmp_path, monkeypatch, capsys, target
):
    import json

    catalog = tmp_path / "broken-catalog.json"
    catalog.write_text("invalid JSON")
    monkeypatch.setenv("TOOLANG_MODEL_CATALOG", str(catalog))
    args = ["--root", str(tmp_path)]
    if target:
        layout = AgentLayout.resident(tmp_path, target)
        layout.home.mkdir(parents=True)
        layout.program.write_text("flow run():\n  pass\n")
        args.append(target)
    assert main([*args, "tools", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)
    assert "model catalog" not in output.err
