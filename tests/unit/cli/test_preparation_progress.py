"""Every CLI State preparation path uses parallel ref progress and one summary."""

from __future__ import annotations

from toolang.catalog import source as cap_source

import json

import pytest

from toolang.up import source as agent_sources

from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state
from tests.support.cli import invoke_script


_REF = "github://example/caps/prompts/review.md@main"


def _agent(root, *, scope="home"):
    layout = AgentLayout.resident(root, "dev")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow run():\n  pass\n")
    config = layout.config if scope == "home" else layout.place_config
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[prompts]\nreview = {{ref="{_REF}"}}\n')
    return layout


@pytest.mark.parametrize("scope", ["root", "home"])
@pytest.mark.parametrize("kind", [None, "prompt", "skill", "psyche", "service"])
def test_cap_inspection_uses_ref_progress_and_completes_before_records(
    tmp_path, monkeypatch, capsys, scope, kind
):
    from toolang.cli.toolang.main import main as too_main

    layout = _agent(tmp_path, scope=scope)
    # Inspection must still work without accepting an executable program.
    layout.program.write_text("invalid Toolang!!!")
    observed = []

    def fetch(_ref):
        output = capsys.readouterr()
        assert f"{_REF} fetching" in output.err
        assert "Fetching prompt" not in output.err
        assert output.out == ""
        observed.append(_ref)
        return b"Review carefully.\n"

    monkeypatch.setattr(cap_source, "_fetch_github_file", fetch)
    command = [kind, "list"] if kind else ["caps"]
    target = ["dev"] if scope == "home" else []
    assert too_main(["--root", str(tmp_path), *target, *command, "--json"]) == 0
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), list)
    assert observed
    assert output.err.count("Prepared 1 cap in ") == 1
    assert "Preparing" not in output.err
    assert "\x1b" not in output.err
    assert not layout.agent_state.exists()


def test_cached_cap_inspection_has_no_preparation_output(tmp_path, monkeypatch, capsys):
    from toolang.cli.toolang.main import main

    layout = _agent(tmp_path)
    monkeypatch.setattr(
        cap_source, "_fetch_github_file", lambda _: b"Review carefully.\n"
    )
    prepare_agent_state(layout)
    monkeypatch.setattr(
        cap_source,
        "_fetch_github_file",
        lambda _: pytest.fail("cached caps must not be fetched again"),
    )
    assert main(["--root", str(tmp_path), "dev", "caps", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)
    assert output.err == ""


def test_cap_edit_publishes_with_ref_progress(tmp_path, monkeypatch, capsys):
    from toolang.cli.toolang.main import main

    layout = _agent(tmp_path)
    (layout.home / "prompts").mkdir()
    (layout.home / "prompts" / "note.md").write_text("Original.\n")
    monkeypatch.setattr(
        "toolang.cli.caps.commands.edit_markdown", lambda _: "Updated.\n"
    )

    def fetch(_ref):
        assert f"{_REF} fetching" in capsys.readouterr().err
        return b"Review carefully.\n"

    monkeypatch.setattr(cap_source, "_fetch_github_file", fetch)
    assert main(["--root", str(tmp_path), "dev", "prompt", "edit", "note"]) == 0
    output = capsys.readouterr()
    assert "Prompt note updated:" in output.out
    assert output.err.count("Prepared 1 cap in ") == 1
    assert layout.agent_state.exists()


def test_offline_info_prepares_current_state_with_ref_progress(
    tmp_path, monkeypatch, capsys
):
    from toolang.cli.toolang.main import main

    _agent(tmp_path)
    monkeypatch.setattr(
        cap_source,
        "_fetch_github_file",
        lambda _: b"Review carefully.\n",
    )
    assert main(["--root", str(tmp_path), "dev", "info"]) == 0
    output = capsys.readouterr()
    assert "Status: down" in output.out
    assert "Prepared" in output.err
    assert "1 prompt" in output.out


@pytest.mark.parametrize("command", [["caps"], ["prompt", "list"]])
def test_remote_cap_inspection_shares_agent_and_cap_progress(
    tmp_path, monkeypatch, capsys, command
):
    from toolang.cli.toolang.main import main

    selector = "https://example.test/dev.too"
    calls = []

    def source(_url):
        calls.append("agent")
        return f"with prompt {_REF}\nflow run():\n  pass\n"

    def cap(_ref):
        assert calls == ["agent"]
        calls.append("cap")
        return b"Review carefully.\n"

    monkeypatch.setattr(agent_sources, "_fetch_http_text", source)
    monkeypatch.setattr(cap_source, "_fetch_github_file", cap)
    assert main(["--root", str(tmp_path), selector, *command, "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)
    assert calls == ["agent", "cap"]
    assert f"{selector} fetching" in output.err
    assert f"{_REF} fetching" in output.err
    assert output.err.count("Prepared agent and 1 cap in ") == 1
    assert "Fetching agent" not in output.err


@pytest.mark.parametrize("command", ["tools", "models", "providers", "workspace"])
def test_remote_help_and_invalid_options_do_not_prepare_sources(
    tmp_path, monkeypatch, capsys, command
):
    from toolang.cli.toolang import main as cli

    selector = "https://example.test/dev.too"
    monkeypatch.setattr(
        agent_sources,
        "_fetch_http_text",
        lambda _: pytest.fail("invalid/help invocation fetched a source"),
    )
    for tail, expected in ((["--help"], 0), (["--unknown"], 2)):
        assert cli.main(["--root", str(tmp_path), selector, command, *tail]) == expected
        capsys.readouterr()
    assert not list(tmp_path.rglob("agent.too"))


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("setup_fails", [False, True])
def test_local_script_completes_preparation_before_setup_and_execution(
    tmp_path, monkeypatch, capsys, quiet, setup_fails
):
    from io import StringIO

    from tests.support.execution_harness import ExecutionHarness
    from toolang.execution.executor import RunExecutor
    from toolang.setup import SetupWatcher

    source = tmp_path / "dev.too"
    text = f"with prompt {_REF}\nflow run(_: Text):\n  pass\n"
    source.write_text(text)
    # The real State pipeline resolves caps; only the model/setup collaborators
    # come from the execution harness, and this flow makes no provider calls.
    harness = ExecutionHarness.create(
        tmp_path / "fixture", source="flow run(_: Text):\n  pass\n", responses=[]
    )
    monkeypatch.setattr(
        cap_source, "_fetch_github_file", lambda _: b"Review carefully.\n"
    )

    preparation_output = []

    async def setup(_watcher, *, progress=None):
        output = capsys.readouterr()
        preparation_output.append(output.err)
        if quiet:
            assert "Prepared" not in output.err
            assert "fetching" not in output.err
        else:
            assert f"{_REF} fetching" in output.err
            assert output.err.count("Prepared 1 cap in ") == 1
        if setup_fails:
            raise ValueError("Setup unavailable")
        return harness.setup

    monkeypatch.setattr(SetupWatcher, "refresh", setup)
    monkeypatch.setattr(SetupWatcher, "current", lambda _: harness.setup)
    original_start = RunExecutor.start
    observed = []

    def start(executor):
        output = capsys.readouterr()
        observed.append(output.err)
        assert "Prepared" not in output.err
        assert "fetching" not in output.err
        return original_start(executor)

    monkeypatch.setattr(RunExecutor, "start", start)
    try:
        args = [str(source), "flow:run", *(["--quiet"] if quiet else []), "--", "done"]
        assert invoke_script(args, prog_name="too", stdin=StringIO()) == (
            1 if setup_fails else 0
        )
        assert bool(observed) == (not setup_fails)
        assert preparation_output
        if setup_fails:
            output = capsys.readouterr()
            assert "Setup unavailable" in output.err
            assert "Prepared" not in output.err
        assert not harness.adapter.invocations
    finally:
        harness.store.close()


@pytest.mark.parametrize("scope", ["root", "home"])
def test_cap_add_uses_ref_rows_without_summarizing_reference_validation(
    tmp_path, monkeypatch, capsys, scope
):
    from toolang.cli.toolang.main import main

    layout = _agent(tmp_path)
    layout.config.write_text("")
    observed = []

    def exists(_kind, _ref):
        output = capsys.readouterr()
        observed.append(output.err)
        assert f"{_REF} resolving" in output.err
        assert "Resolving prompt" not in output.err
        return True

    def fetch(_ref):
        output = capsys.readouterr()
        assert f"{_REF} fetching" in output.err
        assert "Prepared" not in output.err
        return b"Review carefully.\n"

    monkeypatch.setattr(cap_source, "_github_remote_exists", exists)
    monkeypatch.setattr(cap_source, "_fetch_github_file", fetch)
    target = ["dev"] if scope == "home" else []
    assert main(["--root", str(tmp_path), *target, "prompt", "add", _REF]) == 0
    output = capsys.readouterr()
    assert observed
    assert "Prompt review added:" in output.out
    assert output.err.count("Prepared 1 cap in ") == (scope == "home")
    assert ("Prepared" in output.err) == (scope == "home")


def test_cap_add_resolution_failure_keeps_diagnostics_without_success_summary(
    tmp_path, monkeypatch, capsys
):
    from toolang.cli.toolang.main import main

    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda *_: False)
    assert main(["--root", str(tmp_path), "prompt", "add", _REF]) == 1
    output = capsys.readouterr()
    assert f"{_REF} resolving" in output.err
    assert "Failed to resolve prompt" in output.err
    assert "Stage: prepare.resolve" in output.err
    assert "remote prompt not found" in output.err
    assert "Prepared" not in output.err
    assert "added:" not in output.out
