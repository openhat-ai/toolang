from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import hashlib
import io
import json
from pathlib import Path
import os
import time
import tomllib
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4
import pytest
from typer.testing import CliRunner

from toolang import agents
from toolang import caps
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelInfo
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.cli.chat_history import ChatInputHistoryStore
import toolang.cli.toolang.app as cli
import toolang.cli.toolang.inspect as inspect_cli
import toolang.cli.invoke as cli_invoke
import toolang.cli.caps.app as caps_cli
import toolang.cli.caps.commands as caps_commands
import toolang.cli.logo as cli_logo
import toolang.cli.utils as cli_utils
from toolang.cli.progress import CliProgress
from toolang.components.trigger import watch
from toolang.config.log import DEFAULT_AGENT_LOG_SPEC
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang.execution.events import RunEnd, RunStart, StepEnd, StepStart
from toolang.execution.records import ChildCallStepPayload, FlowOpStepPayload
from toolang.common.progress import ProgressEvent
from toolang import work
from toolang.execution.db import ExecutionStore, execution_db_path
runner = CliRunner()
DEFAULT_AGENT_SOURCE = "# Customize this agent here.\n# Docs: https://toolang.ai/docs\n"


def _invoke_app(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input: str | None = None,
    prefix_agent: str | None = None,
):
    previous = cli._CLI_PREFIX_AGENT
    cli._CLI_PREFIX_AGENT = prefix_agent
    try:
        return runner.invoke(cli.app, args, env=env, input=input)
    finally:
        cli._CLI_PREFIX_AGENT = previous


def _invoke_caps_app(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    prefix_agent: str | None = None,
):
    previous = caps_cli._CLI_PREFIX_AGENT
    caps_cli._CLI_PREFIX_AGENT = prefix_agent
    try:
        return runner.invoke(caps_cli.app, args, env=env)
    finally:
        caps_cli._CLI_PREFIX_AGENT = previous


def _indexes_in_order(text: str, tokens: tuple[str, ...]) -> bool:
    indexes = [text.index(token) for token in tokens]
    return indexes == sorted(indexes)


def _ansi_truecolor_background(text: str) -> str | None:
    marker = "48;2;"
    start = text.find(marker)
    if start < 0:
        return None
    values = text[start + len(marker) :].split("m", 1)[0].split(";")[:3]
    if len(values) != 3:
        return None
    red, green, blue = (int(value) for value in values)
    return f"{red:02x}{green:02x}{blue:02x}"


class _FakeModelProvider:
    def __init__(
        self,
        *,
        name: str,
        description: str | None = None,
        required_env: tuple[str, ...] = (),
        base_url: str | None = None,
        api_key_env: str | None = None,
        models: tuple[ModelInfo, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self._required_env = required_env
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._models = models

    def required_env_vars(self) -> tuple[str, ...]:
        return self._required_env

    def default_base_url(self, *, environ) -> str | None:
        del environ
        return self._base_url

    def default_api_key_env(self) -> str | None:
        return self._api_key_env

    def list_models(self, *, environ) -> tuple[ModelInfo, ...]:
        del environ
        return self._models


class _FakeLeafTool:
    def __init__(self, *, name: str, description: str) -> None:
        self.name = name
        self._description = description

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self._description)

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        del arguments, context
        return {}


class _FakeLoadedTool:
    def __init__(self, *, plugin_name: str, leaf_name: str, description: str) -> None:
        self.plugin_name = plugin_name
        self.leaf_tool = _FakeLeafTool(name=leaf_name, description=description)
        self.name = f"{plugin_name}__{leaf_name}"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.leaf_tool.definition().description)

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        del arguments, context
        return {}


def test_cli_main_normalizes_agent_prefix_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prog_name"] = prog_name
        captured["standalone_mode"] = standalone_mode

    monkeypatch.setattr(cli, "app", cast(object, fake_app))
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main(["alice", "stop"])

    assert result == 0
    assert captured["args"] == ["stop", "alice"]
    assert captured["prog_name"] == "toolang"
    assert captured["standalone_mode"] is True


def test_cli_main_normalizes_agent_postfix_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["stop", "alice"])

    assert result == 0
    assert captured["args"] == ["stop", "alice"]


def test_cli_main_intercepts_local_too_program_before_typer(monkeypatch, tmp_path: Path) -> None:
    program_path = tmp_path / "demo.too"
    program_path.write_text("thunk:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_handle(global_args: list[str], body: list[str], *, prog_name: str) -> int:
        captured["global_args"] = list(global_args)
        captured["body"] = list(body)
        captured["prog_name"] = prog_name
        return 0

    monkeypatch.setattr(cli.cli_invoke, "handle_roaming_invoke", fake_handle)
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main([str(program_path), "--help"])

    assert result == 0
    assert captured["global_args"] == []
    assert captured["body"] == [str(program_path), "--help"]
    assert captured["prog_name"] == "toolang"


def test_cli_main_runs_roaming_file_runtime_for_script_inbox(monkeypatch, tmp_path: Path) -> None:
    program_path = tmp_path / "demo.too"
    program_path.write_text("thunk file(in: Part[]):\n  Process a file.\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    captured: dict[str, object] = {}

    def fake_file_runtime(global_args: list[str], body: list[str]) -> int:
        captured["global_args"] = list(global_args)
        captured["body"] = list(body)
        return 0

    monkeypatch.setattr(cli, "_run_roaming_file_runtime", fake_file_runtime)
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main([str(program_path), "--inbox", str(inbox)])

    assert result == 0
    assert captured["global_args"] == []
    assert captured["body"] == [str(program_path), "--inbox", str(inbox)]


def test_cli_main_routes_roaming_thread_commands_to_materialized_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    program_path = tmp_path / "demo.too"
    program_path.write_text("thunk:\n  Reply directly.\n", encoding="utf-8")
    toolang_root = tmp_path / ".toolang"
    captured: dict[str, object] = {}

    def fake_materialize(path: Path) -> tuple[Path, str]:
        captured["source"] = path
        return toolang_root, "demo"

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prog_name"] = prog_name
        captured["standalone_mode"] = standalone_mode
        captured["prefix_agent"] = cli._CLI_PREFIX_AGENT

    monkeypatch.setattr(cli.agents, "materialize_roaming_program", fake_materialize)
    monkeypatch.setattr(cli, "app", cast(object, fake_app))
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main([str(program_path), "threads"])

    assert result == 0
    assert captured == {
        "source": program_path.resolve(),
        "args": ["--root", str(toolang_root), "threads"],
        "prog_name": "toolang",
        "standalone_mode": True,
        "prefix_agent": "demo",
    }
    assert cli._CLI_PREFIX_AGENT is None


def test_cli_main_roaming_threads_can_read_offline_materialized_store(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    program_path = _write_roaming_program(tmp_path, "thunk:\n  Reply directly.\n", name="demo")
    toolang_root, agent_name = agents.materialize_roaming_program(program_path)
    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    try:
        run = store.start_run(
            run_id="run_first",
            thread_id="script_main",
            origin="script",
            input=Message.user("roaming input"),
            created_at="2026-06-06T01:00:00Z",
            started_at="2026-06-06T01:00:00Z",
        )
        store.finish_run(run_id=run.run_id, finished_at="2026-06-06T01:01:00Z")
    finally:
        store.close()

    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main([str(program_path), "threads"])
    output = capsys.readouterr()

    assert result == 0
    assert "script_main" in output.out
    assert "roaming input" in output.out


def test_cli_main_keeps_roaming_thunk_invoke_when_thunk_is_present(monkeypatch, tmp_path: Path) -> None:
    program_path = tmp_path / "demo.too"
    program_path.write_text("thunk file(in: Part[]):\n  Process a file.\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    captured: dict[str, object] = {}

    def fake_handle(global_args: list[str], body: list[str], *, prog_name: str) -> int:
        captured["global_args"] = list(global_args)
        captured["body"] = list(body)
        captured["prog_name"] = prog_name
        return 0

    def fail_file_runtime(global_args: list[str], body: list[str]) -> int:
        del global_args, body
        raise AssertionError("file runtime should not be used")

    monkeypatch.setattr(cli, "_run_roaming_file_runtime", fail_file_runtime)
    monkeypatch.setattr(cli.cli_invoke, "handle_roaming_invoke", fake_handle)
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main([str(program_path), "file", "--inbox", str(inbox)])

    assert result == 0
    assert captured["global_args"] == []
    assert captured["body"] == [str(program_path), "file", "--inbox", str(inbox)]
    assert captured["prog_name"] == "toolang"


def test_cli_main_does_not_preconfigure_roaming_invoke_from_py_log(monkeypatch, tmp_path: Path) -> None:
    program_path = tmp_path / "demo.too"
    program_path.write_text("thunk:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_configure_logging(*, spec: str | None, environ) -> None:
        captured["spec"] = spec
        captured["environ"] = dict(environ)

    def fake_handle(global_args: list[str], body: list[str], *, prog_name: str) -> int:
        captured["global_args"] = list(global_args)
        captured["body"] = list(body)
        captured["prog_name"] = prog_name
        return 0

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(cli.cli_invoke, "handle_roaming_invoke", fake_handle)
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])
    monkeypatch.setenv(PY_LOG_ENV_VAR, "toolang.run=debug")

    result = cli.main([str(program_path), "--help"])

    assert result == 0
    assert captured["spec"] is None
    assert PY_LOG_ENV_VAR not in cast(dict[str, str], captured["environ"])
    assert captured["global_args"] == []
    assert captured["body"] == [str(program_path), "--help"]
    assert captured["prog_name"] == "toolang"


def test_cli_main_does_not_preconfigure_logging_for_standard_commands(monkeypatch) -> None:
    calls: list[tuple[str | None, dict[str, str]]] = []

    def fake_configure_logging(*, spec: str | None, environ) -> None:
        calls.append((spec, dict(environ)))

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        del args, prog_name, standalone_mode

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(cli, "app", cast(object, fake_app))
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main(["list"])

    assert result == 0
    assert calls == []


def test_cli_main_uses_actual_cli_name_for_prog_name(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prog_name"] = prog_name

    monkeypatch.setattr(cli, "app", cast(object, fake_app))
    monkeypatch.setattr(cli.sys, "argv", ["too"])

    result = cli.main(["list"])

    assert result == 0
    assert captured["args"] == ["list"]
    assert captured["prog_name"] == "too"


def test_cli_main_normalizes_agent_prefix_shortcut_for_info(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["alice", "info"])

    assert result == 0
    assert captured["args"] == ["info", "alice"]


def test_cli_main_passes_cap_command_without_agent_prefix(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["skill", "add", "by3gus/pdf-processing"])

    assert result == 0
    assert captured["args"] == ["skill", "add", "by3gus/pdf-processing"]


def test_cli_main_normalizes_agent_prefix_shortcut_for_task_commands(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prefix_agent"] = cli._CLI_PREFIX_AGENT

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["alice", "task", "list"])

    assert result == 0
    assert captured["args"] == ["task", "list"]
    assert captured["prefix_agent"] == "alice"


def test_cli_main_normalizes_agent_prefix_shortcut_for_cap_commands(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prefix_agent"] = cli._CLI_PREFIX_AGENT

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["alice", "skill", "list"])

    assert result == 0
    assert captured["args"] == ["skill", "list"]
    assert captured["prefix_agent"] == "alice"


def test_cli_new_creates_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(cli.app, ["new", "alice"], env={"TOOLANG_ROOT": str(toolang_root)})

    assert result.exit_code in {0, 2}
    program_path = toolang_root / "agents" / "alice" / "agent.too"
    assert result.stdout.strip() == f"Created agent alice: {program_path}"
    assert program_path.read_text(encoding="utf-8") == DEFAULT_AGENT_SOURCE


def test_cli_new_uses_named_template(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["new", "alice", "--template", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    assert (toolang_root / "agents" / "alice" / "agent.too").read_text(encoding="utf-8") == DEFAULT_AGENT_SOURCE


def test_cli_callback_configures_logging_for_standard_commands_from_py_log(monkeypatch, tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    calls: list[tuple[str | None, dict[str, str]]] = []

    def fake_configure_logging(*, spec: str | None, environ) -> None:
        calls.append((spec, dict(environ)))

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "list"],
        env={PY_LOG_ENV_VAR: "toolang.run=debug"},
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] is None
    assert calls[0][1][PY_LOG_ENV_VAR] == "toolang.run=debug"


def test_cli_new_supports_template_alias(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["new", "alice", "-t", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    assert (toolang_root / "agents" / "alice" / "agent.too").read_text(encoding="utf-8") == DEFAULT_AGENT_SOURCE


def test_cli_clone_copies_agent_without_caps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    source_home = toolang_root / "agents" / "alice"
    (source_home / "skills" / "reviewer").mkdir(parents=True, exist_ok=True)
    (source_home / ".caps").mkdir(parents=True, exist_ok=True)
    (source_home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    (source_home / "skills" / "reviewer" / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
    (source_home / ".caps" / "lock.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["clone", "alice", "bob"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    target_program = toolang_root / "agents" / "bob" / "agent.too"
    assert result.stdout.strip() == f"Cloned agent bob: {target_program}"
    assert target_program.read_text(encoding="utf-8") == "agent bob\n"
    assert (toolang_root / "agents" / "bob" / "skills" / "reviewer" / "SKILL.md").is_file()
    assert not (toolang_root / "agents" / "bob" / ".caps").exists()


def test_agent_selector_parsing_supports_name_shorthand_and_ref() -> None:
    local = agents.parse_agent_selector("alice")
    github_short = agents.parse_agent_selector("brice/alice")
    host_short = agents.parse_agent_selector("toolang.ai/alice")
    github_ref = agents.parse_agent_selector("github://brice/agents/team/alice.too@main")

    assert local.form == "name"
    assert local.name == "alice"
    assert github_short.form == "shorthand"
    assert github_short.github_owner == "brice"
    assert github_short.name == "alice"
    assert host_short.form == "shorthand"
    assert host_short.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.form == "ref"
    assert github_ref.resolved_ref().render() == "github://brice/agents/team/alice.too@main"


def test_agent_selector_parsing_supports_repo_shorthand() -> None:
    selector = agents.parse_agent_selector("brice/project/alice")

    assert selector.form == "shorthand"
    assert selector.github_owner == "brice"
    assert selector.github_repo == "project"
    assert selector.name == "alice"


def test_agent_selector_canonicalizes_raw_refs_heads_url() -> None:
    selector = agents.parse_agent_selector(
        "https://raw.githubusercontent.com/briceyan/agents/refs/heads/main/dev.too"
    )

    assert selector.form == "ref"
    assert selector.resolved_ref().render() == "github://briceyan/agents/dev.too@refs/heads/main"


def test_cli_clone_remote_shorthand_defaults_target_name(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    probes: list[str] = []

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "github://brice/agents/agents/alice.too@main"
        return "agent source-name\n"

    def fake_exists(ref: agents.GitHubAgentRef) -> bool:
        probes.append(ref.render())
        return ref.path == "agents/alice.too"

    monkeypatch.setattr(agents, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(agents, "_github_agent_ref_exists", fake_exists)
    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    result = runner.invoke(
        cli.app,
        ["clone", "brice/alice"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    program_path = toolang_root / "agents" / "alice" / "agent.too"
    assert result.stdout.strip() == f"Cloned agent alice: {program_path}"
    assert program_path.read_text(encoding="utf-8") == "agent alice\n"
    assert probes == [
        "github://brice/agents/agents/alice.too@main",
    ]


def test_cli_clone_remote_repo_shorthand_uses_named_repo(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    probes: list[str] = []

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "github://brice/project/alice.too@trunk"
        return "agent source-name\n"

    def fake_exists(ref: agents.GitHubAgentRef) -> bool:
        probes.append(ref.render())
        return ref.path == "alice.too"

    monkeypatch.setattr(agents, "_github_repo_default_branch", lambda owner, repo: "trunk")
    monkeypatch.setattr(agents, "_github_agent_ref_exists", fake_exists)
    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    result = runner.invoke(
        cli.app,
        ["clone", "brice/project/alice"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    assert (toolang_root / "agents" / "alice" / "agent.too").read_text(encoding="utf-8") == "agent alice\n"
    assert probes == [
        "github://brice/project/agents/alice.too@trunk",
        "github://brice/project/alice.too@trunk",
    ]


def test_agent_shorthand_falls_back_to_main_when_default_branch_probe_fails(monkeypatch) -> None:
    probes: list[str] = []

    def fail_default_branch(owner: str, repo: str) -> str:
        del owner, repo
        raise ValueError("rate limited")

    def fake_exists(ref: agents.GitHubAgentRef) -> bool:
        probes.append(ref.render())
        return ref.path == "dev.too"

    monkeypatch.setattr(agents, "_github_repo_default_branch", fail_default_branch)
    monkeypatch.setattr(agents, "_github_agent_ref_exists", fake_exists)

    selector = agents.parse_agent_selector("briceyan/dev")
    ref = agents.resolve_agent_selector_ref(selector)

    assert ref.render() == "github://briceyan/agents/dev.too@main"
    assert probes == [
        "github://briceyan/agents/agents/dev.too@main",
        "github://briceyan/agents/dev.too@main",
    ]


def test_agent_shorthand_error_uses_input_shape(monkeypatch) -> None:
    monkeypatch.setattr(agents, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(agents, "_github_agent_ref_exists", lambda ref: False)

    selector = agents.parse_agent_selector("briceyan/dev")

    with pytest.raises(ValueError, match="could not resolve agent shorthand: briceyan/dev"):
        agents.resolve_agent_selector_ref(selector)


def test_github_agent_fetch_uses_raw_url(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch(url: str) -> str:
        captured["url"] = url
        return "agent dev\n"

    monkeypatch.setattr(agents, "_fetch_http_text", fake_fetch)

    text = agents._fetch_github_text(
        agents.GitHubAgentRef(owner="briceyan", repo="agents", path="dev.too", rev="main")
    )

    assert text == "agent dev\n"
    assert captured["url"] == "https://raw.githubusercontent.com/briceyan/agents/main/dev.too"


def test_cli_clone_remote_url_supports_explicit_target(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "https://toolang.ai/demo/researcher.too"
        return "agent demo\n"

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    result = runner.invoke(
        cli.app,
        ["clone", "https://toolang.ai/demo/researcher.too", "researcher"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    program_path = toolang_root / "agents" / "researcher" / "agent.too"
    assert result.stdout.strip() == f"Cloned agent researcher: {program_path}"
    assert program_path.read_text(encoding="utf-8") == "agent researcher\n"


def test_cli_clone_local_source_requires_target_name(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")

    result = runner.invoke(
        cli.app,
        ["clone", "alice"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 1
    assert "target name is required when cloning one local agent" in result.stderr


def test_cli_progress_groups_agent_and_cap_steps() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 1.2

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="ok",
            detail="github://briceyan/agents/dev.too@main",
        )
    )
    progress(
        ProgressEvent(
            id="agent.fetch:github://briceyan/agents/dev.too@main",
            phase="agent.fetch",
            label="Fetch agent",
            status="ok",
            detail="github://briceyan/agents/dev.too@main",
        )
    )
    progress(
        ProgressEvent(
            id="agent.materialize:github://briceyan/agents/dev.too@main",
            phase="agent.materialize",
            label="Materialize agent",
            status="ok",
            detail="dev",
        )
    )
    progress(
        ProgressEvent(
            id="cap.resolve:psyche:briceyan/concise",
            phase="cap.resolve",
            label="Resolve psyche",
            status="ok",
            detail="github://briceyan/agents/psyches/concise.md@main",
        )
    )
    progress(
        ProgressEvent(
            id="cap.fetch:psyche:github://briceyan/agents/psyches/concise.md@main",
            phase="cap.fetch",
            label="Fetch psyche",
            status="ok",
            detail="1 file",
        )
    )
    progress(
        ProgressEvent(
            id="cap.materialize:psyche:github://briceyan/agents/psyches/concise.md@main",
            phase="cap.materialize",
            label="Materialize psyche",
            status="ok",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )

    assert stream.getvalue() == ""
    progress.finish()

    assert stream.getvalue().splitlines() == [
        "psyche briceyan/concise materialized",
        "Prepared 1 caps in 1.2s",
    ]


def test_cli_progress_mutes_cap_live_lines() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, live=True)

    progress(
        ProgressEvent(
            id="cap.resolve:skill:briceyan/pdf",
            phase="cap.resolve",
            label="Resolve skill",
            status="running",
            detail="briceyan/pdf",
        )
    )

    text = progress._live_text()

    assert text.plain.splitlines()[1] == "+ skill briceyan/pdf resolving"
    assert any(span.style == "dim" for span in text.spans)
    progress.finish(details=False)


def test_cli_progress_shows_live_summary_first() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, live=True)
    progress._started_at -= 7.6

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="dev",
        )
    )
    progress(
        ProgressEvent(
            id="cap.resolve:skill:briceyan/pdf",
            phase="cap.resolve",
            label="Resolve skill",
            status="running",
            detail="briceyan/pdf",
        )
    )

    text = progress._live_text()

    assert text.plain.splitlines() == [
        "Preparing 1 caps: 1 running, 7.6s",
        "+ skill briceyan/pdf resolving",
    ]
    assert text.spans[0].style == "dim"
    progress.finish(details=False)


def test_cli_progress_shows_agent_live_detail_only() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, live=True)
    progress._started_at -= 0.2

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="running",
            detail="briceyan/dev",
        )
    )

    text = progress._live_text()

    assert text.plain.splitlines() == ["agent briceyan/dev resolving"]
    assert text.spans[0].style == "dim"
    progress.finish(details=False)


def test_cli_progress_updates_agent_live_summary_by_phase() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, live=True)
    progress._started_at -= 0.3

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="ok",
            detail="github://briceyan/agents/dev.too@main",
        )
    )
    assert progress._live_text().plain.splitlines() == [
        "agent briceyan/dev resolved: github://briceyan/agents/dev.too@main"
    ]

    progress(
        ProgressEvent(
            id="agent.fetch:github://briceyan/agents/dev.too@main",
            phase="agent.fetch",
            label="Fetch agent",
            status="running",
            detail="https://raw.githubusercontent.com/briceyan/agents/main/dev.too",
        )
    )

    assert progress._live_text().plain.splitlines() == [
        "agent briceyan/dev fetching: https://raw.githubusercontent.com/briceyan/agents/main/dev.too"
    ]

    progress(
        ProgressEvent(
            id="agent.fetch:github://briceyan/agents/dev.too@main",
            phase="agent.fetch",
            label="Fetch agent",
            status="ok",
        )
    )

    assert progress._live_text().plain.splitlines() == ["Fetched 1 agent in 300ms"]
    progress.finish(details=False)


def test_cli_progress_failed_summary_omits_elapsed_time() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.6

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="dev",
        )
    )
    progress(
        ProgressEvent(
            id="cap.resolve:skill:briceyan/pdf",
            phase="cap.resolve",
            label="Resolve skill",
            status="failed",
            detail="not found",
        )
    )

    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Failed 1/1 caps"]


def test_cli_progress_reports_agent_resolve_failure() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="failed",
            detail="could not resolve agent shorthand: briceyan/dev",
        )
    )

    progress.finish(details=False)

    assert stream.getvalue().splitlines() == [
        "Resolve agent failed: could not resolve agent shorthand: briceyan/dev",
    ]


def test_cli_progress_formats_agent_source_stage() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.4

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="ok",
            detail="github://briceyan/agents/dev.too@main",
        )
    )
    progress(
        ProgressEvent(
            id="agent.fetch:github://briceyan/agents/dev.too@main",
            phase="agent.fetch",
            label="Fetch agent",
            status="ok",
        )
    )

    progress.finish()

    assert stream.getvalue().splitlines() == [
        "agent briceyan/dev fetched",
        "Fetched 1 agent in 400ms",
    ]


def test_cli_progress_can_finish_with_summary_only() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )
    progress.finish(details=False)

    assert stream.getvalue() == ""


def test_cli_progress_renders_summary_dimmed_on_tty() -> None:
    class TtyStringIO(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = TtyStringIO()
    progress = CliProgress(stream=stream, live=False)
    progress._started_at -= 0.2

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )
    progress.finish(details=False)

    assert "\x1b[2mPrepared 0 caps in 200ms" in stream.getvalue()


def test_cli_progress_summarizes_zero_caps_when_prepare_runs() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.2

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )
    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Prepared 0 caps in 200ms"]


def test_cli_progress_skips_output_when_prepared_state_is_cached() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.visibility:shared",
            phase="prepare.visibility",
            label="Prepare shared caps",
            status="ok",
            detail="cached",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.visibility:private",
            phase="prepare.visibility",
            label="Prepare private caps",
            status="ok",
            detail="cached",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )

    progress.finish(details=False)

    assert stream.getvalue() == ""


def test_cli_progress_can_show_cached_prepared_state() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, show_cached_prepare=True)
    progress._started_at -= 0.1

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.visibility:shared",
            phase="prepare.visibility",
            label="Prepare shared caps",
            status="ok",
            detail="cached",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.visibility:private",
            phase="prepare.visibility",
            label="Prepare private caps",
            status="ok",
            detail="cached",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )

    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Prepared caps from cache in 100ms"]


def test_cli_progress_can_use_resolved_prepare_summary() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream, prepare_summary_label="Resolved")
    progress._started_at -= 0.1

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.visibility:private",
            phase="prepare.visibility",
            label="Prepare private caps",
            status="ok",
            detail="3 entries",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )
    progress.set_prepare_total(11)

    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Resolved 11 caps in 100ms"]


def test_cli_progress_summarizes_updated_caps() -> None:
    stream = io.StringIO()
    progress = CliProgress(
        stream=stream,
        prepare_summary_label="Resolved",
        show_materialize_summary=True,
    )
    progress._started_at -= 12.0

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress(
        ProgressEvent(
            id="cap.materialize:skill:github://acme/agents/skills/review@main",
            phase="cap.materialize",
            label="Materialize skill",
            status="running",
            detail="agents/alice/.caps/ref/skills/review/SKILL.md",
        )
    )
    progress(
        ProgressEvent(
            id="cap.materialize:skill:github://acme/agents/skills/review@main",
            phase="cap.materialize",
            label="Materialize skill",
            status="ok",
        )
    )
    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="ok",
            detail="abc123",
        )
    )
    progress.set_prepare_total(12)
    assert progress._materialize_finished_at is not None
    progress._post_resolve_started_at = progress._started_at + 4.0
    progress._materialize_finished_at = progress._post_resolve_started_at + 8.0

    progress.finish(details=False)

    assert stream.getvalue().splitlines() == [
        "Resolved 12 caps in 4.0s",
        "Updated 1 caps in 8.0s",
    ]


def test_cli_progress_finish_is_idempotent() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.2

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="alice",
        )
    )
    progress.finish(details=False)
    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Preparing 0 caps: 200ms"]


def test_cli_progress_reports_interrupted_stage_once() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.3

    progress(
        ProgressEvent(
            id="agent.resolve:briceyan/dev",
            phase="agent.resolve",
            label="Resolve agent",
            status="running",
            detail="briceyan/dev",
        )
    )
    progress.interrupt()
    progress.finish(details=False)

    assert stream.getvalue().splitlines() == ["Fetch agent interrupted"]


def test_cli_progress_ignores_events_after_interrupt() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="dev",
        )
    )
    progress.interrupt()
    progress(
        ProgressEvent(
            id="cap.resolve:skill:briceyan/pdf",
            phase="cap.resolve",
            label="Resolve skill",
            status="running",
            detail="briceyan/pdf",
        )
    )
    progress.finish()

    assert stream.getvalue().splitlines() == ["Prepare caps interrupted"]


def test_cli_progress_can_list_pending_items_before_updates() -> None:
    stream = io.StringIO()
    progress = CliProgress(stream=stream)
    progress._started_at -= 0.4

    progress(
        ProgressEvent(
            id="prepare.state",
            phase="prepare.state",
            label="Prepare agent state",
            status="running",
            detail="dev",
        )
    )
    progress(
        ProgressEvent(
            id="cap.resolve:skill:by3gus/pdf-processing",
            phase="cap.resolve",
            label="Resolve skill",
            status="pending",
            detail="by3gus/pdf-processing",
        )
    )

    progress.finish()

    assert stream.getvalue().splitlines() == [
        "skill by3gus/pdf-processing pending",
        "Preparing 1 caps: 1 pending, 400ms",
    ]


def test_cli_run_supports_remote_selector(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_fetch(ref: agents.AgentRef, *, progress=None) -> str:
        del progress
        assert ref.render() == "github://brice/agents/agents/alice.too@main"
        return "agent remote-source\n"

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["toolang_root"] = startup.toolang_root
        captured["agent_name"] = startup.agent_name
        captured["port"] = startup.port
        program_path = startup.toolang_root / "agents" / startup.agent_name / "agent.too"
        captured["program_exists"] = program_path.is_file()
        captured["program_text"] = program_path.read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(agents, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(agents, "_github_agent_ref_exists", lambda ref: ref.path == "agents/alice.too")
    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)
    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 45123)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "brice/alice"],
        env={},
    )

    assert result.exit_code == 0
    assert captured["agent_name"] == "alice"
    assert captured["program_exists"] is True
    assert captured["program_text"] == "agent alice\n"
    assert captured["toolang_root"] == agents.visiting_source_root(
        toolang_root,
        source="brice/alice",
        agent_name="alice",
    )
    assert captured["port"] == 45123


def test_cli_run_supports_remote_url_selector(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_fetch(ref: agents.AgentRef, *, progress=None) -> str:
        del progress
        assert ref.render() == "https://toolang.ai/demo/researcher.too"
        return "agent researcher\n"

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["toolang_root"] = startup.toolang_root
        captured["agent_name"] = startup.agent_name
        captured["port"] = startup.port
        return 0

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)
    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 45124)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "https://toolang.ai/demo/researcher.too"],
        env={},
    )

    assert result.exit_code == 0
    assert captured["agent_name"] == "researcher"
    assert captured["toolang_root"] == agents.visiting_root(
        toolang_root,
        agents.HttpAgentRef(url="https://toolang.ai/demo/researcher.too"),
    )
    assert captured["port"] == 45124


def test_cli_run_rejects_active_resident_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://localhost:7001",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )
    monkeypatch.setattr(
        cli.agent_up,
        "prepare_agent",
        lambda **_kwargs: pytest.fail("active agents should be rejected before prepare"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice"],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent alice already running: https://too.run/7001" in result.stderr
    assert "API:" not in result.stderr
    assert "Stop:" not in result.stderr


def test_cli_run_rejects_missing_resident_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.agent_up,
        "prepare_agent",
        lambda **_kwargs: pytest.fail("missing agents should be rejected before prepare"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "missing"],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent missing not found" in result.stderr
    assert not agents.agent_home(toolang_root, "missing").exists()


def test_cli_run_interrupts_prepare_once(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")

    def interrupt_prepare(*, progress, **_kwargs) -> None:
        progress(
            ProgressEvent(
                id="prepare.state",
                phase="prepare.state",
                label="Prepare agent state",
                status="running",
                detail="alice",
            )
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.agent_up, "prepare_agent", interrupt_prepare)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice"],
        env={},
    )

    assert result.exit_code == 130
    assert result.stderr.count("Prepare caps interrupted") == 1


def test_cli_run_rejects_active_visiting_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    ref = agents.HttpAgentRef(url="https://toolang.ai/demo/researcher.too")
    visiting_root = agents.visiting_root(toolang_root, ref)
    (visiting_root / "agents" / "researcher").mkdir(parents=True, exist_ok=True)
    agents.write_runtime_state(
        visiting_root,
        "researcher",
        endpoint="http://localhost:45124",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )
    monkeypatch.setattr(agents, "fetch_agent_ref", lambda *_args, **_kwargs: "agent researcher\n")
    monkeypatch.setattr(
        cli.agent_up,
        "prepare_agent",
        lambda **_kwargs: pytest.fail("active visiting agents should be rejected before prepare"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", ref.render()],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent researcher already running: https://too.run/45124" in result.stderr
    assert "API:" not in result.stderr
    assert "Stop:" not in result.stderr


def test_active_run_error_omits_urls_for_transient_states() -> None:
    preparing = agents.AgentStatus(
        name="alice",
        status="preparing",
        endpoint="http://localhost:7001",
        api_url="http://localhost:7001/docs",
        webui_url=None,
        sandbox=None,
    )
    starting = agents.AgentStatus(
        name="alice",
        status="starting",
        endpoint="http://localhost:7001",
        api_url="http://localhost:7001/docs",
        webui_url=None,
        sandbox=None,
    )

    assert cli._active_run_error(preparing) == "Agent alice already preparing"
    assert cli._active_run_error(starting) == "Agent alice already starting"


def test_visiting_run_target_reuses_stable_root_and_program(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    ref = agents.HttpAgentRef(url=f"https://toolang.ai/demo/{uuid4().hex}/researcher.too")
    fetches: list[agents.AgentRef] = []

    def fake_fetch(fetch_ref: agents.AgentRef, **_kwargs) -> str:
        fetches.append(fetch_ref)
        return "agent old-name\n"

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    with agents.resolved_run_target(toolang_root, ref.render()) as first:
        agents.write_runtime_state(
            first.toolang_root,
            first.agent_name,
            endpoint="http://127.0.0.1:45678",
            started_at="2026-04-07T11:00:00Z",
            pid=None,
            status="stopped",
        )
        first_program = first.toolang_root / "agents" / first.agent_name / "agent.too"

    with agents.resolved_run_target(toolang_root, ref.render()) as second:
        second_program = second.toolang_root / "agents" / second.agent_name / "agent.too"

    assert first.toolang_root == agents.visiting_root(toolang_root, ref)
    assert second.toolang_root == first.toolang_root
    assert second.toolang_root.parent == Path("/tmp")
    expected_digest = hashlib.sha256(ref.render().encode("utf-8")).hexdigest()[:8]
    assert second.toolang_root.name == f"toolang-researcher-{expected_digest}"
    assert not second.toolang_root.is_relative_to(toolang_root)
    assert second.kind == "visiting"
    assert second.agent_name == "researcher"
    assert second_program == first_program
    assert second_program.read_text(encoding="utf-8") == "agent researcher\n"
    assert fetches == [ref]
    assert agents.preferred_runtime_port(second.toolang_root, second.agent_name) == 45678


def test_visiting_root_ignores_local_toolang_root(tmp_path: Path) -> None:
    ref = agents.HttpAgentRef(url="https://toolang.ai/demo/researcher.too")

    assert agents.visiting_root(tmp_path / "one", ref) == agents.visiting_root(tmp_path / "two", ref)


def test_visiting_run_target_reuses_shorthand_cache_without_resolving(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    selector_text = f"briceyan/{uuid4().hex}"
    agent_name = selector_text.split("/", 1)[1]
    run_root = agents.visiting_source_root(toolang_root, source=selector_text, agent_name=agent_name)
    program_path = run_root / "agents" / agent_name / "agent.too"
    program_path.parent.mkdir(parents=True, exist_ok=True)
    program_path.write_text(f"agent {agent_name}\n", encoding="utf-8")
    monkeypatch.setattr(
        agents,
        "resolve_agent_selector_ref",
        lambda *_args, **_kwargs: pytest.fail("fresh visiting cache should not resolve"),
    )

    with agents.resolved_run_target(toolang_root, selector_text) as target:
        assert target.toolang_root == run_root
        assert target.agent_name == agent_name
        assert target.kind == "visiting"


def test_visiting_run_target_refetches_stale_program_cache(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    ref = agents.HttpAgentRef(url=f"https://toolang.ai/demo/{uuid4().hex}/researcher.too")
    fetched_sources = iter(("agent old-name\n", "agent newer-name\n"))
    fetches: list[agents.AgentRef] = []

    def fake_fetch(fetch_ref: agents.AgentRef, **_kwargs) -> str:
        fetches.append(fetch_ref)
        return next(fetched_sources)

    monkeypatch.setattr(agents, "VISITING_PROGRAM_CACHE_TTL_SEC", 60)
    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    with agents.resolved_run_target(toolang_root, ref.render()) as first:
        program_path = first.toolang_root / "agents" / first.agent_name / "agent.too"

    stale_time = time.time() - 120
    os.utime(program_path, (stale_time, stale_time))

    with agents.resolved_run_target(toolang_root, ref.render()) as second:
        refreshed_program = second.toolang_root / "agents" / second.agent_name / "agent.too"

    assert refreshed_program == program_path
    assert refreshed_program.read_text(encoding="utf-8") == "agent researcher\n"
    assert fetches == [ref, ref]


def test_roaming_materialize_links_source_and_toolang_config(tmp_path: Path) -> None:
    program_path = _write_roaming_program(tmp_path, "agent demo\n\nthunk:\n  First")
    (tmp_path / "toolang.toml").write_text("[models]\ndefault = \"test/model\"\n", encoding="utf-8")

    toolang_root, agent_name = agents.materialize_roaming_program(program_path)

    assert agent_name == "demo"
    agent_home = toolang_root / "agents" / "demo"
    materialized_program = agent_home / "agent.too"
    materialized_config = agent_home / "config.toml"
    assert materialized_program.is_symlink()
    assert materialized_config.is_symlink()
    assert (materialized_program.parent / os.readlink(materialized_program)).resolve() == program_path.resolve()
    assert (materialized_config.parent / os.readlink(materialized_config)).resolve() == (tmp_path / "toolang.toml").resolve()

    program_path.write_text("agent demo\n\nthunk:\n  Second\n", encoding="utf-8")
    assert "Second" in materialized_program.read_text(encoding="utf-8")


def test_roaming_materialize_removes_stale_config_symlink(tmp_path: Path) -> None:
    program_path = _write_roaming_program(tmp_path, "agent demo")
    config_path = tmp_path / "toolang.toml"
    config_path.write_text("[models]\ndefault = \"test/model\"\n", encoding="utf-8")

    toolang_root, _agent_name = agents.materialize_roaming_program(program_path)
    config_link = toolang_root / "agents" / "demo" / "config.toml"
    assert config_link.is_symlink()

    config_path.unlink()
    agents.materialize_roaming_program(program_path)

    assert not config_link.exists()
    assert not config_link.is_symlink()


def test_cli_roaming_program_help_lists_available_targets(capsys, tmp_path: Path) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.

thunk summarize(in: Part[], style?):
  Summarize the current workspace in a concise style.

flow review(in: Text):
  each: Review one item.
""".strip(),
    )

    original_argv = list(cli.sys.argv)
    cli.sys.argv = ["toolang"]
    try:
        result = cli.main([str(program_path), "--help"])
    finally:
        cli.sys.argv = original_argv
    captured = capsys.readouterr()

    assert result == 0
    assert "Usage: toolang" in captured.out
    assert "SCRIPT TARGET [OPTIONS] [PARAMS] [INPUT]..." in captured.out
    assert "Invoke a thunk or flow from a Toolang script." in captured.out
    assert "Script:" in captured.out
    assert "* SCRIPT" not in captured.out
    assert program_path.name in captured.out
    assert "Options" in captured.out
    assert "--models" in captured.out
    assert "Limit available models. Pass CSV or repeat." in captured.out
    assert "--tools" in captured.out
    assert "Allow selected tools. Pass CSV or repeat." in captured.out
    assert "--caps" in captured.out
    assert "Allow selected caps. Pass CSV or repeat." in captured.out
    assert captured.out.index("--models") < captured.out.index("--tools") < captured.out.index("--caps")
    assert "--quiet" in captured.out
    assert "Params" in captured.out
    assert "NAME=VALUE" in captured.out
    assert "Input" in captured.out
    assert "@PATH" in captured.out
    assert "@PATH.md" not in captured.out
    assert "@PATH.png" not in captured.out
    assert "@PATH.mp3" not in captured.out
    assert "Modality is inferred from the extension." in captured.out
    assert "Multimodal message input" not in captured.out
    assert "Targets" in captured.out
    assert "default" in captured.out
    assert "summarize" in captured.out
    assert "review" in captured.out
    assert captured.out.index("Options") < captured.out.index("Targets") < captured.out.index("Params") < captured.out.index("Input")


def test_cli_roaming_thunk_help_is_dynamic(capsys, tmp_path: Path) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk summarize(in: Part[], style?, audience?):
  Summarize the current workspace in a concise style.
""".strip(),
    )

    original_argv = list(cli.sys.argv)
    cli.sys.argv = ["toolang"]
    try:
        result = cli.main([str(program_path), "summarize", "--help"])
    finally:
        cli.sys.argv = original_argv
    captured = capsys.readouterr()

    assert result == 0
    assert "Usage: toolang" in captured.out
    assert "SCRIPT TARGET" in captured.out
    assert "Summarize the current workspace in a concise style." in captured.out
    assert "Script:" in captured.out
    assert program_path.name in captured.out
    assert "Thunk:  summarize" in captured.out
    assert "* TARGET" not in captured.out
    assert "summarize" in captured.out
    assert "[OPTIONS]" in captured.out
    assert "[PARAMS]" in captured.out
    assert "style=TEXT" in captured.out
    assert "audience=TEXT" in captured.out
    assert "[INPUT]..." in captured.out
    assert "--models" in captured.out
    assert "Limit available models. Pass CSV or repeat." in captured.out
    assert "--tools" in captured.out
    assert "Allow selected tools. Pass CSV or repeat." in captured.out
    assert "--caps" in captured.out
    assert "Allow selected caps. Pass CSV or repeat." in captured.out
    assert captured.out.index("--models") < captured.out.index("--tools") < captured.out.index("--caps")
    assert "--quiet" in captured.out
    assert "Params" in captured.out
    assert "Input" in captured.out
    assert "Multimodal message input" not in captured.out
    assert "@PATH" in captured.out
    assert "@PATH.md" not in captured.out
    assert "@PATH.png" not in captured.out
    assert "@PATH.mp3" not in captured.out
    assert "Modality is inferred from the extension." in captured.out
    assert "Thunks" not in captured.out
    assert captured.out.index("Options") < captured.out.index("Params") < captured.out.index("Input")


def test_cli_roaming_invoke_passes_default_thunk_params_and_parts(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[], tone?, retries?: Number, dry_run?: Boolean):
  Rewrite the input using the provided controls.
""".strip(),
    )
    attachment = tmp_path / "image.png"
    attachment.write_bytes(b"png")
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del environ, response
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["thunk_name"] = thunk_name
        captured["input_text"] = input_text
        captured["models"] = models
        captured["metadata"] = dict(metadata or {})
        captured["log_spec"] = log_spec
        captured["prepared_state"] = prepared_state

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main(
        [
            str(program_path),
            "--models",
            "gpt-5",
            "default",
            "rewrite this",
            f"@{attachment}",
            "tone=concise",
            "retries=3",
            "dry_run=true",
            "--models",
            "o3",
        ]
    )
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["agent_name"] == "demo"
    assert captured["toolang_root"] == program_path.parent / ".toolang"
    assert captured["thunk_name"] == "default"
    assert captured["models"] == ("gpt-5", "o3")
    assert captured["log_spec"] is None
    assert captured["prepared_state"] is not None
    assert "rewrite this" in cast(str, captured["input_text"])
    assert str(attachment.resolve()) in cast(str, captured["input_text"])
    assert captured["metadata"] == {
        "invoke_params": {
            "tone": "concise",
            "retries": 3,
            "dry_run": True,
        },
        "invoke_parts": [
            {"type": "text", "text": "rewrite this"},
            {"type": "image", "path": str(attachment.resolve())},
        ],
    }


def test_cli_roaming_invoke_passes_explicit_tool_selectors(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[]):
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        tools: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models
        del metadata, environ, response, log_spec, prepared_state
        captured["tools"] = tools

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main(
        [
            str(program_path),
            "--tools",
            "filesystem,shell",
            "default",
            "hello",
            "--tools",
            "service_use",
        ]
    )
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["tools"] == ("filesystem", "shell", "service_use")


def test_cli_roaming_invoke_passes_explicit_cap_selectors(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[]):
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        caps: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models
        del metadata, environ, response, log_spec, prepared_state
        captured["caps"] = caps

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main(
        [
            str(program_path),
            "--caps",
            "skill/reviewer,service/*[home]",
            "default",
            "hello",
            "--caps",
            "[here]",
        ]
    )
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["caps"] == ("skill/reviewer", "service/*[home]", "[here]")


def test_cli_roaming_invoke_quiet_after_thunk_suppresses_progress_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models, metadata, environ, log_spec, prepared_state
        captured["response"] = response

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)
    monkeypatch.setattr(cli.cli_invoke.sys.stderr, "isatty", lambda: True)

    result = cli.main([str(program_path), "default", "--quiet", "hello"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert output.err == ""
    assert captured["response"] is not None


def test_cli_roaming_invoke_uses_progress_sink_for_tty_stderr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models, metadata, environ, log_spec, prepared_state
        captured["response"] = response

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)
    monkeypatch.setattr(cli.cli_invoke.sys.stderr, "isatty", lambda: True)

    result = cli.main([str(program_path), "default", "hello"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["response"] is not None


def test_cli_roaming_invoke_passes_prepare_progress_for_tty_stderr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}
    real_prepare_agent = cli.cli_invoke.agent_up.prepare_agent

    def fake_prepare_agent(*, toolang_root: Path, agent_name: str, progress=None):
        captured["prepare_progress"] = progress
        return real_prepare_agent(
            toolang_root=toolang_root,
            agent_name=agent_name,
            progress=progress,
        )

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models, metadata, environ, response, log_spec, prepared_state

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "prepare_agent", fake_prepare_agent)
    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)
    monkeypatch.setattr(cli.cli_invoke.sys.stderr, "isatty", lambda: True)

    result = cli.main([str(program_path), "default", "hello"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["prepare_progress"] is not None


def test_cli_roaming_invoke_suppresses_prepare_progress_when_quiet(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}
    real_prepare_agent = cli.cli_invoke.agent_up.prepare_agent

    def fake_prepare_agent(*, toolang_root: Path, agent_name: str, progress=None):
        captured["prepare_progress"] = progress
        return real_prepare_agent(
            toolang_root=toolang_root,
            agent_name=agent_name,
            progress=progress,
        )

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models, metadata, environ, response, log_spec, prepared_state

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "prepare_agent", fake_prepare_agent)
    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)
    monkeypatch.setattr(cli.cli_invoke.sys.stderr, "isatty", lambda: True)

    result = cli.main([str(program_path), "default", "hello", "-q"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert output.err == ""
    assert captured["prepare_progress"] is None


def test_cli_roaming_invoke_handles_keyboard_interrupt_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, input_text, models, metadata, environ, log_spec, prepared_state
        response.on_event(
            RunStart(
                run_id="run_test",
                origin="script",
                thread_id="script_test",
                input=Message.user("hello"),
                created_at="2026-05-21T00:00:00Z",
                started_at="2026-05-21T00:00:00Z",
            )
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)
    monkeypatch.setenv(PY_LOG_ENV_VAR, "toolang.run=debug")

    result = cli.main([str(program_path), "default", "hello"])
    output = capsys.readouterr()

    assert result == 130
    assert output.err.splitlines() == [
        "toolang interrupted",
        "Run: run_test",
        f"Log: {agents.agent_script_run_log_path(program_path.parent / '.toolang', 'demo', thunk_name='default', run_id='run_test')}",
    ]
    assert "Traceback" not in output.err


def test_cli_roaming_invoke_reports_missing_models_without_run_id(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")

    result = cli.main([str(program_path), "default", "hello"])
    output = capsys.readouterr()

    assert result == 1
    assert "toolang error: No available models." in output.err
    assert "toolang model providers" in output.err
    assert "Run: run_" not in output.err


def test_cli_roaming_invoke_requires_explicit_target_name(tmp_path: Path, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    result = cli.main([str(program_path)])
    output = capsys.readouterr()

    assert result == 0
    assert "SCRIPT TARGET [OPTIONS] [PARAMS] [INPUT]..." in output.out
    assert "Targets" in output.out


def test_cli_roaming_invoke_requires_part_for_message_input(tmp_path: Path, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk summarize(in: Part[]):
  Summarize the current workspace in a concise style.
""".strip(),
    )

    result = cli.main([str(program_path), "summarize"])
    output = capsys.readouterr()

    assert result == 0
    assert "Usage:" in output.out
    assert "SCRIPT TARGET [OPTIONS] [INPUT]..." in output.out
    assert "Summarize the current workspace in a concise style." in output.out
    assert "Thunk:  summarize" in output.out
    assert output.err == ""


def test_cli_roaming_invoke_rejects_unknown_target_name(tmp_path: Path, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )

    result = cli.main([str(program_path), "summarize"])
    output = capsys.readouterr()

    assert result == 1
    assert "unknown target: summarize" in output.err


def test_cli_roaming_invoke_passes_flow_executable_kind(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
flow review(in: Text):
  each: Normalize the item.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, models, environ, response, log_spec, prepared_state
        captured["thunk_name"] = thunk_name
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main([str(program_path), "review", "one", "two"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["thunk_name"] == "review"
    assert captured["input_text"] == "one\n\ntwo"
    assert captured["metadata"] == {
        "invoke_params": {},
        "invoke_parts": [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ],
        "executable_kind": "flow",
    }


def test_cli_roaming_invoke_supports_end_of_options_separator(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, models, environ, response, log_spec, prepared_state
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main(
        [
            str(program_path),
            "default",
            "--",
            "--leading-text",
            "@@literal-at",
        ]
    )
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["input_text"] == "--leading-text\n\n@literal-at"
    assert captured["metadata"] == {
        "invoke_params": {},
        "invoke_parts": [
            {"type": "text", "text": "--leading-text"},
            {"type": "text", "text": "@literal-at"},
        ],
    }


def test_cli_roaming_invoke_treats_unknown_name_equals_value_as_message_part(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[], tone?):
  Reply directly.
""".strip(),
    )
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, models, environ, response, log_spec, prepared_state
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main([str(program_path), "default", "style=concise", "tone=direct"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["input_text"] == "style=concise"
    assert captured["metadata"] == {
        "invoke_params": {
            "tone": "direct",
        },
        "invoke_parts": [
            {"type": "text", "text": "style=concise"},
        ],
    }


def test_cli_roaming_invoke_reads_md_path_as_text_part(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[]):
  Reply directly.
""".strip(),
    )
    note = tmp_path / "note.md"
    note.write_text("# Title\n\nBody text.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, models, environ, response, log_spec, prepared_state
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main([str(program_path), "default", f"@{note}"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["input_text"] == "# Title\n\nBody text.\n"
    assert captured["metadata"] == {
        "invoke_params": {},
        "invoke_parts": [
            {"type": "text", "text": "# Title\n\nBody text.\n", "path": str(note.resolve())},
        ],
    }


def test_cli_roaming_invoke_reads_mdx_path_as_text_part(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[]):
  Reply directly.
""".strip(),
    )
    note = tmp_path / "note.mdx"
    note.write_text("# Title\n\n<Callout>Body text.</Callout>\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, models, environ, response, log_spec, prepared_state
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main([str(program_path), "default", f"@{note}"])
    output = capsys.readouterr()

    text = "# Title\n\n<Callout>Body text.</Callout>\n"
    assert result == 0
    assert output.out.strip() == "done"
    assert captured["input_text"] == text
    assert captured["metadata"] == {
        "invoke_params": {},
        "invoke_parts": [
            {"type": "text", "text": text, "path": str(note.resolve())},
        ],
    }


def test_cli_roaming_invoke_passes_video_path_part(tmp_path: Path, monkeypatch, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk(in: Part[]):
  Reply directly.
""".strip(),
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    captured: dict[str, object] = {}

    def fake_invoke(
        *,
        toolang_root: Path,
        agent_name: str,
        thunk_name: str | None,
        input_text: str | None,
        models: tuple[str, ...],
        metadata: dict[str, object] | None,
        environ: dict[str, str],
        response,
        log_spec: str | None = None,
        prepared_state=None,
    ):
        del toolang_root, agent_name, thunk_name, models, environ, response, log_spec, prepared_state
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            run_id = "run_test"
            status = "finished"
            output_text = "done"
            error = None
            log_path = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main([str(program_path), "default", f"@{video}"])
    output = capsys.readouterr()

    assert result == 0
    assert output.out.strip() == "done"
    assert captured["input_text"] == f"Attached video: {video.resolve()}"
    assert captured["metadata"] == {
        "invoke_params": {},
        "invoke_parts": [
            {"type": "video", "path": str(video.resolve())},
        ],
    }


def test_cli_start_rejects_remote_selector(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "brice/alice"],
        env={},
    )

    assert result.exit_code == 1
    assert "start only supports local agent names; clone the remote source first" in result.stderr


def test_cli_start_rejects_missing_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.agent_up,
        "resolve_startup",
        lambda **_kwargs: pytest.fail("missing agents should be rejected before startup resolution"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "missing"],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent missing not found" in result.stderr
    assert not agents.agent_home(toolang_root, "missing").exists()


def test_cli_remove_deletes_stopped_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "remove", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "Removed agent alice"
    assert not (toolang_root / "agents" / "alice").exists()


def test_cli_remove_rejects_active_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "remove", "alice"],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent alice already running: https://too.run/8765" in result.stderr


def test_cli_remove_rejects_orphan_runtime_process(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    monkeypatch.setattr(agents, "agent_runtime_process_pids", lambda *_args: (12345,))

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "remove", "alice"],
        env={},
    )

    assert result.exit_code == 1
    assert "Agent alice already running" in result.stderr
    assert (toolang_root / "agents" / "alice").is_dir()


def test_cli_stop_stops_orphan_runtime_process_without_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    stopped: list[tuple[int, bool]] = []
    monkeypatch.setattr(agents, "agent_runtime_process_pids", lambda *_args: (12345,))
    monkeypatch.setattr(
        agents,
        "_stop_pid",
        lambda pid, *, force: stopped.append((pid, force)) or True,
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "stop", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "Stopped agent alice"
    assert stopped == [(12345, False)]


def test_cli_list_shows_agent_status_and_webui_url(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.create_agent(toolang_root, "bob")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )

    result = runner.invoke(
        cli.app,
        ["list"],
        env={
            "TOOLANG_ROOT": str(toolang_root),
            "TOOLANG_UI_BASE_URL": "https://ui.example/agents",
        },
    )

    assert result.exit_code == 0
    assert "AGENT" in result.stdout
    assert "STATUS" in result.stdout
    assert "SANDBOX" in result.stdout
    assert "PORT" in result.stdout
    assert "API" not in result.stdout
    assert "WEBUI" in result.stdout
    assert "alice" in result.stdout
    assert "running" in result.stdout
    assert "none" in result.stdout
    assert "8765" in result.stdout
    assert "http://127.0.0.1:8765/docs" not in result.stdout
    assert "https://ui.example/agents/8765" in result.stdout
    assert "bob" in result.stdout
    assert "stopped" in result.stdout
    assert "-" in result.stdout


def test_cli_list_shows_managed_sandbox_selector(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": "sandbox-alice",
            "meta": {},
        },
        status="starting",
    )

    result = runner.invoke(
        cli.app,
        ["list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "failed" in result.stdout
    assert "docker:python:3.13-slim" not in result.stdout


def test_cli_list_uses_ui_base_url_from_root_config(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )
    (toolang_root / "config.toml").write_text(
        '[web]\n'
        'ui_base_url = "https://agents.example.test"\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "https://agents.example.test/8765" in result.stdout


def test_cli_list_reads_web_config_without_validating_experiments_caps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )
    (toolang_root / "config.toml").write_text(
        '[web]\n'
        'ui_base_url = "http://localhost:3000"\n'
        '\n'
        '[skills]\n'
        'pdf-processing = { ref = "github://by3gus/agent-skills/skills/pdf-processing@main" }\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "http://localhost:3000/8765" in result.stdout


def test_cli_info_shows_agent_details(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    (toolang_root / "agents" / "alice" / "config.toml").write_text(
        '[models]\n'
        'default = ["o3", "gpt-5"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openai": _FakeModelProvider(
                name="openai",
                models=(
                    ModelInfo(
                        ref="openai/o3",
                        provider="openai",
                        name="o3",
                        model="o3",
                        selectors=("o3", "openai/o3"),
                        adapter="responses",
                    ),
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="shared",
        kind="skill",
        name="hello",
        text="---\ndescription: Say hello.\n---\n# Hello\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="service",
        name="github",
        text=(
            "---\n"
            "description: Example MCP service\n"
            "transport: http\n"
            "target: https://example.com/mcp\n"
            "---\n"
        ),
    )
    work.create_task_text(
        toolang_root,
        "alice",
        "---\ntitle: Review\n---\n\nReview this change.\n",
    )
    work.create_chore_text(
        toolang_root,
        "alice",
        "---\ntitle: Sync\nschedule: FREQ=HOURLY;INTERVAL=1\n---\n\nSync the service.\n",
    )
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
        components=("router.chat", "runner.chat", "trigger.pulse"),
        status="running",
        message="ready",
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "██████████" in result.stdout
    assert "████" in result.stdout
    assert "alice" in result.stdout
    assert "-----" in result.stdout
    assert "Home" in result.stdout
    assert result.stdout.index("██████████") < result.stdout.index("ALICE") < result.stdout.index("Home")
    assert str(toolang_root / "agents" / "alice") in "".join(result.stdout.split())
    assert "ROOM" not in result.stdout
    assert "PROGRAM" not in result.stdout
    assert "RUNTIME" not in result.stdout
    assert "LOG" not in result.stdout
    assert "PULSE" not in result.stdout
    assert "Caps" in result.stdout
    assert "1 skill" in result.stdout
    assert "0 psyches" in result.stdout
    assert "1 service" in result.stdout
    assert "0 prompts" in result.stdout
    assert "Jobs" in result.stdout
    assert "1 chore" in result.stdout
    assert "1 task" in result.stdout
    assert "Models" in result.stdout
    assert "2 models, 1 provider" in result.stdout
    assert "Tools" in result.stdout
    assert "2 tools, 2 sets" in result.stdout
    assert result.stdout.index("Tools") < result.stdout.index("Models")
    assert "Status" in result.stdout
    assert "running (up a day)" in result.stdout
    assert "Sandbox" in result.stdout
    assert "none" in result.stdout
    assert "Components" not in result.stdout
    assert "router.chat, runner.chat, trigger.pulse" not in result.stdout
    assert "PID" in result.stdout
    assert str(os.getpid()) in result.stdout
    assert "Started" in result.stdout
    assert "2026-04-07T11:00:00Z" in result.stdout
    assert "Created" not in result.stdout
    assert "ONLINE" not in result.stdout
    assert "ENDPOINT" not in result.stdout
    assert "API" in result.stdout
    assert "http://127.0.0.1:8765" in result.stdout
    assert "http://127.0.0.1:8765/docs" not in result.stdout
    assert "WebUI" in result.stdout
    assert "https://too.run/8765" in result.stdout
    assert "Updated" not in result.stdout
    assert result.stdout.index("PID") < result.stdout.index("API")
    assert result.stdout.index("WebUI") < result.stdout.index("Started")


def test_cli_info_console_uses_terminal_width(monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "72")

    assert cli_utils._INFO_CONSOLE.width == 72


def test_cli_info_narrow_layout_separates_avatar_from_table(monkeypatch) -> None:
    output = io.StringIO()
    console = cli_utils.Console(file=output, width=80, highlight=False, color_system=None)
    monkeypatch.setattr(cli_utils, "_INFO_CONSOLE", console)

    cli_utils._echo_pairs_table([("Home", "x")], avatar="AA\nBB", title="DEV")

    assert output.getvalue().startswith("AA\nBB\n\nDEV\n---\nHome x")


def test_cli_info_wide_layout_aligns_avatar_with_table(monkeypatch) -> None:
    output = io.StringIO()
    console = cli_utils.Console(file=output, width=120, highlight=False, color_system=None)
    monkeypatch.setattr(cli_utils, "_INFO_CONSOLE", console)

    cli_utils._echo_pairs_table([("Home", "x"), ("Caps", "y")], avatar="AA\nBB", title="DEV")

    assert "AA    Home x" in output.getvalue()


def test_cli_info_avatar_uses_rainbow_style() -> None:
    avatar = cli_logo._rainbow_text(" A\nB ")

    assert avatar.plain == " A\nB "
    assert avatar.style == ""
    assert [(span.start, span.end, span.style) for span in avatar.spans] == [
        (1, 2, "bold #9edb49"),
        (3, 4, "bold #f4d35e"),
    ]


def test_cli_info_avatar_matches_logo_proportions() -> None:
    avatar = cli_utils._agent_avatar().plain
    lines = avatar.splitlines()

    assert len(lines) == 7
    assert {len(line) for line in lines} == {36}
    assert lines[0].startswith(" ██████████")
    assert lines[0].endswith("████ ")
    assert "▄▄▄▄     ▄▄▄▄" in avatar
    assert "▄▄▄▄████" in avatar


def test_cli_info_reads_cap_counts_from_prepared_locks(tmp_path: Path, monkeypatch) -> None:
    from toolang.state.prepared import write_prepared_lock

    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="shared",
        kind="skill",
        name="hello",
        text="---\ndescription: Say hello.\n---\n# Hello\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="service",
        name="github",
        text=(
            "---\n"
            "description: Example MCP service\n"
            "transport: http\n"
            "target: https://example.com/mcp\n"
            "---\n"
        ),
    )
    durable = caps.scan_durable_state(toolang_root, "alice")
    shared_lock, shared_files = caps.build_visibility_lock(durable, visibility="shared")
    private_lock, private_files = caps.build_visibility_lock(durable, visibility="private")
    write_prepared_lock(toolang_root, shared_lock, files=shared_files)
    write_prepared_lock(toolang_root, private_lock, files=private_files)

    def fail_list_entries(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("info should use prepared locks for cap counts")

    monkeypatch.setattr(caps, "list_entries", fail_list_entries)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "0 psyches" in result.stdout
    assert "1 skill" in result.stdout
    assert "1 service" in result.stdout
    assert "0 prompts" in result.stdout


def test_cli_info_rebuilds_missing_prepared_lock(tmp_path: Path, monkeypatch) -> None:
    import shutil

    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="hello",
        text="---\ndescription: Say hello.\n---\n# Hello\n",
    )
    prepared = cli.agent_up.prepare_agent(toolang_root=toolang_root, agent_name="alice")
    private_lock_path = prepared.private_lock.lock_path
    assert private_lock_path.is_file()
    shutil.rmtree(private_lock_path.parent)

    def fail_list_entries(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("info should rebuild prepared locks instead of scanning entries")

    monkeypatch.setattr(caps, "list_entries", fail_list_entries)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "1 skill" in result.stdout
    assert private_lock_path.is_file()
    assert "Prepared 1 cap" in result.stderr


def test_cli_info_for_stopped_agent_shows_created_only(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
        components=("router.chat", "runner.chat", "trigger.pulse"),
        status="running",
    )
    agents.stop_runtime_state(toolang_root, "alice")

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "██████████" in result.stdout
    assert "Status" in result.stdout
    assert "AGENT" not in result.stdout
    assert "stopped" in result.stdout
    assert "Created" in result.stdout
    assert "Sandbox" not in result.stdout
    assert "Components" not in result.stdout
    assert "Started" not in result.stdout
    assert "Updated" not in result.stdout
    assert "ENDPOINT" not in result.stdout
    assert "API" not in result.stdout
    assert "WebUI" not in result.stdout
    assert "PID" not in result.stdout


def test_cli_info_for_running_docker_sandbox_shows_container_pid(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    monkeypatch.setattr(agents, "docker_container_running", lambda _name: True)
    monkeypatch.setattr(
        agents,
        "docker_container_identity",
        lambda _name: ("abcdef1234567890fedcba", 4321),
    )
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=None,
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": "toolang-alice",
            "meta": {},
        },
        components=("router.chat", "runner.chat", "trigger.pulse"),
        status="running",
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "PID" in result.stdout
    assert "abcdef123456:4321" in result.stdout
    assert result.stdout.index("PID") < result.stdout.index("API")


def test_cli_info_prefers_runtime_models_for_active_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    (toolang_root / "agents" / "alice" / "config.toml").write_text(
        '[models]\n'
        'default = ["o3", "gpt-5"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "anthropic": _FakeModelProvider(
                name="anthropic",
                models=(
                    ModelInfo(
                        ref="anthropic/claude",
                        provider="anthropic",
                        name="claude",
                        model="claude",
                        selectors=("claude", "anthropic/claude"),
                        adapter="responses",
                    ),
                ),
            ),
            "openai": _FakeModelProvider(
                name="openai",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
    )
    monkeypatch.setattr(cli.agent_up, "load_tool_plugins", lambda *, config=None: {})
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
        components=("router.chat", "runner.chat"),
        models=("claude", "gpt-5"),
        status="running",
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "info", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert "Models" in result.stdout
    assert "2 models, 2 providers" in result.stdout


def test_cli_channel_list_shows_installed_channels(monkeypatch) -> None:
    def fake_list_plugin_infos(*, group: str) -> list[cli.agent_up.PluginInfo]:
        assert group == "toolang.channel"
        return [cli.agent_up.PluginInfo(name="telegram", source="external")]

    monkeypatch.setattr(cli.agent_up, "list_plugin_infos", fake_list_plugin_infos)

    result = runner.invoke(cli.app, ["channel", "list"])

    assert result.exit_code == 0
    assert "CHANNEL" in result.stdout
    assert "SOURCE" in result.stdout
    assert "telegram" in result.stdout
    assert "external" in result.stdout


def test_cli_sandbox_list_shows_installed_sandboxes(monkeypatch) -> None:
    def fake_list_plugin_infos(*, group: str) -> list[cli.agent_up.PluginInfo]:
        assert group == "toolang.sandbox"
        return [
            cli.agent_up.PluginInfo(name="docker", source="external"),
            cli.agent_up.PluginInfo(name="none", source="built-in"),
        ]

    monkeypatch.setattr(cli.agent_up, "list_plugin_infos", fake_list_plugin_infos)

    result = runner.invoke(cli.app, ["sandbox", "list"])

    assert result.exit_code == 0
    assert "SANDBOX" in result.stdout
    assert "SOURCE" in result.stdout
    assert "docker" in result.stdout
    assert "none" in result.stdout
    assert "built-in" in result.stdout
    assert "external" in result.stdout


def test_cli_tool_list_shows_installed_tool_plugin_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )

    def fake_list_plugin_infos(*, group: str) -> list[cli.agent_up.PluginInfo]:
        assert group == "toolang.tool"
        return [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ]

    monkeypatch.setattr(cli.agent_up, "list_plugin_infos", fake_list_plugin_infos)

    result = runner.invoke(cli.app, ["tool", "list"])

    assert result.exit_code == 0
    assert "SET" in result.stdout
    assert "TOOL" in result.stdout
    assert "DESCRIPTION" in result.stdout
    assert "filesystem" in result.stdout
    assert "read_text" in result.stdout
    assert "shell" in result.stdout
    assert "execute" in result.stdout
    assert "PLUGIN" not in result.stdout
    assert "SOURCE" not in result.stdout
    assert "filesystem__read_text" not in result.stdout
    assert "shell__execute" not in result.stdout
    assert "2 tools, 2 toolsets" in result.stdout


def test_cli_tool_list_filters_by_tool_selector(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )

    result = runner.invoke(cli.app, ["tool", "list", "--select", "shell/*"])

    assert result.exit_code == 0
    assert "shell" in result.stdout
    assert "execute" in result.stdout
    assert "filesystem" not in result.stdout
    assert "read_text" not in result.stdout
    assert "1 tool, 1 toolset" in result.stdout


def test_cli_tool_list_filters_by_cross_namespace_tool_selector(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )

    result = runner.invoke(cli.app, ["tool", "list", "--select", "*/execute"])

    assert result.exit_code == 0
    assert "shell" in result.stdout
    assert "execute" in result.stdout
    assert "filesystem" not in result.stdout
    assert "read_text" not in result.stdout
    assert "1 tool, 1 toolset" in result.stdout


def test_cli_tool_list_bare_pattern_matches_tool_name_not_toolset(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )

    bare_result = runner.invoke(cli.app, ["tool", "list", "--filter", "shell"])
    name_result = runner.invoke(cli.app, ["tool", "list", "--filter", "execute"])

    assert bare_result.exit_code == 0
    assert "No matched tools." in bare_result.stdout
    assert name_result.exit_code == 0
    assert "shell" in name_result.stdout
    assert "execute" in name_result.stdout
    assert "filesystem" not in name_result.stdout


def test_cli_tool_list_filters_by_plugin_filter(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "issues__search": _FakeLoadedTool(
                plugin_name="tracker",
                leaf_name="search",
                description="Search issues.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="tracker", source="external"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )

    result = runner.invoke(cli.app, ["tool", "list", "--filter", "*[plugin:tracker]"])

    assert result.exit_code == 0
    assert "tracker" in result.stdout
    assert "search" in result.stdout
    assert "shell" not in result.stdout
    assert "execute" not in result.stdout


def test_cli_tool_list_supports_filter_short_option(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
            "shell__execute": _FakeLoadedTool(
                plugin_name="shell",
                leaf_name="execute",
                description="Run one shell command.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [
            cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
            cli.agent_up.PluginInfo(name="shell", source="external"),
        ],
    )

    result = runner.invoke(cli.app, ["tool", "list", "-f", "execute"])

    assert result.exit_code == 0
    assert "shell" in result.stdout
    assert "execute" in result.stdout
    assert "filesystem" not in result.stdout


def test_cli_tool_list_reports_no_matched_tools_for_empty_filter(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_tool_plugins",
        lambda *, config=None: {
            "filesystem__read_text": _FakeLoadedTool(
                plugin_name="filesystem",
                leaf_name="read_text",
                description="Read one text file.",
            ),
        },
    )
    monkeypatch.setattr(
        cli.agent_up,
        "list_plugin_infos",
        lambda *, group: [cli.agent_up.PluginInfo(name="filesystem", source="built-in")],
    )

    result = runner.invoke(cli.app, ["tool", "list", "--select", "shell/*"])

    assert result.exit_code == 0
    assert "No matched tools." in result.stdout
    assert "toolang tool list --select <selector>" in result.stdout


def test_cli_model_list_shows_discovered_models(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openai": _FakeModelProvider(
                name="openai",
                required_env=("OPENAI_API_KEY",),
                base_url="https://api.openai.com/v1",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                        tools=True,
                        streaming=True,
                        details="Built-in OpenAI route.",
                    ),
                ),
            ),
            "openrouter": _FakeModelProvider(
                name="openrouter",
                required_env=("OPENROUTER_API_KEY",),
                base_url="https://openrouter.ai/api/v1",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openrouter",
                        name="gpt-5",
                        model="openai/gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                        tools=True,
                        streaming=True,
                        details="Built-in OpenRouter route.",
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "list"],
        env={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": ""},
    )

    assert result.exit_code == 0
    assert "MODEL" in result.stdout
    assert "PROVIDER" in result.stdout
    assert "SCOPE" not in result.stdout
    assert "PROFILE" in result.stdout
    assert "1 model, 1 provider" in result.stdout
    assert "openai" in result.stdout
    assert "openrouter" not in result.stdout
    assert "openai/gpt-5" in result.stdout
    assert "streaming=y" in result.stdout
    assert "tools=y" in result.stdout


def test_cli_model_providers_orders_config_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openai": _FakeModelProvider(
                name="openai",
                required_env=("OPENAI_API_KEY",),
                base_url="https://api.openai.com/v1",
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "providers"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "url=https://api.openai.com/v1, adapter=responses, env=OPENAI_API_KEY" in result.stdout


def test_cli_model_providers_marks_missing_env_and_offline_url(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "google": _FakeModelProvider(
                name="google",
                required_env=("GEMINI_API_KEY",),
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                models=(),
            ),
            "ollama": _FakeModelProvider(
                name="ollama",
                base_url="http://127.0.0.1:11434/v1",
                models=(),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "providers"],
        env={"GEMINI_API_KEY": ""},
    )

    assert result.exit_code == 0
    assert "env=GEMINI_API_KEY(missing)" in result.stdout
    assert "missing_env=GEMINI_API_KEY" not in result.stdout
    assert "url=http://127.0.0.1:11434/v1(offline)" in result.stdout


def test_cli_model_list_filters_by_model_selector(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openai": _FakeModelProvider(
                name="openai",
                required_env=("OPENAI_API_KEY",),
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
            "openrouter": _FakeModelProvider(
                name="openrouter",
                required_env=("OPENROUTER_API_KEY",),
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openrouter",
                        name="gpt-5",
                        model="openai/gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "list", "--select", "[openrouter]"],
        env={"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "openrouter" in result.stdout
    assert "openai      remote" not in result.stdout


def test_cli_model_list_reports_no_matched_models_for_empty_filter(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openai": _FakeModelProvider(
                name="openai",
                required_env=("OPENAI_API_KEY",),
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openai",
                        name="gpt-5",
                        model="gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "list", "--select", "[openrouter]"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "No matched models." in result.stdout
    assert "toolang model list --select <selector>" in result.stdout


def test_cli_model_list_filters_by_capability_selector(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openrouter": _FakeModelProvider(
                name="openrouter",
                required_env=("OPENROUTER_API_KEY",),
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openrouter",
                        name="gpt-5",
                        model="openai/gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                        tools=True,
                        streaming=True,
                    ),
                    ModelInfo(
                        ref="google/gemini-pro",
                        provider="openrouter",
                        name="gemini-pro",
                        model="google/gemini-pro",
                        selectors=("gemini-pro", "google/gemini-pro"),
                        adapter="responses",
                        tools=False,
                        streaming=True,
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "list", "--select", "[remote,streaming:y,tools=false]"],
        env={"OPENROUTER_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "google/gemini-pro" in result.stdout
    assert "openai/gpt-5" not in result.stdout


def test_cli_model_list_supports_filter_short_option(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda *_args: {
            "openrouter": _FakeModelProvider(
                name="openrouter",
                required_env=("OPENROUTER_API_KEY",),
                models=(
                    ModelInfo(
                        ref="openai/gpt-5",
                        provider="openrouter",
                        name="gpt-5",
                        model="openai/gpt-5",
                        selectors=("gpt-5", "openai/gpt-5"),
                        adapter="responses",
                        tools=True,
                        streaming=True,
                    ),
                    ModelInfo(
                        ref="google/gemini-pro",
                        provider="openrouter",
                        name="gemini-pro",
                        model="google/gemini-pro",
                        selectors=("gemini-pro", "google/gemini-pro"),
                        adapter="responses",
                        tools=False,
                        streaming=True,
                    ),
                ),
            ),
        },
    )

    result = runner.invoke(
        cli.app,
        ["model", "list", "-f", "gpt-*"],
        env={"OPENROUTER_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "openai/gpt-5" in result.stdout
    assert "google/gemini-pro" not in result.stdout


def test_cli_run_hands_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del progress, prepared_state
        captured["toolang_root"] = startup.toolang_root
        captured["agent_name"] = startup.agent_name
        captured["host"] = startup.host
        captured["endpoint_host"] = startup.endpoint_host
        captured["port"] = startup.port
        captured["sandbox"] = startup.selector.render()
        captured["models"] = startup.model_selectors
        captured["tools"] = startup.tool_selectors
        captured["dev"] = startup.dev_artifact
        captured["sandbox_child"] = sandbox_child
        captured["component_names"] = startup.enabled_components
        captured["log_spec"] = startup.log_spec
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "alice",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--enable",
            "chat",
            "--enable",
            "inspect",
        ],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["toolang_root"] == toolang_root
    assert captured["agent_name"] == "alice"
    assert captured["host"] == "0.0.0.0"
    assert captured["endpoint_host"] == "0.0.0.0"
    assert captured["port"] == 9000
    assert captured["sandbox"] == "none"
    assert captured["models"] == ()
    assert captured["tools"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["component_names"] == ("router.chat", "runner.chat", "router.inspect")
    assert captured["log_spec"] == DEFAULT_AGENT_LOG_SPEC
    assert cast(dict[str, str], captured["environ"])["TOOLANG_ROOT"] == str(toolang_root)
    assert cast(dict[str, str], captured["environ"])[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC


def test_cli_run_reuses_prepared_state_for_foreground_runtime(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    prepared_state = object()
    captured: dict[str, object] = {}
    prepare_calls = 0

    def fake_prepare_agent(**_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return prepared_state

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del startup, environ, sandbox_child, progress
        captured["prepared_state"] = prepared_state
        return 0

    monkeypatch.setattr(cli.agent_up, "prepare_agent", fake_prepare_agent)
    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--enable", "inspect"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert prepare_calls == 1
    assert captured["prepared_state"] is prepared_state


def test_cli_run_resolves_port_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 8765)

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del progress, prepared_state
        captured["toolang_root"] = startup.toolang_root
        captured["agent_name"] = startup.agent_name
        captured["host"] = startup.host
        captured["endpoint_host"] = startup.endpoint_host
        captured["port"] = startup.port
        captured["sandbox"] = startup.selector.render()
        captured["models"] = startup.model_selectors
        captured["dev"] = startup.dev_artifact
        captured["sandbox_child"] = sandbox_child
        captured["component_names"] = startup.enabled_components
        captured["log_spec"] = startup.log_spec
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--enable", "chat"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["toolang_root"] == toolang_root
    assert captured["agent_name"] == "alice"
    assert captured["host"] == "127.0.0.1"
    assert captured["endpoint_host"] == "localhost"
    assert captured["port"] == 8765
    assert captured["sandbox"] == "none"
    assert captured["models"] == ()
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["component_names"] == ("router.chat", "runner.chat")
    assert captured["log_spec"] == DEFAULT_AGENT_LOG_SPEC
    assert cast(dict[str, str], captured["environ"])["TOOLANG_ROOT"] == str(toolang_root)


def test_cli_run_supports_csv_loop_option(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["component_names"] = startup.enabled_components
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--enable", "chat,inspect,poll"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["component_names"] == ("router.chat", "runner.chat", "router.inspect", "trigger.poll")


def test_cli_run_passes_model_selectors_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["models"] = startup.model_selectors
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--models", "openai/gpt-5[openai]", "--models", "o3"],
        env={"TOOLANG_ROOT": str(toolang_root), "OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert captured["models"] == ("openai/gpt-5[openai]", "o3")


def test_cli_run_accepts_glob_model_selector_as_available_model_filter(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["models"] = startup.model_selectors
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--models", "openai/*"],
        env={"TOOLANG_ROOT": str(toolang_root), "OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert captured["models"] == ("openai/*",)


def test_cli_run_passes_tool_selectors_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["tools"] = startup.tool_selectors
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--tools", "filesystem,shell", "--tools", "service_use"],
        env={"TOOLANG_ROOT": str(toolang_root), "OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert captured["tools"] == ("filesystem", "shell", "service_use")


def test_cli_run_passes_cap_selectors_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del environ, sandbox_child, progress, prepared_state
        captured["caps"] = startup.cap_selectors
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--caps", "skill/reviewer,service/*[home]", "--caps", "[here]"],
        env={"TOOLANG_ROOT": str(toolang_root), "OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert captured["caps"] == ("skill/reviewer", "service/*[home]", "[here]")


def test_cli_run_rejects_missing_default_model_env(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    monkeypatch.setattr(
        cli.agent_up,
        "start_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime should exit before launching"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice", "--enable", "chat"],
        env={
            "DEEPSEEK_API_KEY": "",
            "GEMINI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "OLLAMA_HOST": "http://127.0.0.1:9",
        },
    )

    assert result.exit_code == 1
    assert "No available models." in result.stderr
    assert "toolang model providers" in result.stderr


def test_cli_run_uses_py_log_spec(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del sandbox_child, progress, prepared_state
        captured["environ"] = environ
        captured["log_spec"] = startup.log_spec
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice"],
        env={PY_LOG_ENV_VAR: "toolang.run=debug"},
    )

    assert result.exit_code == 0
    assert captured["log_spec"] == "toolang.run=debug"
    assert cast(dict[str, str], captured["environ"])[PY_LOG_ENV_VAR] == "toolang.run=debug"


def test_cli_run_requires_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run"],
        env={},
    )

    assert result.exit_code in {0, 2}
    assert "Usage:" in result.stdout
    assert "run [OPTIONS] AGENT" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Existing local agent name, remote agent ref, or URL." in result.stdout


def test_cli_run_loads_root_and_agent_env_with_agent_override(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir(parents=True, exist_ok=True)
    (toolang_root / ".env").write_text("TELEGRAM_BOT_TOKEN=root-token\nROOT_ONLY=1\n", encoding="utf-8")
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "agents" / "alice" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=agent-token\nAGENT_ONLY=1\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_start_runtime(
        startup: cli.agent_up.StartupSpec,
        *,
        environ: dict[str, str],
        sandbox_child: bool = False,
        progress=None,
        prepared_state=None,
    ) -> int:
        del progress, prepared_state
        captured["environ"] = environ
        captured["endpoint_host"] = startup.endpoint_host
        captured["sandbox"] = startup.selector.render()
        captured["models"] = startup.model_selectors
        captured["dev"] = startup.dev_artifact
        captured["sandbox_child"] = sandbox_child
        captured["log_spec"] = startup.log_spec
        return 0

    monkeypatch.setattr(cli.agent_up, "start_runtime", fake_start_runtime)
    monkeypatch.setattr(cli.agent_up, "prepare_agent", lambda **_kwargs: None)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice", "--enable", "inspect"],
        env={},
    )

    assert result.exit_code == 0
    environ = cast(dict[str, str], captured["environ"])
    assert environ["TELEGRAM_BOT_TOKEN"] == "agent-token"
    assert environ["ROOT_ONLY"] == "1"
    assert environ["AGENT_ONLY"] == "1"
    assert captured["endpoint_host"] == "localhost"
    assert captured["sandbox"] == "none"
    assert captured["models"] == ()
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["log_spec"] == DEFAULT_AGENT_LOG_SPEC


def test_cli_start_spawns_background_run_and_reports_status(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 8765)

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr
        captured["command"] = command
        captured["env"] = env
        captured["cwd"] = cwd
        captured["start_new_session"] = start_new_session
        captured["close_fds"] = close_fds
        stdout.write(b"launcher\n")
        stdout.flush()
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://localhost:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice", "--sandbox", "none", "--enable", "inspect"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "Started agent alice: https://too.run/8765"
    assert captured["command"] == [
        cli.sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(toolang_root),
        "run",
        "alice",
        "--host",
        "127.0.0.1",
        "--endpoint-host",
        "localhost",
        "--port",
        "8765",
        "--sandbox",
        "none",
        "--enable",
        "router.inspect",
    ]
    assert cast(dict[str, str], captured["env"])["TOOLANG_ROOT"] == str(toolang_root)
    assert cast(dict[str, str], captured["env"])[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC
    assert captured["cwd"] == str(Path.cwd())
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert agents.agent_runtime_log_path(toolang_root, "alice").read_text(encoding="utf-8") == "launcher\n"


def test_cli_start_propagates_py_log_to_agent_process(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self) -> int | None:
            return None

    def fake_popen(
        command: list[str],
        *,
        stdin,
        stdout,
        stderr,
        env: dict[str, str],
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ) -> FakeProcess:
        del stdin, stderr
        captured["command"] = list(command)
        stdout.write(b"launcher\n")
        stdout.flush()
        captured["env"] = dict(env)
        captured["cwd"] = cwd
        captured["start_new_session"] = start_new_session
        captured["close_fds"] = close_fds
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(toolang_root),
            "start",
            "alice",
            "--sandbox",
            "none",
        ],
        env={PY_LOG_ENV_VAR: "toolang.run=debug,httpx=off", "OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert command[0:5] == [
        cli.sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(toolang_root),
    ]
    assert cast(dict[str, str], captured["env"])[PY_LOG_ENV_VAR] == "toolang.run=debug,httpx=off"


def test_cli_start_rejects_active_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 1
    assert "Agent alice already running: https://too.run/8765" in result.stderr
    assert "API:" not in result.stderr
    assert "Stop:" not in result.stderr


def test_cli_start_allows_restart_after_stale_preparing_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=None,
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": None,
            "meta": {},
        },
        status="preparing",
    )

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del command, stdin, stdout, stderr, env, cwd, start_new_session, close_fds
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
            status="running",
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "Agent alice already running" not in result.stderr


def test_cli_start_supports_csv_loop_option(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 8765)

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr, env, cwd, start_new_session, close_fds
        captured["command"] = command
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice", "--enable", "chat,inspect"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--port" in command
    assert command[command.index("--port") + 1] == "8765"
    assert command[-6:] == [
        "--enable",
        "router.chat",
        "--enable",
        "runner.chat",
        "--enable",
        "router.inspect",
    ]


def test_cli_start_includes_model_selectors_in_background_command(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 8765)

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr, env, cwd, start_new_session, close_fds
        captured["command"] = command
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(toolang_root),
            "start",
            "alice",
            "--models",
            "gpt-5",
            "--models",
            "o3",
        ],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    first_flag = command.index("--models")
    assert command[first_flag + 1] == "gpt-5"
    second_flag = command.index("--models", first_flag + 1)
    assert command[second_flag + 1] == "o3"


def test_cli_start_includes_tool_selectors_in_background_command(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.agent_up, "resolve_runtime_port", lambda **_kwargs: 8765)

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr, env, cwd, start_new_session, close_fds
        captured["command"] = command
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(toolang_root),
            "start",
            "alice",
            "--tools",
            "filesystem,shell",
            "--tools",
            "service_use",
        ],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    first_flag = command.index("--tools")
    assert command[first_flag + 1] == "filesystem"
    second_flag = command.index("--tools", first_flag + 1)
    assert command[second_flag + 1] == "shell"
    third_flag = command.index("--tools", second_flag + 1)
    assert command[third_flag + 1] == "service_use"


def test_cli_start_rejects_unconfigured_model_selector(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    (toolang_root / "config.toml").write_text(
        '[models.aliases.gateway]\n'
        'ref = "openai/gpt-5"\n'
        'provider = "openai"\n'
        'adapter = "responses"\n'
        'key_env = "STARTUP_MISSING_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("startup should exit before launching"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice", "--models", "gateway"],
        env={},
    )

    assert result.exit_code == 1
    assert "STARTUP_MISSING_API_KEY" in result.stderr


def test_cli_start_rejects_missing_default_model_env(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("startup should exit before launching"),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice"],
        env={
            "DEEPSEEK_API_KEY": "",
            "GEMINI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "OLLAMA_HOST": "http://127.0.0.1:9",
        },
    )

    assert result.exit_code == 1
    assert "No available models." in result.stderr
    assert "OPENAI_API_KEY" in result.stderr
    assert "toolang model providers" in result.stderr


def test_cli_start_preserves_host_endpoint_host_and_sandbox_in_background_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr, env, cwd, start_new_session, close_fds
        captured["command"] = list(command)
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://agent.example.com:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(toolang_root),
            "start",
            "alice",
            "--host",
            "0.0.0.0",
            "--endpoint-host",
            "agent.example.com",
            "--port",
            "8765",
            "--sandbox",
            "docker:python:3.13-slim",
        ],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--host" in command
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert "--endpoint-host" in command
    assert command[command.index("--endpoint-host") + 1] == "agent.example.com"
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "docker:python:3.13-slim"
    assert "--port" in command
    assert command[command.index("--port") + 1] == "8765"


def test_cli_start_reuses_preferred_runtime_port(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:63295",
        started_at="2026-04-09T10:00:00Z",
        pid=None,
        status="stopped",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr("toolang.agents.agent_runtime_process_pids", lambda *_args: ())

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del stdin, stderr, env, cwd, start_new_session, close_fds
        captured["command"] = command
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint="http://127.0.0.1:63295",
            started_at="2026-04-09T10:00:01Z",
            pid=os.getpid(),
            status="running",
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--port" in command
    assert command[command.index("--port") + 1] == "63295"
    assert "--enable" in command
    component_names = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--enable"
    ]
    assert component_names == [
        "router.chat",
        "router.manage",
        "router.inspect",
        "runner.chat",
        "runner.task",
        "runner.chore",
        "trigger.pulse",
        "trigger.watch",
    ]


def test_cli_start_reports_failed_when_process_exits_before_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")

    class FakeProcess:
        def poll(self) -> int:
            return 1

    def fake_popen(
        command,
        *,
        stdin,
        stdout,
        stderr,
        env,
        cwd: str,
        start_new_session: bool,
        close_fds: bool,
    ):
        del command, stdin, stderr, env, cwd, start_new_session, close_fds
        stdout.write(b"boom\n")
        stdout.flush()
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 1
    assert "Agent alice failed to start:" in result.stderr


def test_cli_start_requires_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start"],
        env={},
    )

    assert result.exit_code in {0, 2}
    assert "Usage:" in result.stdout
    assert "AGENT start [OPTIONS]" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Existing local agent name." in result.stdout


def test_cli_stop_stops_sandboxed_agent(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": "sandbox-alice",
            "meta": {},
        },
    )
    captured: dict[str, object] = {}

    class FakeSandbox:
        name = "docker"

        def stop(self, state, *, force: bool = False) -> None:
            captured["runtime_id"] = state.runtime_id
            captured["force"] = force

    monkeypatch.setattr(cli.agent_up, "create_sandbox_plugin", lambda name, config=None: FakeSandbox())

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "stop", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "Stopped agent alice"
    assert captured["runtime_id"] == "sandbox-alice"
    assert captured["force"] is False


def test_cli_cap_remote_add_list_remove_round_trip(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        caps,
        "_remote_materialized_files",
        lambda *, relative_entry_path, kind, name, ref, progress=None: {
            str(relative_entry_path): b"---\ndescription: Review code\n---\n# Reviewer\n"
        },
    )

    add_result = _invoke_caps_app(
        ["skill", "add", "acme/reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == "Added skill reviewer: github://acme/agents/skills/reviewer@main"

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(encoding="utf-8")
    assert "[skills]" in config_text
    assert 'reviewer = { ref = "github://acme/agents/skills/reviewer@main" }' in config_text
    assert (
        toolang_root / "agents" / "alice" / ".caps" / "wired" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8") == "---\ndescription: Review code\n---\n# Reviewer\n"

    list_remote_result = _invoke_caps_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_remote_result.exit_code == 0
    assert "SKILL" in list_remote_result.stdout
    assert "SOURCE" in list_remote_result.stdout
    assert "FORM" in list_remote_result.stdout
    assert "SCOPE" in list_remote_result.stdout
    assert "DESCRIPTION" not in list_remote_result.stdout
    assert "reviewer" in list_remote_result.stdout
    assert "wired" in list_remote_result.stdout
    assert "home" in list_remote_result.stdout
    assert "https://github.com/acme/agents/tree/main/skills/reviewer" in list_remote_result.stdout

    remove_result = _invoke_caps_app(
        ["skill", "remove", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert remove_result.exit_code == 0
    assert remove_result.stdout.strip() == "Removed skill reviewer: github://acme/agents/skills/reviewer@main"

    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Review code\n"
            "---\n"
            "# Reviewer\n\n"
            "Review code carefully.\n"
        ),
    )

    add_result = _invoke_caps_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == (
        f"Created skill reviewer: {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer' / 'SKILL.md'}"
    )

    list_result = _invoke_caps_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "SKILL" in list_result.stdout
    assert "SOURCE" in list_result.stdout
    assert "FORM" in list_result.stdout
    assert "SCOPE" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "file" in list_result.stdout
    assert "home" in list_result.stdout
    assert "agents/alice/skills/reviewer" in list_result.stdout


def test_cli_cap_remote_list_shows_accessible_source_url(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        caps,
        "_remote_materialized_files",
        lambda *, relative_entry_path, kind, name, ref, progress=None: {
            str(relative_entry_path): b"---\ndescription: Review code\n---\n# Reviewer\n"
        },
    )

    add_result = _invoke_caps_app(
        ["skill", "add", "https://github.com/acme/agents/tree/main/skills/reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == "Added skill reviewer: github://acme/agents/skills/reviewer@main"

    list_result = _invoke_caps_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "SOURCE" in list_result.stdout
    assert "https://github.com/acme/agents/tree/main/skills/reviewer" in list_result.stdout
    assert "github://acme/agents/skills/reviewer@main" not in list_result.stdout


def test_cli_cap_remote_file_list_uses_github_blob_url(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        caps,
        "_remote_materialized_files",
        lambda *, relative_entry_path, kind, name, ref, progress=None: {
            str(relative_entry_path): b"Prefer concise answers.\n"
        },
    )

    add_result = _invoke_caps_app(
        ["psyche", "add", "github://acme/agents/psyches/concise.md@main"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0

    list_result = _invoke_caps_app(
        ["psyche", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "https://github.com/acme/agents/blob/main/psyches/concise.md" in list_result.stdout


def test_cli_cap_local_new_edit_remove_round_trip(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Review code\n"
            "---\n"
            "# Reviewer\n\n"
            "Review code carefully.\n"
        ),
    )
    new_result = _invoke_caps_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert new_result.exit_code == 0
    assert new_result.stdout.strip() == (
        f"Created skill reviewer: {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer' / 'SKILL.md'}"
    )
    assert new_result.stderr == ""
    assert (
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8").startswith(
        "---\ndescription: Review code\n---\n# Reviewer\n"
    )

    list_result = _invoke_caps_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "SKILL" in list_result.stdout
    assert "SOURCE" in list_result.stdout
    assert "FORM" in list_result.stdout
    assert "SCOPE" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "file" in list_result.stdout
    assert "home" in list_result.stdout
    assert "agents/alice/skills/reviewer" in list_result.stdout

    edited_text = (
        "---\n"
        "description: Review code deeply\n"
        "---\n"
        "# Reviewer\n\n"
        "Review code even more carefully.\n"
    )
    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: edited_text)
    edit_result = _invoke_caps_app(
        ["skill", "edit", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert edit_result.exit_code == 0
    assert edit_result.stdout.strip() == (
        f"Updated skill reviewer: {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer' / 'SKILL.md'}"
    )
    assert edit_result.stderr == ""
    assert (
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8") == edited_text

    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    no_changes_result = _invoke_caps_app(
        ["skill", "edit", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert no_changes_result.exit_code == 0
    assert no_changes_result.stdout.strip() == "No changes"

    duplicate_result = _invoke_caps_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert duplicate_result.exit_code == 1
    assert "Skill reviewer already exists" in duplicate_result.stderr

    delete_result = _invoke_caps_app(
        ["skill", "delete", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert delete_result.exit_code == 0
    assert delete_result.stdout.strip() == (
        f"Deleted skill reviewer: {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer'}"
    )
    assert delete_result.stderr == ""
    assert not (toolang_root / "agents" / "alice" / "skills" / "reviewer").exists()

    missing_result = _invoke_caps_app(
        ["skill", "delete", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert missing_result.exit_code == 1
    assert "Skill reviewer not found" in missing_result.stderr


def test_cli_cap_local_new_reuses_existing_remote_cap_outputs(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"}

    monkeypatch.setattr(caps, "_fetch_github_directory", fake_fetch)
    add_result = _invoke_caps_app(
        ["skill", "add", "acme/pdf"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert fetches == ["github://acme/agents/skills/pdf@main"]

    monkeypatch.setattr(
        caps,
        "_fetch_github_directory",
        lambda ref: pytest.fail(f"unexpected remote fetch: {ref.render()}"),
    )
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Review code\n"
            "---\n"
            "# Reviewer\n\n"
            "Review code carefully.\n"
        ),
    )
    new_result = _invoke_caps_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert new_result.exit_code == 0
    assert new_result.stderr == ""


def test_cli_cap_remote_add_reports_not_found(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: False)

    result = _invoke_caps_app(
        ["skill", "add", "acme/missing"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 1
    assert "Wired skill acme/missing not found" in result.stderr


def test_cli_cap_add_preserves_unrelated_config_sections(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    config_path = toolang_root / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[web]\n'
        'cors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        caps_cli.app,
        ["skill", "add", "by3gus/pdf-processing"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "Resolved 1 caps" in result.stderr
    text = config_path.read_text(encoding="utf-8")
    assert "[web]" in text
    assert "cors_allowed_origins" in text
    assert "http://localhost:3000" in text
    assert "https://too.run" in text
    assert "[skills]" in text
    assert (
        'pdf-processing = { ref = "github://by3gus/agents/skills/pdf-processing@main" }'
        in text
    )


def test_cli_remote_cap_add_remove_reuses_existing_wired_outputs(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return b"Remote psyche body.\n"

    monkeypatch.setattr(caps, "_fetch_github_file", fake_fetch)
    caps.add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="psyche",
        ref="github://bench/agents/psyches/old.md@main",
    )
    prepared_result = _invoke_caps_app(
        ["psyche", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert prepared_result.exit_code == 0
    assert fetches == ["github://bench/agents/psyches/old.md@main"]

    fetches.clear()
    add_result = _invoke_caps_app(
        ["psyche", "add", "github://bench/agents/psyches/new.md@main"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert add_result.exit_code == 0
    assert "Resolved 1 caps" in add_result.stderr
    assert "Updated 1 caps" in add_result.stderr
    assert fetches == ["github://bench/agents/psyches/new.md@main"]

    fetches.clear()
    remove_result = _invoke_caps_app(
        ["psyche", "remove", "old"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert remove_result.exit_code == 0
    assert remove_result.stderr == ""
    assert fetches == []


def test_cli_cap_new_cancel_does_not_create(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_edit(*_args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    result = runner.invoke(
        caps_cli.app,
        ["prompt", "new", "rewrite"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert result.stdout == "No changes\n"
    assert captured["require_save"] is True
    assert captured["extension"] == ".md"
    assert not (toolang_root / "prompts" / "rewrite.md").exists()


def test_cli_cap_new_unchanged_template_does_not_create(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: None)
    result = runner.invoke(
        caps_cli.app,
        ["prompt", "new", "rewrite"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert result.stdout == "No changes\n"
    assert not (toolang_root / "prompts" / "rewrite.md").exists()


def test_cli_cap_new_cancel_does_not_resolve_program_remote_uses(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True)
    (toolang_root / "agents" / "alice" / "agent.too").write_text(
        "agent alice\n\nuse skill briceyan/pdf-processing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda owner, repo: pytest.fail(f"unexpected remote branch lookup: {owner}/{repo}"),
    )

    result = _invoke_caps_app(
        ["psyche", "new", "add3"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert result.stdout == "No changes\n"
    assert not (toolang_root / "agents" / "alice" / "psyches" / "add3.md").exists()


def test_cli_cap_new_save_does_not_resolve_program_remote_uses(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True)
    (toolang_root / "agents" / "alice" / "agent.too").write_text(
        "agent alice\n\nuse skill briceyan/pdf-processing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        caps,
        "_fetch_github_directory",
        lambda ref: {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"},
    )
    watch.build_prepared_state(caps.scan_durable_state(toolang_root, "alice"))

    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: "Saved psyche.\n")
    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda owner, repo: pytest.fail(f"unexpected remote branch lookup: {owner}/{repo}"),
    )

    result = _invoke_caps_app(
        ["psyche", "new", "add4"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        f"Created psyche add4: {toolang_root / 'agents' / 'alice' / 'psyches' / 'add4.md'}"
    )
    assert (toolang_root / "agents" / "alice" / "psyches" / "add4.md").read_text(
        encoding="utf-8"
    ) == "Saved psyche.\n"


def test_cli_cap_new_supports_named_template(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_edit(text: str, *, extension: str, require_save: bool):
        captured["text"] = text
        captured["extension"] = extension
        captured["require_save"] = require_save
        return text

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    result = runner.invoke(
        caps_cli.app,
        ["service", "new", "search", "-t", "stdio"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "transport: stdio" in cast(str, captured["text"])
    assert "target: uvx example-mcp-server" in cast(str, captured["text"])


def test_cli_task_new_persists_id(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    result = _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    task = work.list_tasks(toolang_root, "alice")[0]
    saved = task.path.read_text(encoding="utf-8")
    assert task.path == toolang_root / "agents" / "alice" / "tasks" / f"{task.document.task_id()}.md"
    assert "\nid: " in saved
    assert "title: Task title" in saved
    assert "stage: todo" not in saved


def test_cli_task_and_chore_help_orders_commands() -> None:
    task = _invoke_app(["task", "--help"], prefix_agent="alice")
    chore = _invoke_app(["chore", "--help"], prefix_agent="alice")

    assert task.exit_code == 0
    assert chore.exit_code == 0
    assert _indexes_in_order(
        task.stdout,
        (
            "list",
            "new",
            "clone",
            "edit",
            "delete",
            "draft",
            "ready",
            "archive",
            "cancel",
            "reopen",
        ),
    )
    assert "run      Unsupported." not in task.stdout
    assert "Move a task to ready." in task.stdout
    assert "Move a task to archive." in task.stdout
    assert _indexes_in_order(
        chore.stdout,
        (
            "list",
            "new",
            "clone",
            "edit",
            "delete",
            "draft",
            "ready",
            "archive",
            "cancel",
            "run",
        ),
    )
    assert "reopen   Unsupported." not in chore.stdout
    assert "Move a chore to ready." in chore.stdout
    assert "Move a chore to archive." in chore.stdout
    assert "Trigger a chore run now." in chore.stdout
    assert "Run a chore." not in chore.stdout


def test_cli_task_list_shows_task_rows(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "state: inactive\n"
            "stage: running\n"
            "---\n"
            "Review the current plan.\n"
        ),
    )
    _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["task", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "ID" in result.stdout
    assert "TASK" in result.stdout
    assert "LIFECYCLE" in result.stdout
    assert "Review the current plan." in result.stdout
    assert "ready" in result.stdout


def test_cli_task_clone_creates_ready_copy(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["task", "new", "--draft"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    task_id = work.list_draft_tasks(toolang_root, "alice")[0].document.task_id()

    result = _invoke_app(
        ["task", "clone", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    cloned = work.list_tasks(toolang_root, "alice")[0]

    assert result.exit_code == 0
    assert f"task {cloned.document.task_id()} cloned" in result.stdout
    assert cloned.document.task_id() != task_id
    assert cloned.document.title == "Task title"


def test_cli_task_delete_requires_archived_task(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    task_id = work.list_tasks(toolang_root, "alice")[0].document.task_id()

    active_delete = _invoke_app(
        ["task", "delete", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert active_delete.exit_code == 1
    assert f"task is not archived: {task_id}; archive it before deleting" in active_delete.output
    assert work.find_task(toolang_root, "alice", task_id) is not None

    archive_result = _invoke_app(
        ["task", "archive", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    delete_result = _invoke_app(
        ["task", "delete", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert archive_result.exit_code == 0
    assert delete_result.exit_code == 0
    assert delete_result.stdout.strip() == f"task {task_id} deleted"
    assert work.find_archived_task(toolang_root, "alice", task_id) is None


def test_cli_task_draft_and_ready_move_lifecycle(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    task_id = work.list_tasks(toolang_root, "alice")[0].document.task_id()

    draft_result = _invoke_app(
        ["task", "draft", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    drafted = work.find_task(toolang_root, "alice", task_id, lifecycle="draft")
    ready_result = _invoke_app(
        ["task", "ready", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    readied = work.find_task(toolang_root, "alice", task_id)

    assert draft_result.exit_code == 0
    assert f"task {task_id} drafted" in draft_result.stdout
    assert drafted is not None
    assert ready_result.exit_code == 0
    assert f"task {task_id} ready" in ready_result.stdout
    assert readied is not None


def test_cli_task_ready_moves_archived_task_back(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    task_id = work.list_tasks(toolang_root, "alice")[0].document.task_id()
    _invoke_app(
        ["task", "archive", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["task", "ready", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert f"task {task_id} ready" in result.stdout
    assert work.find_task(toolang_root, "alice", task_id) is not None
    assert work.find_archived_task(toolang_root, "alice", task_id) is None


def test_cli_chore_new_and_list_show_schedule(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["chore", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "ID" in result.stdout
    assert "CHORE" in result.stdout
    assert "SCHEDULE" in result.stdout
    assert "Chore title" in result.stdout
    assert "FREQ=HOURLY;INTERVAL=1" in result.stdout


def test_cli_chore_clone_creates_ready_copy(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new", "--draft"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    chore_id = work.list_draft_chores(toolang_root, "alice")[0].document.chore_id()

    result = _invoke_app(
        ["chore", "clone", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    cloned = work.list_chores(toolang_root, "alice")[0]

    assert result.exit_code == 0
    assert f"chore {cloned.document.chore_id()} cloned" in result.stdout
    assert cloned.document.chore_id() != chore_id
    assert cloned.document.title == "Chore title"
    assert cloned.document.schedule == "FREQ=HOURLY;INTERVAL=1"


def test_cli_chore_draft_and_ready_move_lifecycle(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    chore_id = work.list_chores(toolang_root, "alice")[0].document.chore_id()

    draft_result = _invoke_app(
        ["chore", "draft", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    drafted = work.find_chore(toolang_root, "alice", chore_id, lifecycle="draft")
    ready_result = _invoke_app(
        ["chore", "ready", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    readied = work.find_chore(toolang_root, "alice", chore_id)

    assert draft_result.exit_code == 0
    assert f"chore {chore_id} drafted" in draft_result.stdout
    assert drafted is not None
    assert ready_result.exit_code == 0
    assert f"chore {chore_id} ready" in ready_result.stdout
    assert readied is not None


def test_cli_chore_ready_moves_archived_chore_back(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    chore_id = work.list_chores(toolang_root, "alice")[0].document.chore_id()
    _invoke_app(
        ["chore", "archive", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["chore", "ready", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    chore = work.find_chore(toolang_root, "alice", chore_id)
    assert chore is not None
    assert work.find_archived_chore(toolang_root, "alice", chore_id) is None


def test_cli_task_new_records_task_changed_update(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)

    result = _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    store = ExecutionStore(execution_db_path(toolang_root, "alice"))
    try:
        updates = store.list_updates(limit=10)
    finally:
        store.close()
    assert [item.kind for item in updates] == ["task_changed"]
    assert str(updates[0].payload["id"]).strip()


def test_cli_global_cap_change_does_not_create_agent_local_update_store(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Example entry\n"
            "---\n"
            "Example body.\n"
        ),
    )

    result = runner.invoke(
        caps_cli.app,
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert not execution_db_path(toolang_root, "default").exists()



def test_cli_task_requires_agent_prefix(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["task", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "AGENT task list" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name." in result.stdout


def test_cli_chore_requires_agent_prefix(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["chore", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "AGENT chore list" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name." in result.stdout


def test_cli_task_new_help_shows_required_prefix_agent() -> None:
    result = runner.invoke(cli.app, ["task", "new", "--help"])

    assert result.exit_code == 0
    assert "AGENT task new" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name." in result.stdout
    assert "--template" not in result.stdout
    assert "Task name" not in result.stdout


def test_cli_work_group_help_shows_required_prefix_agent() -> None:
    task_result = runner.invoke(cli.app, ["task", "--help"])
    chore_result = runner.invoke(cli.app, ["chore", "--help"])

    assert task_result.exit_code == 0
    assert chore_result.exit_code == 0
    assert "Usage:" in task_result.stdout
    assert "AGENT task" in task_result.stdout
    assert "Manage agent tasks." in task_result.stdout
    assert "Usage:" in chore_result.stdout
    assert "AGENT chore" in chore_result.stdout
    assert "Manage agent chores." in chore_result.stdout


def test_cli_registers_cap_commands() -> None:
    for command in ("caps", "psyche", "skill", "service", "prompt"):
        result = runner.invoke(cli.app, [command, "--help"])

        assert result.exit_code == 0


def test_cli_does_not_register_plugin_command() -> None:
    result = runner.invoke(cli.app, ["plugin", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_cli_fmt_is_hidden_but_available() -> None:
    help_result = runner.invoke(cli.app, ["--help"])
    fmt_result = runner.invoke(cli.app, ["fmt", "--help"])

    assert help_result.exit_code == 0
    assert " fmt " not in help_result.stdout
    assert fmt_result.exit_code == 0
    assert "Format .too files." in fmt_result.stdout


def test_cli_parse_is_hidden_but_available() -> None:
    help_result = runner.invoke(cli.app, ["--help"])
    parse_result = runner.invoke(cli.app, ["parse", "--help"])

    assert help_result.exit_code == 0
    assert " parse " not in help_result.stdout
    assert parse_result.exit_code == 0
    assert "Parse a .too file and print its AST." in parse_result.stdout


def test_cli_parse_prints_too_ast(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source_path.write_text(
        "struct Result:\n"
        "  title: Text\n"
        "\n"
        "thunk review(in: Pack) -> Json:\n"
        "  models = deepseek/*\n"
        "  user: Review it.\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["parse", str(source_path)])

    assert result.exit_code == 0
    ast_data = json.loads(result.stdout)
    assert "_source_lines" not in ast_data
    assert "declarations" not in ast_data
    assert ast_data["node"] == "program"
    assert [item["node"] for item in ast_data["items"]] == ["struct", "thunk"]
    assert ast_data["items"][0] == {
        "node": "struct",
        "name": "Result",
        "fields": [
            {
                "name": "title",
                "type": {"kind": "builtin", "name": "Text", "array_depth": 0},
                "optional": False,
                "span": {"line": 2},
            }
        ],
        "span": {"line": 1},
    }
    assert ast_data["items"][1] == {
        "node": "thunk",
        "directives": [
            {
                "name": "models",
                "op": "=",
                "values": ["deepseek/*"],
                "span": {"line": 5},
            }
        ],
        "messages": [
            {
                "content": "Review it.",
                "span": {"line": 6},
                "role": "user",
            }
        ],
        "span": {"line": 4},
        "name": "review",
        "params": [
            {
                "name": "in",
                "optional": False,
                "type": {"kind": "builtin", "name": "Pack", "array_depth": 0},
            }
        ],
        "return": {"kind": "builtin", "name": "Json", "array_depth": 0},
    }


def test_cli_parse_supports_stdin_and_compact_output() -> None:
    result = runner.invoke(
        cli.app,
        ["parse", "--compact", "--stdin-filepath", "buffer.too", "-"],
        input="thunk:\n  hello\n",
    )

    assert result.exit_code == 0
    assert "\n  " not in result.stdout
    ast_data = json.loads(result.stdout)
    assert ast_data["items"][0]["messages"][0]["content"] == "hello"


def test_cli_parse_include_source_lines() -> None:
    result = runner.invoke(
        cli.app,
        ["parse", "--source-lines", "-"],
        input="thunk:\n  hello\n",
    )

    assert result.exit_code == 0
    ast_data = json.loads(result.stdout)
    assert ast_data["_source_lines"] == ["thunk:", "  hello"]


def test_cli_fmt_formats_too_file(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source_path.write_text(
        "#!/usr/bin/env toolang\n"
        "\n"
        "struct Result:\n"
        "    title:Text\n"
        "\n"
        "thunk review( input:Message)->Json:\n"
        "    model= deepseek/*\n"
        "    user:   Review it.\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["fmt", str(source_path)])

    assert result.exit_code == 0
    assert "formatted" in result.stdout
    assert source_path.read_text(encoding="utf-8") == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "struct Result:\n"
        "  title: Text\n"
        "\n"
        "thunk review(in: Pack) -> Json:\n"
        "  models = deepseek/*\n"
        "\n"
        "  user: Review it.\n"
    )


def test_cli_fmt_check_reports_unformatted_without_writing(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source = "thunk review( input:Message):\n    user:   Review it.\n"
    source_path.write_text(source, encoding="utf-8")

    result = runner.invoke(cli.app, ["fmt", "--check", str(source_path)])

    assert result.exit_code == 1
    assert f"would reformat {source_path}" in result.stdout
    assert source_path.read_text(encoding="utf-8") == source


def test_cli_fmt_formats_stdin_with_filepath() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "--stdin-filepath", "buffer.too"],
        input="thunk review( input:Message):\n    user:   Review it.\n",
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "thunk review(in: Pack):\n"
        "  user: Review it.\n"
    )


def test_cli_fmt_formats_dash_as_stdin() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "-"],
        input="thunk review( input:Message):\n    user:   Review it.\n",
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "thunk review(in: Pack):\n"
        "  user: Review it.\n"
    )


def test_cli_fmt_handles_implicit_message_before_roles() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "-"],
        input=(
            "thunk is_relevant(in:Part[]):\n"
            "    Evidence bundle:\n"
            "    {{ _ }}\n"
            "\n"
            "    user:\n"
            "        abc\n"
            "\n"
            "    assistant:\n"
            "        def\n"
        ),
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "thunk is_relevant(in: Part[]):\n"
        "  Evidence bundle:\n"
        "  {{ _ }}\n"
        "\n"
        "  user:\n"
        "    abc\n"
        "\n"
        "  assistant:\n"
        "    def\n"
    )


def test_cli_fmt_formats_stdin_with_tab_size() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "--tab-size", "4", "-"],
        input="thunk review( input:Message):\n  user:\n    Review it.\n",
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "thunk review(in: Pack):\n"
        "    user:\n"
        "        Review it.\n"
    )


def test_cli_fmt_allows_stdin_filepath_with_dash() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "--stdin-filepath", "buffer.too", "-"],
        input="#Comment\n",
    )

    assert result.exit_code == 0
    assert result.stdout == "# Comment\n"


def test_cli_fmt_rejects_stdin_filepath_with_file_args(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source_path.write_text("thunk:\n  hello\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["fmt", "--stdin-filepath", "buffer.too", str(source_path)],
        input="thunk:\n  hello\n",
    )

    assert result.exit_code != 0
    assert "--stdin-filepath can only be combined with '-'" in result.output


def test_cli_fmt_rejects_dash_with_other_file_args(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source_path.write_text("thunk:\n  hello\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["fmt", "-", str(source_path)],
        input="thunk:\n  hello\n",
    )

    assert result.exit_code != 0
    assert "'-' cannot be combined with other path arguments" in result.output


def test_cli_caps_alias_lists_caps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="shared",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = runner.invoke(
        cli.app,
        ["caps"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "KIND" in result.stdout
    assert "CAP" in result.stdout
    assert "skill" in result.stdout
    assert "reviewer" in result.stdout
    assert "root" in result.stdout


def test_cli_cap_kind_alias_lists_agent_caps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = _invoke_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "SKILL" in result.stdout
    assert "reviewer" in result.stdout
    assert "home" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout


def test_cli_cap_commands_cover_file_backed_kinds(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    cases = (
        ("psyche", "reviewer", toolang_root / "psyches" / "reviewer.md"),
        ("prompt", "rewrite", toolang_root / "prompts" / "rewrite.md"),
        ("service", "search", toolang_root / "services" / "search.md"),
    )

    def fake_edit(text: str, **_kwargs) -> str:
        if "transport: http" in text:
            return (
                "---\n"
                "description: Example service\n"
                "transport: http\n"
                "target: https://example.com/mcp\n"
                "---\n"
            )
        return "---\ndescription: Example entry\n---\nExample body.\n"

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    for kind, name, path in cases:
        add_result = runner.invoke(
            caps_cli.app,
            [kind, "new", name],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert add_result.exit_code == 0
        assert add_result.stdout.strip() == f"Created {kind} {name}: {path}"
        assert "Resolved" not in add_result.stderr

        list_result = runner.invoke(
            caps_cli.app,
            [kind, "list"],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert list_result.exit_code == 0
        assert kind.upper() in list_result.stdout
        assert "SOURCE" in list_result.stdout
        assert "FORM" in list_result.stdout
        assert "SCOPE" in list_result.stdout
        assert name in list_result.stdout
        assert "file" in list_result.stdout
        assert "root" in list_result.stdout
        assert f"{kind}s/{name}" in list_result.stdout

        delete_result = runner.invoke(
            caps_cli.app,
            [kind, "delete", name],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert delete_result.exit_code == 0
        assert delete_result.stdout.strip() == f"Deleted {kind} {name}: {path}"
        assert "Resolved" not in delete_result.stderr
        assert not path.exists()


def test_cli_cap_template_outputs_named_template() -> None:
    skill_result = runner.invoke(caps_cli.app, ["skill", "template", "default"])
    prompt_result = runner.invoke(caps_cli.app, ["prompt", "template", "default"])
    service_result = runner.invoke(caps_cli.app, ["service", "template", "default"])
    psyche_result = runner.invoke(caps_cli.app, ["psyche", "template", "default"])

    assert skill_result.exit_code == 0
    assert prompt_result.exit_code == 0
    assert service_result.exit_code == 0
    assert psyche_result.exit_code == 0
    assert skill_result.stdout.strip().startswith(
        "---\ndescription: Trigger this skill for requests that need this workflow.\n---"
    )
    assert "`description` is the trigger summary." in skill_result.stdout
    assert prompt_result.stdout.strip().startswith("Write the reusable prompt text here.\n")
    assert "transport: http" in service_result.stdout
    assert "# headers:" in service_result.stdout
    assert "# env:" not in service_result.stdout
    assert "Use optional `headers` for HTTP auth." in service_result.stdout
    assert "Header values like `$API_TOKEN` declare required environment variables." in service_result.stdout
    assert "Prefer:" in psyche_result.stdout


def test_cli_cap_template_without_argument_lists_named_templates() -> None:
    result = runner.invoke(caps_cli.app, ["service", "template"])

    assert result.exit_code == 0
    assert "TEMPLATE" in result.stdout
    assert "default" in result.stdout
    assert "stdio" in result.stdout


def test_cli_skill_help_describes_remote_and_local_commands() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "--help"])

    assert result.exit_code == 0
    assert "Manage skill caps." in result.stdout
    assert "add" in result.stdout
    assert "remove" in result.stdout
    assert "new" in result.stdout
    assert "edit" in result.stdout
    assert "delete" in result.stdout
    assert "list" in result.stdout
    assert result.stdout.index("list") < result.stdout.index("new")
    assert result.stdout.index("new") < result.stdout.index("edit")
    assert result.stdout.index("edit") < result.stdout.index("delete")
    assert result.stdout.index("delete") < result.stdout.index("add")
    assert result.stdout.index("add") < result.stdout.index("remove")


def test_cli_run_help_mentions_how_to_select_agent() -> None:
    result = runner.invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "run [OPTIONS] AGENT" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Existing local agent name, remote agent ref, or URL." in result.stdout


def test_cli_models_option_help_is_consistent_for_run_commands() -> None:
    expected = "Limit available models. Pass CSV or repeat."

    for command in (["run", "--help"], ["start", "--help"]):
        result = runner.invoke(cli.app, command)

        assert result.exit_code == 0
        assert expected in result.stdout


def test_cli_tools_option_help_is_consistent_for_run_commands() -> None:
    expected = "Allow selected tools. Pass CSV or repeat."

    for command in (["run", "--help"], ["start", "--help"]):
        result = runner.invoke(cli.app, command)

        assert result.exit_code == 0
        assert "--tools" in result.stdout
        assert expected in result.stdout


def test_cli_caps_option_help_is_consistent_for_run_commands() -> None:
    expected = "Allow selected caps. Pass CSV or repeat."

    for command in (["run", "--help"], ["start", "--help"]):
        result = runner.invoke(cli.app, command)

        assert result.exit_code == 0
        assert "--caps" in result.stdout
        assert expected in result.stdout


def test_cli_chat_help_supports_model_tool_and_cap_options() -> None:
    result = runner.invoke(cli.app, ["chat", "--help"])

    assert result.exit_code == 0
    assert "--models" in result.stdout
    assert "Limit available models. Pass CSV or repeat." in result.stdout
    assert "--tools" in result.stdout
    assert "Allow selected tools. Pass CSV or repeat." in result.stdout
    assert "--caps" in result.stdout
    assert "Allow selected caps. Pass CSV or repeat." in result.stdout


def test_cli_runtime_option_help_order_and_descriptions() -> None:
    expected = (
        ("--sandbox", "Run the agent in a sandbox."),
        ("--models", "Limit available models. Pass CSV or repeat."),
        ("--tools", "Allow selected tools. Pass CSV or repeat."),
        ("--caps", "Allow selected caps. Pass CSV or repeat."),
        ("--host", "Bind the agent API to this host."),
        ("--port", "Bind the agent API to this port."),
        ("--enable", "Enable runtime components. Pass CSV or repeat."),
        ("--dev", "Use wheels from this file or directory"),
    )

    for command in (["run", "--help"], ["start", "--help"]):
        result = runner.invoke(cli.app, command)

        assert result.exit_code == 0
        positions = [result.stdout.index(option) for option, description in expected]
        assert positions == sorted(positions)
        for _option, description in expected:
            assert description in result.stdout
        assert "[default: none]" in result.stdout
        assert "starting a sandbox." in result.stdout


def test_cli_model_list_select_help() -> None:
    result = runner.invoke(cli.app, ["model", "list", "--help"])

    assert result.exit_code == 0
    assert "--filter" in result.stdout
    assert "-f" in result.stdout
    assert "--select" in result.stdout
    assert "--models" not in result.stdout
    assert "Filter models with selector-list syntax." in result.stdout
    assert "Pass" in result.stdout
    assert "CSV or repeat." in result.stdout


def test_cli_info_help_mentions_required_agent() -> None:
    result = runner.invoke(cli.app, ["info", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "AGENT info [OPTIONS]" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name" in result.stdout


def test_cli_skill_add_help_mentions_agent_scope() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "add", "--help"])

    assert result.exit_code == 0
    assert "Wire a skill ref." in result.stdout
    assert "[AGENT] skill add" in result.stdout


def test_cli_skill_new_help_mentions_agent_scope() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "new", "--help"])

    assert result.exit_code == 0
    assert "Create a file-backed skill." in result.stdout
    assert "[AGENT] skill new" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Scope" not in result.stdout
    assert "Apply to this agent's home skills instead of root" in result.stdout
    assert "skills." in result.stdout


def test_cli_skill_template_help_shows_plain_text_metavar() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "template", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "[AGENT] skill template [OPTIONS] [NAME]" in result.stdout
    assert "name       TEXT" in result.stdout
    assert "Template name." in result.stdout


def test_cli_skill_remove_help_mentions_agent_scope() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "remove", "--help"])

    assert result.exit_code == 0
    assert "Unwire a skill." in result.stdout
    assert "[AGENT] skill remove" in result.stdout


def test_cli_skill_edit_help_mentions_agent_scope() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "edit", "--help"])

    assert result.exit_code == 0
    assert "Edit a file-backed skill." in result.stdout
    assert "[AGENT] skill edit" in result.stdout


def test_cli_skill_list_help_mentions_agent_scope_concisely() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "list", "--help"])

    assert result.exit_code == 0
    assert "List skills." in result.stdout
    assert "[AGENT] skill list" in result.stdout
    assert "agent      TEXT  Also include this agent's home skills." in result.stdout


def test_cli_cap_list_with_agent_defaults_to_all_scopes(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Local psyche\n"
            "---\n"
            "Agent guidance.\n"
        ),
    )
    runner.invoke(
        caps_cli.app,
        ["psyche", "new", "abc"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )
    _invoke_caps_app(
        ["psyche", "new", "def"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_caps_app(
        ["psyche", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "abc" in result.stdout
    assert "def" in result.stdout
    assert "root" in result.stdout
    assert "home" in result.stdout
    assert "psyches/abc.md" in result.stdout
    assert "agents/alice/psyches/def.md" in result.stdout


def test_cli_cap_list_global_filters_results(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "description: Local psyche\n"
            "---\n"
            "Guidance.\n"
        ),
    )
    runner.invoke(
        caps_cli.app,
        ["psyche", "new", "abc"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )
    _invoke_caps_app(
        ["psyche", "new", "def"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    shared_result = _invoke_caps_app(
        ["psyche", "list", "--filter", "root"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert shared_result.exit_code == 0
    assert "abc" in shared_result.stdout
    assert "def" not in shared_result.stdout
    assert "root" in shared_result.stdout

    private_result = _invoke_caps_app(
        ["psyche", "list", "--filter", "home"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert private_result.exit_code == 0
    assert "abc" not in private_result.stdout
    assert "def" in private_result.stdout
    assert "home" in private_result.stdout


def test_cli_cap_list_concept_filters_results(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    caps.add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/remote-reviewer",
    )

    result = _invoke_caps_app(
        ["skill", "list", "--filter", "wired,home"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "remote-reviewer" in result.stdout
    assert "local-reviewer" not in result.stdout
    assert "wired" in result.stdout
    assert "home" in result.stdout
    assert "https://github.com/acme/agents/tree/main/skills/remote-reviewer" in result.stdout

    union_result = _invoke_caps_app(
        ["skill", "list", "--filter", "file,wired,home"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert union_result.exit_code == 0
    assert "remote-reviewer" in union_result.stdout
    assert "local-reviewer" in union_result.stdout
    assert "home" in union_result.stdout


def test_cli_cap_list_bare_pattern_matches_cap_name_not_kind(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="patch",
        text="---\ndescription: Patch changes\n---\n# Patch\n",
    )

    result = _invoke_caps_app(
        ["skill", "list", "--filter", "*l*"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "local-reviewer" in result.stdout
    assert "patch" not in result.stdout


def test_cli_cap_list_supports_filter_short_option(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="patch",
        text="---\ndescription: Patch changes\n---\n# Patch\n",
    )

    result = _invoke_caps_app(
        ["skill", "list", "-f", "*l*"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "local-reviewer" in result.stdout
    assert "patch" not in result.stdout


def test_cli_cap_kind_list_rejects_explicit_family_pattern(tmp_path: Path) -> None:
    result = _invoke_caps_app(
        ["skill", "list", "--filter", "skill/*"],
        env={"TOOLANG_ROOT": str(tmp_path / "toolang")},
        prefix_agent="alice",
    )

    assert result.exit_code == 1
    assert "must not include a family" in result.stderr


def test_cli_cap_list_rejects_empty_filter_list(tmp_path: Path) -> None:
    result = _invoke_caps_app(
        ["skill", "list", "--filter", "[]"],
        env={"TOOLANG_ROOT": str(tmp_path / "toolang")},
        prefix_agent="alice",
    )

    assert result.exit_code == 1
    assert "filter list cannot be empty" in result.stderr


def test_standalone_caps_command_lists_all_cap_kinds(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="shared",
        kind="psyche",
        name="style",
        text="Prefer concise answers.\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = _invoke_caps_app(
        ["list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "KIND" in result.stdout
    assert "CAP" in result.stdout
    assert "SOURCE" in result.stdout
    assert "FORM" in result.stdout
    assert "SCOPE" in result.stdout
    assert "psyche" in result.stdout
    assert "skill" in result.stdout
    assert "style" in result.stdout
    assert "reviewer" in result.stdout
    assert "root" in result.stdout
    assert "home" in result.stdout
    assert "psyches/style.md" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout


def test_standalone_caps_list_collects_all_kinds_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[set[str] | None] = []

    def fake_list_entries(_toolang_root, _agent_name, *, visibility=None, kinds=None):
        del visibility
        calls.append(kinds)
        return ()

    monkeypatch.setattr(caps, "list_entries", fake_list_entries)

    result = runner.invoke(
        caps_cli.app,
        ["--root", str(tmp_path / "toolang"), "list"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "No caps found."
    assert calls == [{"psyche", "skill", "service", "prompt"}]


def test_standalone_caps_all_kind_list_prepares_agent_once_with_progress(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = _invoke_caps_app(
        ["list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "reviewer" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout
    assert "Resolved 1 caps" in result.stderr


def test_standalone_caps_command_supports_concept_filters(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="shared",
        kind="psyche",
        name="style",
        text="Prefer concise answers.\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = _invoke_caps_app(
        ["list", "--filter", "skill/*[file,home]"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "reviewer" in result.stdout
    assert "style" not in result.stdout
    assert "skill" in result.stdout
    assert "psyche" not in result.stdout
    assert "file" in result.stdout
    assert "home" in result.stdout


def test_standalone_caps_command_treats_here_caps_as_not_root(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    (toolang_root / "agents" / "alice" / "agent.too").write_text(
        (
            "agent alice\n\n"
                "psyche reviewer:\n"
                "  Prefer concrete findings.\n"
        ),
        encoding="utf-8",
    )

    result = _invoke_caps_app(
        ["list", "--filter", "here"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "reviewer" in result.stdout
    assert "agents/alice/agent.too:" in result.stdout
    assert "inline" in result.stdout
    assert "here" in result.stdout


def test_cli_main_normalizes_agent_prefix_shortcut_for_caps_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prog_name"] = prog_name
        captured["standalone_mode"] = standalone_mode
        captured["prefix_agent"] = cli._CLI_PREFIX_AGENT

    monkeypatch.setattr(cli, "app", cast(object, fake_app))
    monkeypatch.setattr(cli.sys, "argv", ["toolang"])

    result = cli.main(["alice", "caps"])

    assert result == 0
    assert captured["args"] == ["caps"]
    assert captured["prefix_agent"] == "alice"
    assert cli._CLI_PREFIX_AGENT is None


@pytest.mark.parametrize(
    ("command", "path"),
    (
        ("threads", "/api/v1/threads"),
        ("runs", "/api/v1/runs"),
    ),
)
def test_cli_main_thread_commands_support_agent_prefix_shortcut(
    command: str,
    path: str,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_runtime_json(ctx: Any, request_path: str) -> dict[str, object]:
        captured["agent"] = cast(dict[str, object], ctx.obj).get("agent")
        captured["path"] = request_path
        return {"items": []}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli.sys, "argv", ["too"])

    with pytest.raises(SystemExit) as exc:
        cli.main(["dev", command])

    assert exc.value.code == 0
    assert captured == {"agent": "dev", "path": path}
    assert cli._CLI_PREFIX_AGENT is None


def test_cli_threads_lists_title_and_run_count(monkeypatch) -> None:
    title = "This is a very long thread title that should be truncated before display"

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        return {
            "items": [
                {
                    "id": "web_abc12345",
                    "title": title,
                    "run_count": 12,
                    "origin": "chat",
                    "channel": "web",
                    "status": "running",
                    "updated_at": "2026-06-04T09:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["threads", "dev"])

    assert result.exit_code == 0
    assert "TITLE" in result.stdout
    assert "RUNS" in result.stdout
    assert "CHANNEL" not in result.stdout
    assert "ORIGIN" not in result.stdout
    assert "chat" not in result.stdout
    assert "web" in result.stdout
    assert "12" in result.stdout
    assert "This is a very long thread title that should..." in result.stdout
    assert title not in result.stdout


def test_cli_threads_lists_offline_runs_when_agent_is_not_running(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = ExecutionStore(execution_db_path(toolang_root, "alice"))
    try:
        run = store.start_run(
            run_id="run_first",
            thread_id="script_main",
            origin="script",
            input=Message.user("index docs"),
            created_at="2026-06-06T01:00:00Z",
            started_at="2026-06-06T01:00:00Z",
        )
        store.finish_run(run_id=run.run_id, finished_at="2026-06-06T01:01:00Z")
    finally:
        store.close()

    result = _invoke_app(
        ["threads", "alice"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "script_main" in result.stdout
    assert "index docs" in result.stdout


def test_cli_runs_lists_title(monkeypatch) -> None:
    title = "This is a very long run summary that should be truncated before display"

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/runs"
        return {
            "items": [
                {
                    "id": "run_abc12345",
                    "summary": title,
                    "thread_id": "web_thread1",
                    "origin": "chat",
                    "status": "finished",
                    "created_at": "2026-06-04T09:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["runs", "dev"])

    assert result.exit_code == 0
    assert result.stdout.index("THREAD") < result.stdout.index("RUN")
    assert "TITLE" in result.stdout
    assert "ORIGIN" not in result.stdout
    assert "chat" not in result.stdout
    assert "web_thread1" in result.stdout
    assert "run_abc12345" in result.stdout
    assert "succeeded" in result.stdout
    assert "This is a very long run summary that should b..." in result.stdout
    assert title not in result.stdout


def test_cli_runs_falls_back_to_offline_store_when_api_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    store = ExecutionStore(execution_db_path(toolang_root, "alice"))
    try:
        run = store.start_run(
            run_id="run_first",
            thread_id="script_abc123",
            origin="file",
            input=Message.user("summarize file"),
            created_at="2026-06-06T01:00:00Z",
            started_at="2026-06-06T01:00:00Z",
        )
        store.finish_run(run_id=run.run_id, finished_at="2026-06-06T01:01:00Z")
    finally:
        store.close()

    monkeypatch.setattr(
        cli,
        "_runtime_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli.click.ClickException("runtime request failed")),
    )

    result = _invoke_app(
        ["runs", "alice", "--thread", "script_abc123"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert "run_first" in result.stdout
    assert "summarize file" in result.stdout


def test_cli_runs_hides_thread_column_when_filtered_by_thread(monkeypatch) -> None:
    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/runs?thread_id=term_bzrh67se"
        return {
            "items": [
                {
                    "id": "run_abc12345",
                    "summary": "one run",
                    "thread_id": "term_bzrh67se",
                    "origin": "chat",
                    "status": "running",
                    "created_at": "2026-06-04T09:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["runs", "dev", "--thread", "term_bzrh67se"])

    assert result.exit_code == 0
    assert "THREAD" not in result.stdout
    assert "term_bzrh67se" not in result.stdout
    assert "RUN" in result.stdout
    assert "run_abc12345" in result.stdout
    assert "one run" in result.stdout


def test_cli_send_uses_terminal_client_and_streams(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_runtime_stream(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> None:
        captured["path"] = request_path
        captured["payload"] = payload

    monkeypatch.setattr(cli, "_runtime_stream", fake_runtime_stream)

    result = _invoke_app(["send", "dev", "term_thread", "review this repo"])

    assert result.exit_code == 0
    assert captured["path"] == "/api/v1/chat/stream"
    assert captured["payload"] == {
        "thread": "term_thread",
        "client": "tui",
        "message": {"role": "user", "parts": [{"type": "text", "text": "review this repo"}]},
    }


def test_cli_chat_passes_model_tool_cap_and_executable_selectors(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_chat_interactive(
        _ctx: Any,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        captured["thread_id"] = thread_id
        captured["selector_payload"] = selector_payload

    monkeypatch.setattr(cli, "_chat_interactive", fake_chat_interactive)

    result = _invoke_app(
        [
            "chat",
            "dev",
            "--models",
            "openai/gpt-5,openai/o3",
            "--models",
            "openai/gpt-5",
            "--tools",
            "filesystem,shell",
            "--tools",
            "service_use",
            "--caps",
            "skill/reviewer,service/*[home]",
            "--caps",
            "[here]",
            "--thunk",
            "summarize",
        ]
    )

    assert result.exit_code == 0
    assert captured["thread_id"] is None
    payload = cast(dict[str, object], captured["selector_payload"])
    assert payload["models"] == ["openai/gpt-5", "openai/o3"]
    assert payload["tools"] == ["filesystem", "shell", "service_use"]
    assert payload["caps"] == ["skill/reviewer", "service/*[home]", "[here]"]
    assert payload["thunk"] == "summarize"


def test_cli_chat_without_agent_shows_help_without_opening_ui(monkeypatch) -> None:
    opened = False

    def fake_chat_interactive(
        _ctx: Any,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        del thread_id, selector_payload
        nonlocal opened
        opened = True

    monkeypatch.setattr(cli, "_chat_interactive", fake_chat_interactive)

    result = _invoke_app(["chat"])

    assert result.exit_code == 0
    assert opened is False
    assert "Usage:" in result.stdout
    assert "Open a terminal chat session." in result.stdout
    assert "Toolang (v" not in result.stdout


@pytest.mark.parametrize("command", ("chat", "threads", "runs"))
def test_cli_required_agent_thread_commands_without_agent_exit_after_help(monkeypatch, command: str) -> None:
    runtime_calls: list[str] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        runtime_calls.append(request_path)
        return {"items": []}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_chat_interactive", lambda *_args, **_kwargs: runtime_calls.append("chat"))

    result = _invoke_app([command])

    assert result.exit_code == 0
    assert runtime_calls == []
    assert "Usage:" in result.stdout
    assert "AGENT" in result.stdout


def test_cli_chat_without_args_does_not_create_thread_until_first_message(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(
        cli.agents,
        "get_agent_status",
        lambda *_args, **_kwargs: cli.agents.AgentStatus(
            name="dev",
            status="stopped",
            endpoint=None,
            api_url=None,
            webui_url=None,
            sandbox=None,
        ),
    )

    result = _invoke_app(["chat", "dev"], input="/exit\n")

    assert result.exit_code == 0
    assert calls == []
    assert "thread term_new" not in result.stdout


def test_cli_chat_scripted_help_command_does_not_create_thread(monkeypatch) -> None:
    posts: list[tuple[str, object]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(
        cli.agents,
        "get_agent_status",
        lambda *_args, **_kwargs: cli.agents.AgentStatus(
            name="dev",
            status="stopped",
            endpoint=None,
            api_url=None,
            webui_url=None,
            sandbox=None,
        ),
    )

    result = _invoke_app(["chat", "dev"], input="/help\n/exit\n")

    assert result.exit_code == 0
    assert posts == []
    assert "Slash Commands" in result.stdout
    assert "/model [SELECTOR]  List or switch models." in result.stdout
    assert "/flow [NAME]       List or use a flow." in result.stdout


def test_cli_chat_without_thread_creates_thread_for_first_scripted_message(monkeypatch) -> None:
    posts: list[tuple[str, object]] = []
    streams: list[tuple[str, dict[str, object]]] = []
    listeners: list[str] = []

    class FakeListener:
        def stop(self) -> None:
            listeners.append("stopped")

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    def fake_start_thread_event_listener(_ctx: Any, thread_id: str, **_kwargs: object) -> FakeListener:
        listeners.append(thread_id)
        return FakeListener()

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        del event_handler
        streams.append((request_path, payload))

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_start_thread_event_listener", fake_start_thread_event_listener)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(
        cli.agents,
        "get_agent_status",
        lambda *_args, **_kwargs: cli.agents.AgentStatus(
            name="dev",
            status="stopped",
            endpoint=None,
            api_url=None,
            webui_url=None,
            sandbox=None,
        ),
    )

    result = _invoke_app(["chat", "dev"], input="hello\n/exit\n")

    assert result.exit_code == 0
    assert posts == [("/api/v1/threads", {"client": "tui"})]
    assert listeners == ["term_new", "stopped"]
    assert "thread term_new" in result.stdout
    assert len(streams) == 1
    request_path, payload = streams[0]
    assert request_path == "/api/v1/chat/stream"
    assert payload["thread"] == "term_new"
    assert payload["message"] == {"role": "user", "parts": [{"type": "text", "text": "hello"}]}


def test_cli_chat_thread_without_message_sends_interactive_lines(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    listeners: list[str] = []

    class FakeListener:
        def stop(self) -> None:
            listeners.append("stopped")

    def fake_start_thread_event_listener(_ctx: Any, thread_id: str, **_kwargs: object) -> FakeListener:
        listeners.append(thread_id)
        return FakeListener()

    def fake_runtime_consume_stream(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> None:
        calls.append((request_path, payload))

    monkeypatch.setattr(cli, "_start_thread_event_listener", fake_start_thread_event_listener)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)

    result = _invoke_app(["chat", "dev", "term_existing"], input="hello\n/exit\n")

    assert result.exit_code == 0
    assert "thread term_existing" in result.stdout
    assert listeners == ["term_existing", "stopped"]
    assert len(calls) == 1
    request_path, payload = calls[0]
    assert request_path == "/api/v1/chat/stream"
    assert payload["thread"] == "term_existing"
    assert payload["client"] == "tui"
    assert payload["message"] == {"role": "user", "parts": [{"type": "text", "text": "hello"}]}
    assert isinstance(payload["request_id"], str)
    assert payload["request_id"].startswith("term_")


def test_cli_chat_interactive_tty_uses_prompt_toolkit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    def fake_prompt_toolkit(
        ctx: Any,
        *,
        thread_id: str,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        captured["ctx"] = ctx
        captured["thread_id"] = thread_id
        captured["selector_payload"] = selector_payload

    monkeypatch.setattr(cli, "_chat_interactive_prompt_toolkit", fake_prompt_toolkit)

    cli._chat_interactive(cast(Any, object()), thread_id="term_existing", selector_payload={"models": ["openai/gpt-5"]})

    assert captured["thread_id"] == "term_existing"
    assert captured["selector_payload"] == {"models": ["openai/gpt-5"]}


def test_cli_chat_interactive_tty_accepts_missing_thread_without_creating_thread(monkeypatch) -> None:
    captured: dict[str, object] = {}
    posts: list[tuple[str, object]] = []

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    def fake_prompt_toolkit(
        ctx: Any,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        del ctx
        captured["thread_id"] = thread_id
        captured["selector_payload"] = selector_payload

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_interactive_prompt_toolkit", fake_prompt_toolkit)
    cli._chat_interactive(cast(Any, object()), thread_id=None, selector_payload={})

    assert posts == []
    assert captured["thread_id"] is None
    assert captured["selector_payload"] == {}


def test_cli_chat_run_lines_render_events_without_status_or_truncating_assistant() -> None:
    long_text = "first sentence. " + ("full assistant output " * 12).strip()
    run = cli._ChatRun(run_id="run_long", message="summarize", status="succeeded")
    run.completed_steps[1] = {
        "kind": "tool",
        "step_index": 1,
        "output": [{"type": "tool_result", "tool_name": "web_search", "output": {"ok": True}}],
    }
    run.completed_steps[2] = {
        "kind": "model",
        "step_index": 2,
        "payload": {"model_ref": "openai/gpt-5"},
        "output": [{"type": "text", "text": long_text}],
    }
    run.completed_steps[3] = {
        "kind": "tool",
        "step_index": 3,
        "error": "rate limited",
        "output": [{"type": "tool_result", "tool_name": "weather", "output": {}}],
    }
    run.completed_steps[4] = {
        "kind": "system",
        "step_index": 4,
        "payload": {"message": "runtime checkpoint saved"},
    }

    lines = cli._chat_run_lines(run, include_steps=True)
    rendered = "\n".join(lines)
    visible = " ".join(cli._chat_visible_text(rendered).split())

    assert long_text in visible
    assert f"• {long_text[:120]}" not in rendered
    assert "= succeeded" not in rendered
    assert "..." not in rendered
    assert "› ran web_search" in rendered
    assert "› ran weather failed: rate limited" in rendered
    assert "◇ runtime checkpoint saved" in rendered
    assert "steps:" not in rendered
    assert "model call completed" not in rendered


def test_cli_chat_active_run_panel_tails_activity_without_truncating_scrollback() -> None:
    run = cli._ChatRun(run_id="run_busy", message="run a flow", status="running")
    for index in range(1, 31):
        run.completed_steps[index] = {
            "kind": "system",
            "step_index": index,
            "payload": {"message": f"progress line {index:02d}"},
        }

    panel = cli._ChatLastRunPanel(lambda: run)
    activity = panel.activity_lines()
    visible_activity = cli._chat_visible_text("\n".join(activity))
    scrollback = cli._chat_visible_text("\n".join(cli._chat_run_lines(run, include_steps=True)))

    assert len(activity) == cli._CHAT_MAX_ACTIVE_RUN_ACTIVITY_ROWS + 2
    assert "earlier lines" in visible_activity
    assert "progress line 01" not in visible_activity
    assert "progress line 30" in visible_activity
    assert "progress line 01" in scrollback
    assert "progress line 30" in scrollback


def test_cli_chat_run_lines_render_tool_requests_and_inputs() -> None:
    run = cli._ChatRun(run_id="run_tool", message="在terminal里，纯文本格式输出", status="succeeded")
    tool_part = {
        "type": "tool_call",
        "tool_call_id": "call_1",
        "tool_name": "shell__execute",
        "tool_family": "shell__execute",
        "input": {"command": "date '+%Y-%m-%d %H:%M:%S %Z'"},
    }
    run.record_part({"step_index": 1, "part_index": 0, "part": tool_part})
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "payload": {"model_ref": "deepseek/deepseek-v4-flash"},
        "output": [tool_part],
    }
    run.completed_steps[2] = {
        "kind": "tool",
        "step_index": 2,
        "input": [{"step_index": 1, "part_index": 0}],
        "output": [{"type": "tool_result", "tool_name": "shell__execute", "output": {"stdout": "2026-06-12 19:17:13 CST\n"}}],
    }

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "requested shell__execute" not in rendered
    assert "› ran shell__execute: date '+%Y-%m-%d %H:%M:%S %Z'" in rendered
    assert "model call completed" not in rendered
    assert "› ran shell__execute\n" not in rendered


def test_cli_chat_run_lines_render_pending_tool_request_until_result() -> None:
    run = cli._ChatRun(run_id="run_tool", message="search", status="running")
    tool_part = {
        "type": "tool_call",
        "tool_call_id": "call_1",
        "tool_name": "web_search__search",
        "input": {"query": "李荣浩 热门歌曲 代表作"},
    }
    run.record_part({"step_index": 1, "part_index": 0, "part": tool_part})
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "output": [tool_part],
    }

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "• requested web_search__search: 李荣浩 热门歌曲 代表作" in rendered


def test_cli_chat_model_step_uses_recorded_tool_part_when_output_is_empty() -> None:
    run = cli._ChatRun(run_id="run_tool", message="search", status="running")
    run.record_part(
        {
            "step_index": 1,
            "part_index": 0,
            "part": {
                "type": "tool_call",
                "tool_call_id": "call_1",
                "tool_name": "web_search__search",
                "input": {"query": "李荣浩 热门歌曲 代表作"},
            },
        }
    )
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "output": [],
    }

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "• requested web_search__search: 李荣浩 热门歌曲 代表作" in rendered
    assert "model call completed" not in rendered


def test_cli_chat_empty_model_step_says_no_message() -> None:
    run = cli._ChatRun(run_id="run_empty", message="continue", status="running")
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "payload": {"model_ref": "deepseek/deepseek-v4-flash"},
        "output": [],
    }

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "• model returned no message (deepseek/deepseek-v4-flash)" in rendered
    assert "model call completed" not in rendered


def test_cli_chat_run_lines_render_consumed_steer_input_before_response() -> None:
    run = cli._ChatRun(run_id="run_steer", message="sleep 120 secs", status="succeeded")
    run.complete_step(
        {
            "kind": "tool",
            "step_index": 1,
            "output": [{"type": "tool_result", "tool_name": "shell__execute", "output": {"stdout": ""}}],
        }
    )
    run.record_command(
        {
            "kind": "steer",
            "ref": {"kind": "command", "index": 1},
            "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
        }
    )
    run.start_step({"kind": "model", "step_index": 2})
    run.complete_step(
        {
            "kind": "model",
            "step_index": 2,
            "output": [{"type": "text", "text": 'Now "abc" - still testing the queue?'}],
        }
    )

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))
    visible = cli._chat_visible_text(rendered)

    assert _indexes_in_order(
        visible,
        (
            "› ran shell__execute",
            "+ abc",
            '• Now "abc" - still testing the queue?',
        ),
    )
    assert "pending for next step" not in visible
    assert cli._chat_ansi_style(cli._CHAT_STEER_INPUT_FG, cli._CHAT_STEER_INPUT_BG) in rendered
    assert any(line.strip() == "+ abc" for line in visible.splitlines())


def test_cli_chat_steer_input_block_has_vertical_blank_lines() -> None:
    block = cli._chat_steer_input_block(
        {
            "kind": "steer",
            "ref": {"kind": "command", "index": 1},
            "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
        },
        waiting=False,
    )
    visible_lines = cli._chat_visible_text("\n".join(block)).splitlines()
    marker_index = next(index for index, line in enumerate(visible_lines) if line.strip() == "+ abc")

    assert block[0] == ""
    assert block[-1] == ""
    assert visible_lines[marker_index - 1].strip() == ""
    assert visible_lines[marker_index + 1].strip() == ""


def test_cli_chat_steer_waiting_uses_footer_padding_line() -> None:
    block = cli._chat_steer_input_block(
        {
            "kind": "steer",
            "ref": {"kind": "command", "index": 1},
            "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
        },
        waiting=True,
    )
    visible_lines = cli._chat_visible_text("\n".join(block)).splitlines()
    marker_index = next(index for index, line in enumerate(visible_lines) if line.strip() == "+ abc")

    assert visible_lines[marker_index + 1].strip() == "pending for next step"
    assert block[-1] == ""


def test_cli_chat_active_run_uses_steer_input_bar_colors() -> None:
    command = {
        "kind": "steer",
        "ref": {"kind": "command", "index": 1},
        "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
    }
    run = cli._ChatRun(run_id="run_steer", message="sleep 120 secs", status="running")
    run.start_step({"run_id": "run_steer", "step_index": 1, "kind": "tool"})
    run.record_command(command)
    panel = cli._ChatLastRunPanel(lambda: run)

    rendered = panel.render_activity()
    rendered_text = "".join(text for _style, text in rendered)

    assert ("class:steer-input.dim", "+") in rendered
    assert any(style == "class:steer-input" and "abc" in text for style, text in rendered)
    assert any(style == "class:steer-input.dim" and text.strip() == "pending for next step" for style, text in rendered)
    assert rendered_text.count("+ abc") == 1
    style = cli.Style.from_dict(cli._chat_ui_palette())
    steer_backgrounds = {
        style.get_attrs_for_style_str(fragment_style).bgcolor
        for fragment_style, _text in rendered
        if fragment_style.startswith("class:steer-input")
    }
    assert steer_backgrounds == {cli._CHAT_STEER_INPUT_BG.removeprefix("#")}
    scrollback_background = _ansi_truecolor_background("\n".join(cli._chat_steer_input_block(command, waiting=True)))
    input_background = _ansi_truecolor_background(cli._chat_input_block_line(""))
    assert scrollback_background == next(iter(steer_backgrounds))
    assert scrollback_background != input_background


def test_cli_chat_active_run_uses_normal_input_bar_colors() -> None:
    run = cli._ChatRun(run_id="run_user", message="hello", status="running")
    panel = cli._ChatLastRunPanel(lambda: run)

    rendered = panel.render_user()
    rendered_text = "".join(text for _style, text in rendered)

    assert ("class:normal-input.dim", ">") in rendered
    assert any(style == "class:normal-input" and "hello" in text for style, text in rendered)
    assert any(style == "class:normal-input.dim" and text.strip() == "run_user" for style, text in rendered)
    assert rendered_text.count("> hello") == 1


def test_cli_chat_run_lines_render_pending_steer_after_active_step() -> None:
    run = cli._ChatRun(run_id="run_steer", message="sleep 120 secs", status="running")
    run.complete_step(
        {
            "kind": "tool",
            "step_index": 1,
            "output": [{"type": "tool_result", "tool_name": "shell__execute", "output": {"stdout": ""}}],
        }
    )
    run.start_step({"run_id": "run_steer", "step_index": 2, "kind": "model"})
    run.record_command(
        {
            "kind": "steer",
            "ref": {"kind": "command", "index": 1},
            "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
        }
    )

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))
    visible = cli._chat_visible_text(rendered)

    assert _indexes_in_order(
        visible,
        (
            "› ran shell__execute",
            "• thinking...",
            "+ abc",
            "pending for next step",
        ),
    )


def test_cli_chat_completed_step_keeps_timeline_for_steer_ordering() -> None:
    run = cli._ChatRun(run_id="run_steer", message="sleep 120 secs", status="running")
    run.record_command(
        {
            "kind": "steer",
            "ref": {"kind": "command", "index": 1},
            "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
        }
    )
    run.start_step({"run_id": "run_steer", "step_index": 1, "kind": "model"})
    run.complete_step(
        {
            "run_id": "run_steer",
            "step_index": 1,
            "kind": "model",
            "output": [{"type": "text", "text": "after steer"}],
        }
    )

    visible = cli._chat_visible_text("\n".join(cli._chat_run_lines(run, include_steps=True)))

    assert _indexes_in_order(visible, ("+ abc", "• after steer"))
    assert "pending for next step" not in visible


def test_cli_chat_run_command_steer_records_input_bar(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_runtime_json", lambda _ctx, _path: {})
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_steer", message="working", status="running")

    app.handle_runtime_event(
        {
            "type": "run_command",
            "payload": {
                "run_id": "run_steer",
                "kind": "steer",
                "ref": {"kind": "command", "index": 1},
                "message": {"role": "user", "parts": [{"type": "text", "text": "abc"}]},
            },
        }
    )

    visible = cli._chat_visible_text("\n".join(cli._chat_run_lines(app.active_run, include_steps=True)))
    assert "+ abc" in visible


def test_cli_chat_run_lines_render_stop_command_by_event_order() -> None:
    run = cli._ChatRun(run_id="run_cancel", message="sleep 120 secs", status="canceled")
    run.start_step({"run_id": "run_cancel", "step_index": 1, "kind": "tool"})
    run.record_command(
        {
            "kind": "stop",
            "mode": "immediate",
            "ref": {"kind": "command", "index": 1},
        }
    )
    run.complete_step(
        {
            "run_id": "run_cancel",
            "step_index": 1,
            "kind": "tool",
            "output": [{"type": "tool_result", "tool_name": "shell__execute", "output": {"stdout": ""}}],
        }
    )

    visible = cli._chat_visible_text("\n".join(cli._chat_run_lines(run, include_steps=True)))

    assert _indexes_in_order(
        visible,
        (
            "› ran shell__execute",
            "◇ cancel requested",
            "◇ stopped run_cancel: canceled",
        ),
    )


def test_cli_chat_run_command_stop_records_timeline_and_canceling(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_runtime_json", lambda _ctx, _path: {})
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="working", status="running")
    app.active_run.mark_running()

    app.handle_runtime_event(
        {
            "type": "run_command",
            "payload": {
                "run_id": "run_cancel",
                "kind": "stop",
                "mode": "immediate",
                "ref": {"kind": "command", "index": 1},
            },
        }
    )

    assert app.active_run is not None
    assert app.active_run.status == "canceling"
    visible = cli._chat_visible_text("\n".join(cli._chat_run_lines(app.active_run, include_steps=True)))
    assert "◇ cancel requested" in visible


def test_cli_chat_run_lines_end_with_one_blank_without_leading_blank() -> None:
    first = cli._ChatRun(run_id="run_one", message="first", status="succeeded")
    first.complete_step(
        {
            "kind": "model",
            "step_index": 1,
            "output": [{"type": "text", "text": "first response"}],
        }
    )
    second = cli._ChatRun(run_id="run_two", message="second", status="succeeded")
    second.complete_step(
        {
            "kind": "model",
            "step_index": 1,
            "output": [{"type": "text", "text": "second response"}],
        }
    )

    first_lines = cli._chat_run_lines(first, include_steps=True)
    second_lines = cli._chat_run_lines(second, include_steps=True)

    assert first_lines[-1] == ""
    assert cli._chat_visible_text(first_lines[-2]).strip()
    assert second_lines[0].startswith(cli._chat_ansi_style(cli._CHAT_INPUT_FG, cli._CHAT_INPUT_BG))
    assert not cli._chat_visible_text(second_lines[0]).strip()
    assert cli._chat_visible_text(second_lines[1]).strip()


def test_cli_chat_scrollback_input_bar_keeps_padding() -> None:
    run = cli._ChatRun(run_id="run_input", message="hello", status="succeeded")

    lines = cli._chat_scrollback_user_block(run)

    assert lines[0].startswith(cli._chat_ansi_style(cli._CHAT_INPUT_FG, cli._CHAT_INPUT_BG))
    assert not cli._chat_visible_text(lines[0]).strip()
    assert cli._chat_visible_text(lines[1]).strip() == "> hello"
    assert cli._chat_visible_text(lines[2]).strip() == "run_input"


def test_cli_chat_ai_sdk_tool_result_replaces_running_tool_line(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_runtime_json",
        lambda _ctx, _path: {"default": None, "items": []},
    )
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_tool", message="search", status="running")

    app.record_chat_stream_tool_request(
        {
            "toolCallId": "call_1",
            "toolName": "web_search__search",
            "input": {"query": "李荣浩 热门歌曲 代表作"},
        }
    )

    active = "\n".join(cli._chat_run_lines(app.active_run, include_steps=True))
    assert "› running web_search__search: 李荣浩 热门歌曲 代表作" in active

    app.record_chat_stream_tool_result(
        {
            "toolCallId": "call_1",
            "toolName": "web_search__search",
            "output": {"results": []},
        }
    )

    rendered = "\n".join(cli._chat_run_lines(app.active_run, include_steps=True))
    assert "requested web_search__search" not in rendered
    assert rendered.count("web_search__search") == 1
    assert "› ran web_search__search: 李荣浩 热门歌曲 代表作" in rendered


def test_cli_chat_flow_run_lines_render_stage_summary() -> None:
    run = cli._ChatRun(run_id="run_flow", message="agent framework impl", status="succeeded")
    child = cli._ChatRun(run_id="run_child_1", message="agent framework impl", status="succeeded")
    tool_part = {
        "type": "tool_call",
        "tool_call_id": "tool_1",
        "tool_name": "filesystem__read_text",
        "tool_family": "filesystem__read_text",
        "input": {"path": "task.md"},
    }
    child.record_part({"step_index": 1, "part_index": 0, "part": tool_part})
    child.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "status": "finished",
        "output": [tool_part],
    }
    child.completed_steps[2] = {
        "kind": "tool",
        "step_index": 2,
        "status": "finished",
        "input": [{"step_index": 1, "part_index": 0}],
        "output": [{"type": "tool_result", "tool_name": "filesystem__read_text", "output": "ok"}],
    }
    run.child_runs[child.run_id] = child
    run.completed_steps[1] = {
        "kind": "step",
        "step_index": 1,
        "status": "finished",
        "payload": {
            "op": "prepare_do",
            "stage_index": 0,
            "stage_kind": "do",
            "metadata": {
                "stage_index": 0,
                "stage_total": 2,
                "stage_title": "Expand the research question into diverse search queries",
                "input_preview": "agent framework impl",
                "stage_target": "expand_queries",
            },
        },
        "output": [],
    }
    run.completed_steps[2] = {
        "kind": "run",
        "step_index": 2,
        "status": "finished",
        "payload": {
            "target_kind": "thunk",
            "target": "expand_queries",
            "child_run_ids": ["run_child_1"],
            "stage_index": 0,
            "stage_kind": "do",
            "metadata": {
                "stage_index": 0,
                "stage_total": 2,
                "stage_title": "Expand the research question into diverse search queries",
                "stage_target": "expand_queries",
            },
        },
        "output": [{"type": "text", "text": "query 1\nquery 2\nquery 3"}],
    }
    run.completed_steps[3] = {
        "kind": "step",
        "step_index": 3,
        "status": "finished",
        "payload": {
            "op": "set_current",
            "stage_index": 0,
            "stage_kind": "do",
            "output_preview": "query 1\nquery 2\nquery 3",
            "metadata": {
                "stage_index": 0,
                "stage_total": 2,
                "stage_title": "Expand the research question into diverse search queries",
                "input_preview": "agent framework impl",
            },
        },
        "output": [{"type": "text", "text": "query 1\nquery 2\nquery 3"}],
    }
    run.completed_steps[4] = {
        "kind": "step",
        "step_index": 4,
        "status": "finished",
        "payload": {
            "op": "prepare_each",
            "stage_index": 1,
            "stage_kind": "each",
            "output_preview": {"count": 3},
            "metadata": {
                "stage_index": 1,
                "stage_total": 2,
                "stage_title": "Search the web for each query",
                "parallelism": 4,
                "input_preview": {"count": 3},
            },
        },
        "output": [],
    }
    run.completed_steps[5] = {
        "kind": "run",
        "step_index": 5,
        "status": "finished",
        "payload": {
            "target_kind": "thunk",
            "target": "search_web",
            "child_run_ids": ["run_child_2"],
            "stage_index": 1,
            "stage_kind": "each",
            "metadata": {
                "stage_index": 1,
                "stage_total": 2,
                "stage_title": "Search the web for each query",
                "parallelism": 4,
                "item_count": 3,
                "item_index": 0,
                "lane_index": 0,
            },
        },
        "output": [],
    }
    run.completed_steps[6] = {
        "kind": "model",
        "step_index": 6,
        "status": "finished",
        "output": [{"type": "text", "text": "child internal answer"}],
    }

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "[1] Expand the research question into diverse search queries" in rendered
    assert "1 item -> 3 items" in rendered
    assert "[2] Search the web for each query" in rendered
    assert "1/3 calls" in rendered
    assert "4 lanes" in rendered
    assert "\x1b[2m  ‣ thunk expand_queries · run_child_1 succeeded\x1b[22m" in rendered
    assert "  • requested filesystem__read_text: task.md" in rendered
    assert "  \x1b[2m› ran filesystem__read_text: task.md\x1b[22m" in rendered
    assert "\x1b[2m    ok\x1b[22m" in rendered
    assert "\x1b[2m    ok\x1b[22m\n\n[2] Search the web for each query" in rendered
    assert "lane 1/4 · 1/1 calls" in rendered
    assert "\x1b[2m  ‣ item 1/3 · thunk search_web · run_child_2 succeeded\x1b[22m" in rendered
    assert "child internal answer" not in rendered


def test_cli_chat_run_lines_render_queue_state() -> None:
    run = cli._ChatRun(run_id="run_queue", message="agent framework impl", status="queued")
    run.update_queue(
        "run_queued",
        {
            "run_id": "run_queue",
            "waiting_for": "queue",
            "position": 2,
        },
    )

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "◇ queued run_queue · position 2" in rendered

    run.update_queue(
        "run_waiting",
        {
            "run_id": "run_queue",
            "waiting_for": "thread",
        },
    )

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "◇ waiting run_queue for thread" in rendered


def test_cli_chat_flow_run_lines_keep_terminal_error_message() -> None:
    run = cli._ChatRun(run_id="run_flow_failed", message="agent framework impl", status="failed")
    run.completed_steps[1] = {
        "kind": "step",
        "step_index": 1,
        "status": "finished",
        "payload": {
            "op": "prepare_do",
            "stage_index": 0,
            "stage_kind": "do",
            "metadata": {
                "stage_index": 0,
                "stage_total": 1,
                "stage_title": "Expand the research question",
            },
        },
        "output": [],
    }
    cli._chat_record_system_event(run, "error: backend rejected the request", clear_active=True)

    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "◇ stopped run_flow_failed: failed" in rendered
    assert "[1] Expand the research question" in rendered
    assert "! backend rejected the request" in rendered


def test_cli_chat_header_does_not_show_thread_id() -> None:
    rendered = "\n".join(cli._chat_header_lines("runtime model", home_label="/tmp/toolang/agents/alice"))
    visible = cli._chat_visible_text(rendered)

    assert "Toolang" in visible
    assert "model: runtime model" in visible
    assert "home:  /tmp/toolang/agents/alice" in visible
    assert "history:" not in visible
    assert "thread:" not in visible


def test_cli_chat_model_output_renders_markdown_without_dimming() -> None:
    text = "# Result\n\n**Done**\n\n- first\n- second\n\n```text\nhello\n```"
    run = cli._ChatRun(run_id="run_md", message="format this", status="succeeded")
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "output": [{"type": "text", "text": text}],
    }

    lines = cli._chat_run_lines(run, include_steps=True)
    rendered = "\n".join(lines)
    model_lines = [line for line in lines if "Result" in line or "Done" in line or "first" in line or "hello" in line]

    assert "Result" in rendered
    assert "Done" in rendered
    assert "first" in rendered
    assert "hello" in rendered
    assert "**Done**" not in rendered
    assert all("\x1b[2m" not in line for line in model_lines)


def test_cli_chat_markdown_width_accounts_for_message_prefix(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((120, 24)))

    assert cli._chat_markdown_width() == 100


def test_cli_chat_plain_model_output_wraps_to_content_width(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((44, 24)))
    text = "What can I do for you? Need a hand with files, tasks, services, or a shell command?"
    run = cli._ChatRun(run_id="run_plain", message="hello", status="succeeded")
    run.completed_steps[1] = {
        "kind": "model",
        "step_index": 1,
        "output": [{"type": "text", "text": text}],
    }

    lines = cli._chat_run_lines(run, include_steps=True)
    message_start = next(index for index, line in enumerate(lines) if "What can" in line)
    message_lines = []
    for line in lines[message_start:]:
        if not cli._chat_visible_text(line).strip():
            break
        message_lines.append(line)
    visible = " ".join(cli._chat_visible_text("\n".join(message_lines)).split())

    assert len(message_lines) >= 2
    assert text in visible
    assert all(cli._chat_display_len(line) <= 44 for line in message_lines)


def test_cli_chat_markdown_preserves_section_spacing() -> None:
    lines = cli._chat_render_markdown_lines("# First\n\none\n\n## Second\n\ntwo")

    first_index = next(index for index, line in enumerate(lines) if "First" in line)
    second_index = next(index for index, line in enumerate(lines) if "Second" in line)

    assert lines[first_index + 1] == ""
    assert lines[second_index - 1] == ""
    assert lines[second_index + 1] == ""
    assert "" not in cli._chat_render_markdown_lines("one\n\ntwo")


def test_cli_chat_tool_message_stays_on_one_recommended_width_line(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((42, 24)))
    payload = {
        "output": [
            {
                "type": "tool_result",
                "tool_name": "shell__execute",
                "output": "first line\nsecond line with enough content to exceed the terminal width",
            }
        ]
    }

    lines = cli._chat_tool_message_lines(payload, indent="  ")

    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert cli._chat_visible_text(lines[0]).startswith("    first line second line")
    assert cli._chat_visible_text(lines[0]).endswith("...")
    assert cli._chat_display_len(lines[0]) <= cli._chat_markdown_width()


def test_cli_chat_palette_uses_fixed_neutral_panel_colors() -> None:
    palette = cli._chat_ui_palette()

    assert "last-run" not in palette
    assert palette["queue"] == "fg:#f2f2f2 bg:#3a3a3a"
    assert palette["queue.dim"] == "fg:#b8b8b8 bg:#3a3a3a"
    assert palette["normal-input"] == "fg:#f5f5f5 bg:#444444"
    assert palette["normal-input.dim"] == "fg:#b8b8b8 bg:#444444"
    assert palette["input"] == "fg:#f5f5f5 bg:#444444"
    assert palette["steer-input"] == "fg:#f5f5f5 bg:#2f555d"
    assert palette["steer-input.dim"] == "fg:#b8b8b8 bg:#2f555d"
    assert palette["status"] == "fg:#f2f2f2 bg:#5a5a5a"
    assert len({palette["status.model"], palette["status.thunk"], palette["status.flow"]}) == 3
    assert palette["cursor"] == "fg:#111111 bg:#eeeeee"
    assert palette["input.cursor"] == "fg:#111111 bg:#eeeeee"


def test_cli_chat_user_block_uses_input_panel_colors(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((20, 24)))

    line = cli._chat_input_block_line("hello")

    assert cli._chat_ui_palette()["input"] == cli._chat_prompt_style(cli._CHAT_INPUT_FG, cli._CHAT_INPUT_BG)
    assert line.startswith(cli._chat_ansi_style(cli._CHAT_INPUT_FG, cli._CHAT_INPUT_BG))
    assert cli._chat_visible_text(line) == "hello               "


def test_cli_chat_prompt_text_change_does_not_force_app_invalidation() -> None:
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    prompt = cli._ChatPromptBox(lambda _event: None, invalidate, "runtime model")

    prompt.buffer.text = "hello"

    assert invalidations == 0


def test_cli_chat_prompt_status_uses_horizontal_padding() -> None:
    prompt = cli._ChatPromptBox(lambda _event: None, lambda: None, "runtime model")

    status_text = "".join(text for _style, text in prompt.render_status())

    assert status_text.startswith("  runtime model")
    assert "^c cancel  ^d exit  ^j newline  ↑↓ history" in status_text
    assert status_text.endswith("  ")

    prompt.set_error("failed")
    error_text = "".join(text for _style, text in prompt.render_status())

    assert error_text == "  ! failed  "


def test_cli_chat_prompt_status_colors_model_thunk_and_flow() -> None:
    flow_segments = cli._ChatPromptBox(lambda _event: None, lambda: None, "runtime model  flow:review").render_status()
    thunk_segments = cli._ChatPromptBox(lambda _event: None, lambda: None, "openai/o3  thunk:summarize").render_status()

    assert ("class:status.model", "  runtime model") in flow_segments
    assert ("class:status.flow", "flow:review") in flow_segments
    assert ("class:status.model", "  openai/o3") in thunk_segments
    assert ("class:status.thunk", "thunk:summarize") in thunk_segments


def test_cli_chat_prompt_cancel_and_exit_key_bindings() -> None:
    prompt = cli._ChatPromptBox(lambda _event: None, lambda: None, "runtime model")
    keys = cli.KeyBindings()

    prompt.bind(keys)

    bindings = {tuple(str(key) for key in binding.keys) for binding in keys.bindings}
    assert ("Keys.ControlC",) in bindings
    assert ("Keys.ControlD",) in bindings
    assert ("Keys.ControlQ",) in bindings
    assert ("Keys.Escape",) not in bindings
    assert ("Keys.Escape", "Keys.Escape") in bindings
    assert ("Keys.Escape", "Keys.ControlM") in bindings


def test_cli_chat_input_history_store_persists_multiline_entries(tmp_path: Path) -> None:
    path = tmp_path / "chat-input-history.jsonl"
    store = ChatInputHistoryStore(path, limit=10)

    store.append("hello")
    store.append("line 1\nline 2")
    with path.open("a", encoding="utf-8") as file:
        file.write("not json\n")
        file.write('{"text": ""}\n')

    assert store.load() == ["hello", "line 1\nline 2"]


def test_cli_chat_prompt_loads_and_records_persistent_history(tmp_path: Path) -> None:
    store = ChatInputHistoryStore(tmp_path / "chat-input-history.jsonl", limit=10)
    store.append("first")
    store.append("second")

    prompt = cli._ChatPromptBox(lambda _event: None, lambda: None, "runtime model", history_store=store)
    assert prompt.history_entries() == ["first", "second"]

    prompt.record_history("third")
    prompt.record_history("third")

    assert prompt.history_entries() == ["first", "second", "third"]
    assert ChatInputHistoryStore(store.path, limit=10).load() == ["first", "second", "third"]


def test_cli_chat_input_history_store_compacts_old_entries(tmp_path: Path) -> None:
    store = ChatInputHistoryStore(tmp_path / "chat-input-history.jsonl", limit=3, compact_limit=3)

    for index in range(7):
        store.append(f"message {index}")

    assert store.load() == ["message 4", "message 5", "message 6"]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 3


def test_cli_chat_startup_resolves_selected_model_label(monkeypatch) -> None:
    calls = 0

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert request_path == "/api/v1/chat/models"
        return {
            "default": "openai/o3",
            "items": [
                {
                    "selector": "[deepseek]",
                    "ref": "deepseek/deepseek-v4-flash",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                }
            ],
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={"models": ["[deepseek]"]})

    assert calls == 1
    assert app.status_label() == "deepseek/deepseek-v4-flash"
    assert "model: deepseek/deepseek-v4-flash" in cli._chat_visible_text(
        "\n".join(cli._chat_header_lines(app.status_label(), home_label=app.home_label))
    )


def test_cli_chat_model_command_keeps_previous_selector_on_miss(monkeypatch) -> None:
    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/chat/models"
        return {
            "default": "openai/o3[openai]",
            "items": [
                {
                    "selector": "openai/o3[openai]",
                    "ref": "openai/o3",
                    "provider": "openai",
                    "model": "o3",
                }
            ],
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)

    selector_payload: dict[str, object] = {"models": ["openai/o3[openai]"]}
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload=selector_payload)

    app.handle_submit("/model no/such-model")

    assert selector_payload == {"models": ["openai/o3[openai]"]}
    assert app.model_label == "openai/o3"
    assert app.prompt.error_message == "Model selector matched no models: no/such-model"


def test_cli_chat_bottom_layout_can_shrink_to_compact_prompt() -> None:
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    root = app.build_layout().container
    height = root.preferred_height(80, 1)

    assert height.min == 1
    assert height.preferred == app.bottom_rows()


def test_cli_chat_model_command_lists_models_without_starting_run(monkeypatch) -> None:
    writes: list[list[str]] = []
    posts: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/chat/models"
        return {
            "default": "openai/o3[openai]",
            "items": [
                {"selector": "openai/o3[openai]", "provider": "openai", "adapter": "responses"},
                {"selector": "openai/gpt-5[openai]", "provider": "openai", "adapter": "responses"},
            ],
        }

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})

    app.handle_submit("/model")

    rendered = "\n".join(writes[0])
    visible = cli._chat_visible_text(rendered)
    assert posts == []
    assert app.active_run is None
    assert "chat command" not in visible
    assert "◇ available models" in visible
    assert "\x1b[2m◇ available models" not in rendered
    assert "openai/o3[openai] default  openai responses" in visible
    assert "openai/gpt-5[openai]  openai responses" in visible


def test_cli_chat_model_command_updates_selected_model(monkeypatch) -> None:
    writes: list[list[str]] = []
    posts: list[tuple[str, object]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        assert request_path == "/api/v1/chat/models"
        return {
            "default": "openai/o3[openai]",
            "items": [
                {
                    "selector": "openai/o3[openai]",
                    "ref": "openai/o3",
                    "provider": "openai",
                    "model": "o3",
                }
            ],
        }

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    selector_payload: dict[str, object] = {}
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload=selector_payload)

    app.handle_submit("/model openai/o3")

    assert posts == []
    assert app.active_run is None
    assert selector_payload["models"] == ["openai/o3"]
    assert app.prompt.status_label == "openai/o3"
    assert "model: openai/o3" in cli._chat_visible_text("\n".join(writes[0]))


def test_cli_chat_thunk_and_flow_commands_list_and_switch(monkeypatch) -> None:
    writes: list[list[str]] = []
    posts: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        if request_path == "/api/v1/chat/thunks":
            return {"default": "chat", "items": [{"name": "chat"}, {"name": "summarize"}]}
        if request_path == "/api/v1/chat/flows":
            return {"default": None, "items": [{"name": "review"}]}
        raise AssertionError(request_path)

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    selector_payload: dict[str, object] = {}
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload=selector_payload)

    app.handle_submit("/thunk")
    app.handle_submit("/thunk summarize")
    app.handle_submit("/flow review")
    app.handle_submit("/flow")

    visible = cli._chat_visible_text("\n".join(line for lines in writes for line in lines))
    assert posts == []
    assert app.active_run is None
    assert "◇ available thunks" in visible
    assert "chat  default" in visible
    assert "summarize" in visible
    assert "thunk: summarize" in visible
    assert "flow: review" in visible
    assert "review  current" in visible
    assert selector_payload == {"flow": "review"}
    assert app.prompt.status_label == "runtime model  flow:review"


def test_cli_chat_queue_panel_uses_current_numbering() -> None:
    items = [
        cli._ChatQueueItem(kind="run", text="first request"),
        cli._ChatQueueItem(kind="run", text="second request"),
    ]
    panel = cli._ChatSubmissionQueue(lambda: items)

    del items[0]

    assert panel.lines() == ["  queued for submission:", "  [1] second request"]
    rendered = panel.render()
    rendered_text = "".join(text for _style, text in rendered)
    assert ("class:queue.dim", "  queued for submission:") in rendered
    assert ("class:queue.dim", "[1]") in rendered
    assert ("class:queue", " second request") in rendered
    assert "[1] second request" in rendered_text
    height = panel.height_dimension()
    assert height.preferred == 2
    assert height.max == 2


def test_cli_chat_queue_commands_help_delete_edit_and_clear(monkeypatch) -> None:
    writes: list[list[str]] = []

    monkeypatch.setattr(cli, "_runtime_json", lambda _ctx, _path: {})
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.pending = [
        cli._ChatQueueItem(kind="run", text="first request"),
        cli._ChatQueueItem(kind="run", text="second request"),
    ]

    app.handle_submit("/queue")
    app.handle_submit("/q d 1")
    app.handle_submit("/q e 1")
    app.pending = [cli._ChatQueueItem(kind="run", text="third request")]
    app.handle_submit("/q c")

    visible = cli._chat_visible_text("\n".join(line for lines in writes for line in lines))
    assert "Queue Commands" in visible
    assert "/queue steer N" in visible
    assert "/q s N" in visible
    assert "#2 run: second request" not in visible
    assert "deleted queue" not in visible
    assert "editing queue" not in visible
    assert "queue cleared" not in visible
    assert app.pending == []
    assert app.prompt.buffer.text == "second request"


def test_cli_chat_queue_steer_sends_pending_item_to_active_run(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    writes: list[list[str]] = []

    monkeypatch.setattr(cli, "_runtime_json", lambda _ctx, _path: {})
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append((request_path, payload))
        return {"input": {}}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    app = cli._ChatBottomApp(cast(Any, object()), thread_id="term_existing", selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_active", message="working", status="running", accept_child_trace=True)
    app.pending = [cli._ChatQueueItem(kind="run", text="please adjust")]

    app.handle_submit("/q s 1")

    assert calls == [
        (
            "/api/v1/runs/run_active/steer",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "please adjust"}]}},
        )
    ]
    assert app.pending == []
    assert writes == []


def test_cli_chat_queue_run_is_not_supported(monkeypatch) -> None:
    writes: list[list[str]] = []

    monkeypatch.setattr(cli, "_runtime_json", lambda _ctx, _path: {})
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.pending = [cli._ChatQueueItem(kind="steer", text="continue as a run")]

    app.handle_submit("/queue r 1")

    assert app.pending == [cli._ChatQueueItem(kind="steer", text="continue as a run")]
    assert app.prompt.error_message == "Unknown queue command: r"
    assert writes == []


def test_cli_chat_normal_submit_after_flow_selection_still_starts_run(monkeypatch) -> None:
    streams: list[tuple[str, dict[str, object]]] = []
    posts: list[tuple[str, object]] = []

    class FakeListener:
        def stop(self) -> None:
            pass

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        del event_handler
        streams.append((request_path, payload))

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", lambda _ctx, _thread_id, **_kwargs: FakeListener())

    selector_payload: dict[str, object] = {}
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload=selector_payload)

    app.handle_submit("/flow review")
    app.handle_submit("hello")
    time.sleep(0.1)

    assert posts == [("/api/v1/threads", {"client": "tui"})]
    assert len(streams) == 1
    request_path, payload = streams[0]
    assert request_path == "/api/v1/chat/stream"
    assert payload["message"] == {"role": "user", "parts": [{"type": "text", "text": "hello"}]}
    assert payload["flow"] == "review"
    assert "thunk" not in payload


def test_cli_chat_flow_submit_renders_flow_stream_steps(monkeypatch) -> None:
    printed: list[list[str]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        assert request_path == "/api/v1/chat/stream"
        assert payload["flow"] == "research"
        assert event_handler is not None
        event_handler(
            {
                "type": "start",
                "messageId": "run_flow",
                "messageMetadata": {"run_id": "run_flow", "thread_id": "term_new"},
            }
        )
        event_handler(
            {
                "type": "step_start",
                "payload": {
                    "run_id": "run_flow",
                    "thread_id": "term_new",
                    "step_index": 1,
                    "kind": "step",
                },
            }
        )
        event_handler(
            {
                "type": "step_end",
                "payload": {
                    "run_id": "run_flow",
                    "thread_id": "term_new",
                    "step_index": 1,
                    "kind": "step",
                    "status": "finished",
                    "output": [],
                    "payload": {
                        "op": "prepare_do",
                        "stage_kind": "do",
                        "stage_index": 0,
                        "stage_total": 6,
                        "metadata": {"stage_title": "Expand the research question into diverse search queries"},
                    },
                },
            }
        )
        event_handler({"type": "finish"})

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    selector_payload: dict[str, object] = {}
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload=selector_payload)
    monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_runtime_event(cast(dict[str, Any], event.value)))

    app.handle_submit("/flow research")
    app.handle_submit("agent framewrok implemenations")
    time.sleep(0.1)

    visible = cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
    assert selector_payload == {"flow": "research"}
    assert app.active_run is None
    assert "flow: research" in visible
    assert "[1] Expand the research question into diverse search queries" in visible
    assert "ran prepare_do" not in visible


def test_cli_chat_submit_does_not_start_thread_listener(monkeypatch) -> None:
    streams: list[tuple[str, dict[str, object]]] = []
    listener_started = False

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        del event_handler
        streams.append((request_path, payload))

    def fake_start_thread_event_listener(_ctx: Any, _thread_id: str, **_kwargs: object) -> object:
        nonlocal listener_started
        listener_started = True
        return object()

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", fake_start_thread_event_listener)

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})

    app.handle_submit("hello")
    time.sleep(0.1)

    assert not listener_started
    assert streams
    assert app.active_run is not None
    assert app.active_run.message == "hello"


def test_cli_chat_start_run_consumes_own_stream_events(monkeypatch) -> None:
    printed: list[list[str]] = []

    class FakeListener:
        def stop(self) -> None:
            pass

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        assert request_path == "/api/v1/chat/stream"
        assert event_handler is not None
        request_id = cast(str, payload["request_id"])
        event_handler(
            {
                "type": "run_command",
                "payload": {
                    "kind": "start",
                    "run_id": "run_local",
                    "request_id": request_id,
                    "message": payload["message"],
                },
            }
        )
        event_handler(
            {
                "type": "step_end",
                "payload": {
                    "run_id": "run_local",
                    "step_index": 1,
                    "kind": "model",
                    "output": [{"type": "text", "text": "done"}],
                },
            }
        )
        event_handler({"type": "run_end", "payload": {"run_id": "run_local", "status": "finished"}})

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", lambda _ctx, _thread_id, **_kwargs: FakeListener())
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    loop = asyncio.new_event_loop()
    app.loop = loop
    try:
        monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_runtime_event(cast(dict[str, Any], event.value)))
        app.handle_submit("hello")
        time.sleep(0.1)
        assert printed
        assert "done" in cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
        assert app.active_run is None
    finally:
        loop.close()


def test_cli_chat_start_run_handles_ai_sdk_stream_events(monkeypatch) -> None:
    printed: list[list[str]] = []

    class FakeListener:
        def stop(self) -> None:
            pass

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        assert request_path == "/api/v1/chat/stream"
        assert payload["message"] == {"role": "user", "parts": [{"type": "text", "text": "hello"}]}
        assert event_handler is not None
        event_handler(
            {
                "type": "start",
                "messageId": "run_ai",
                "messageMetadata": {"run_id": "run_ai", "thread_id": "term_new"},
            }
        )
        event_handler({"type": "start-step"})
        event_handler({"type": "text-start", "id": "msg_run_ai"})
        event_handler({"type": "text-delta", "id": "msg_run_ai", "delta": "hello"})
        event_handler({"type": "text-delta", "id": "msg_run_ai", "delta": " world"})
        event_handler({"type": "text-end", "id": "msg_run_ai"})
        event_handler({"type": "finish-step"})
        event_handler(
            {
                "type": "finish",
                "finishReason": "stop",
                "messageMetadata": {"run_id": "run_ai", "thread_id": "term_new"},
            }
        )

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", lambda _ctx, _thread_id, **_kwargs: FakeListener())
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_runtime_event(cast(dict[str, Any], event.value)))

    app.handle_submit("hello")
    time.sleep(0.1)

    assert app.active_run is None
    assert printed
    visible = cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
    assert "hello world" in visible
    assert "run_ai" in visible


def test_cli_chat_cancel_active_run_posts_cancel(monkeypatch) -> None:
    sent: list[str] = []

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    monkeypatch.setattr(app, "send_cancel_request", sent.append)

    app.handle_cancel()

    assert app.active_run is not None
    assert app.active_run.status == "canceling"
    assert app.active_run.cancel_requested is True
    assert sent == ["run_cancel"]
    assert app.prompt.error_message == ""


def test_cli_chat_interrupt_clears_input_before_cancel(monkeypatch) -> None:
    sent: list[str] = []

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    app.prompt.buffer.text = "draft"
    monkeypatch.setattr(app, "send_cancel_request", sent.append)

    app.handle_interrupt()

    assert app.prompt.buffer.text == ""
    assert app.active_run is not None
    assert app.active_run.status == "running"
    assert sent == []


def test_cli_chat_interrupt_cancels_active_run_when_input_empty(monkeypatch) -> None:
    sent: list[str] = []

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    monkeypatch.setattr(app, "send_cancel_request", sent.append)

    app.handle_interrupt()

    assert app.active_run is not None
    assert app.active_run.status == "canceling"
    assert sent == ["run_cancel"]


def test_cli_chat_interrupt_does_not_exit_when_idle(monkeypatch) -> None:
    exited = False
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})

    def nonlocal_set_exited() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "handle_quit", nonlocal_set_exited)

    app.handle_interrupt()

    assert exited is False


def test_cli_chat_eof_only_exits_when_idle_with_empty_input(monkeypatch) -> None:
    exits = 0
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})

    def fake_quit() -> None:
        nonlocal exits
        exits += 1

    monkeypatch.setattr(app, "handle_quit", fake_quit)

    app.prompt.buffer.text = "draft"
    app.handle_eof()
    app.prompt.buffer.text = ""
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    app.handle_eof()
    app.active_run = None
    app.handle_eof()

    assert exits == 1


def test_cli_chat_exit_commands_force_quit(monkeypatch) -> None:
    exits = 0
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    app.prompt.buffer.text = "draft"

    def fake_quit() -> None:
        nonlocal exits
        exits += 1

    monkeypatch.setattr(app, "handle_quit", fake_quit)

    assert app.handle_local_command("/quit") is True
    assert app.handle_local_command("/exit") is True
    assert exits == 2


def test_cli_chat_send_cancel_request_uses_empty_payload(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append((request_path, payload))
        return {}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.send_cancel_request("run_cancel")

    deadline = time.monotonic() + 1
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)

    assert calls == [("/api/v1/runs/run_cancel/cancel", {})]


def test_cli_chat_cancel_waits_until_run_starts(monkeypatch) -> None:
    sent: list[str] = []

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="", message="hello", status="submitting", accept_child_trace=True)
    monkeypatch.setattr(app, "send_cancel_request", sent.append)

    app.handle_cancel()
    app.handle_chat_stream_start({"messageMetadata": {"run_id": "run_late", "thread_id": "term_new"}})

    assert app.active_run is not None
    assert app.active_run.status == "canceling"
    assert app.active_run.started is True
    assert sent == ["run_late"]


def test_cli_chat_cancel_failure_stays_in_active_run_progress() -> None:
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    app.active_run.request_cancel()

    app.handle_cancel_error(
        "runtime request failed: Error code: 409 - {'error': {'message': 'run is not running'}}"
    )

    assert app.active_run is not None
    assert app.active_run.status == "running"
    assert app.prompt.error_message == ""
    visible = cli._chat_visible_text("\n".join(cli._chat_run_lines(app.active_run, include_steps=True)))
    assert "! cancel failed: run is not running" in visible


def test_cli_chat_canceled_run_state_line_is_dim() -> None:
    line = cli._chat_run_state_line(cli._ChatRun(run_id="run_cancel", message="hello", status="canceled"))

    assert cli._chat_visible_text(line) == "◇ stopped run_cancel: canceled"
    assert line.startswith(cli._CHAT_DIM)


def test_cli_chat_canceled_run_end_finishes_active_progress(monkeypatch) -> None:
    printed: list[list[str]] = []
    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_cancel", message="hello", status="running", accept_child_trace=True)
    app.active_run.mark_running()
    app.active_run.request_cancel()
    app.local_streaming.set()
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app.handle_runtime_event(
        {
            "type": "run_end",
            "event_type": "run_end",
            "payload": {
                "run_id": "run_cancel",
                "thread_id": "term_new",
                "status": "canceled",
                "finished_at": "2026-01-01T00:00:01Z",
            },
        }
    )

    assert app.active_run is None
    assert not app.has_active_run()
    visible = cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
    assert "◇ stopped run_cancel: canceled" in visible


def test_cli_chat_stream_thread_exceptions_surface_as_ui_errors(monkeypatch) -> None:
    printed: list[list[str]] = []

    class FakeListener:
        def stop(self) -> None:
            pass

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        del payload, event_handler
        assert request_path == "/api/v1/chat/stream"
        raise RuntimeError("stream parser exploded")

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", lambda _ctx, _thread_id, **_kwargs: FakeListener())
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_error(str(event.value)))

    app.handle_submit("hello")
    time.sleep(0.1)

    assert app.active_run is None
    assert app.prompt.error_message == ""
    assert printed


def test_cli_chat_stream_error_after_queue_clears_active_run(monkeypatch) -> None:
    printed: list[list[str]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        assert request_path == "/api/v1/chat/stream"
        assert payload["models"] == ["missing"]
        assert event_handler is not None
        event_handler(
            {
                "type": "run_queued",
                "event_type": "run_queued",
                "payload": {
                    "run_id": "run_missing",
                    "thread_id": "term_new",
                    "position": 1,
                    "waiting_for": "queue",
                },
            }
        )
        event_handler({"type": "error", "errorText": "No matched models."})
        event_handler({"type": "finish"})

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={"models": ["missing"]})
    monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_runtime_event(cast(dict[str, Any], event.value)))

    app.handle_submit("hello")
    time.sleep(0.1)

    assert app.active_run is None
    visible = cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
    assert "queued run_missing" in visible
    assert "! No matched models." in visible
    assert "◇ stopped run_missing: failed" in visible


def test_cli_chat_ai_sdk_stream_error_survives_finish_event(monkeypatch) -> None:
    printed: list[list[str]] = []

    class FakeListener:
        def stop(self) -> None:
            pass

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        assert request_path == "/api/v1/threads"
        assert payload == {"client": "tui"}
        return {"thread_id": "term_new"}

    def fake_runtime_consume_stream(
        _ctx: Any,
        request_path: str,
        *,
        payload: dict[str, object],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        del payload
        assert request_path == "/api/v1/chat/stream"
        assert event_handler is not None
        event_handler(
            {
                "type": "start",
                "messageId": "run_failed",
                "messageMetadata": {"run_id": "run_failed", "thread_id": "term_new"},
            }
        )
        event_handler({"type": "error", "errorText": "backend rejected the request"})
        event_handler({"type": "finish"})

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_runtime_consume_stream", fake_runtime_consume_stream)
    monkeypatch.setattr(cli, "_start_thread_event_listener", lambda _ctx, _thread_id, **_kwargs: FakeListener())
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: printed.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})
    monkeypatch.setattr(app, "emit_from_thread", lambda event: app.handle_runtime_event(cast(dict[str, Any], event.value)))

    app.handle_submit("hello")
    time.sleep(0.1)

    assert app.active_run is None
    assert app.prompt.error_message == ""
    assert printed
    visible = cli._chat_visible_text("\n".join(line for lines in printed for line in lines))
    assert "◇ stopped run_failed: failed" in visible
    assert "! backend rejected the request" in visible


def test_cli_chat_existing_thread_does_not_start_thread_listener(monkeypatch) -> None:
    listener_started = False

    def fake_start_thread_event_listener(_ctx: Any, _thread_id: str, **_kwargs: object) -> object:
        nonlocal listener_started
        listener_started = True
        return object()

    monkeypatch.setattr(cli, "_start_thread_event_listener", fake_start_thread_event_listener)

    app = cli._ChatBottomApp(cast(Any, object()), thread_id="term_test", selector_payload={"thunk": "summarize"})

    assert app.ensure_thread_id() == "term_test"
    assert not listener_started


def test_cli_chat_thread_listener_ignores_child_run_trace_events() -> None:
    app = cli._ChatBottomApp(cast(Any, object()), thread_id="term_existing", selector_payload={})
    app.active_run = cli._ChatRun(run_id="run_top", message="hello", status="running")

    app.handle_runtime_event(
        {
            "type": "run_start",
            "payload": {
                "run_id": "run_child",
                "thread_id": "term_existing",
                "parent_run_id": "run_top",
                "call_kind": "child",
                "input": {"role": "user", "parts": [{"type": "text", "text": "child"}]},
            },
        }
    )
    app.handle_runtime_event(
        {
            "type": "step_end",
            "payload": {
                "run_id": "run_child",
                "thread_id": "term_existing",
                "step_index": 1,
                "kind": "model",
                "output": [{"type": "text", "text": "child output"}],
            },
        }
    )

    assert app.active_run is not None
    assert app.active_run.run_id == "run_top"
    assert app.active_run.completed_steps == {}


def test_cli_chat_own_stream_projects_child_run_trace_events() -> None:
    app = cli._ChatBottomApp(cast(Any, object()), thread_id="term_existing", selector_payload={})
    app.active_run = cli._ChatRun(
        run_id="run_top",
        message="hello",
        status="running",
        executable_kind="flow",
        executable_name="research",
        accept_child_trace=True,
    )
    app.active_run.completed_steps[1] = {
        "kind": "step",
        "step_index": 1,
        "status": "finished",
        "payload": {
            "op": "prepare_do",
            "stage_index": 0,
            "stage_kind": "do",
            "metadata": {"stage_index": 0, "stage_title": "Expand query"},
        },
        "output": [],
    }
    app.active_run.completed_steps[2] = {
        "kind": "run",
        "step_index": 2,
        "status": "finished",
        "payload": {
            "target_kind": "thunk",
            "target": "expand",
            "child_run_ids": ["run_child"],
            "stage_index": 0,
            "stage_kind": "do",
            "metadata": {"stage_index": 0, "stage_title": "Expand query"},
        },
        "output": [],
    }

    app.handle_runtime_event(
        {
            "type": "run_start",
            "payload": {
                "run_id": "run_child",
                "root_run_id": "run_top",
                "parent_run_id": "run_top",
                "thread_id": "term_existing",
                "executable_kind": "thunk",
                "executable_name": "expand",
                "input": {"role": "user", "parts": [{"type": "text", "text": "child"}]},
            },
        }
    )
    app.handle_runtime_event(
        {
            "type": "step_end",
            "payload": {
                "run_id": "run_child",
                "thread_id": "term_existing",
                "step_index": 1,
                "kind": "model",
                "status": "finished",
                "output": [{"type": "text", "text": "child output"}],
            },
        }
    )

    rendered = "\n".join(cli._chat_run_lines(app.active_run, include_steps=True))

    assert "thunk expand · run_child running" in rendered
    assert "• child output" in rendered


def test_cli_chat_help_command_shows_local_help_without_starting_run(monkeypatch) -> None:
    writes: list[list[str]] = []
    posts: list[tuple[str, object]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_write_lines", lambda lines, **_kwargs: writes.append(lines))

    app = cli._ChatBottomApp(cast(Any, object()), thread_id=None, selector_payload={})

    app.handle_submit("/?")

    visible = cli._chat_visible_text("\n".join(writes[0]))
    rendered = "\n".join(writes[0])
    assert posts == []
    assert app.active_run is None
    assert "◇ Slash Commands" in visible
    assert "\x1b[2m◇ Slash Commands" not in rendered
    assert "/model [SELECTOR]  List or switch models." in visible
    assert "/thunk [NAME]      List or use a thunk." in visible
    assert "/flow [NAME]       List or use a flow." in visible
    assert "/exit, /quit       Exit chat." in visible
    assert "\n  \n  /model" not in visible


def test_cli_chat_active_step_lines_use_lightweight_event_text() -> None:
    run = cli._ChatRun(run_id="run_active", message="check weather", status="running")
    run.tool_calls_by_part[(1, 0)] = cli._ChatToolCall("weather", {"query": "杭州天气"})

    run.start_step({"kind": "model", "step_index": 1})
    run.start_step({"kind": "tool", "step_index": 2, "input": [{"step_index": 1, "part_index": 0}]})

    assert cli._chat_active_step_line(run.steps[1]) == "• thinking..."
    assert "› running weather" in cli._chat_active_step_line(run.steps[2])


def test_cli_chat_error_replaces_active_step_with_friendly_system_line() -> None:
    run = cli._ChatRun(run_id="run_failed", message="再来一次", status="running")
    run.start_step({"kind": "model", "step_index": 1})
    error = (
        "Error code: 400 - {'error': {'message': \"Messages with role 'tool' must be a response\", "
        "'type': 'invalid_request_error'}}"
    )

    cli._chat_record_system_event(run, f"error: {cli._chat_friendly_error(error)}", clear_active=True)
    rendered = "\n".join(cli._chat_run_lines(run, include_steps=True))

    assert "thinking..." not in rendered
    assert "! Messages with role 'tool' must be a response" in rendered
    assert "Messages with role 'tool' must be a response" in rendered
    assert "invalid_request_error" not in rendered
    assert "! run run_failed" not in rendered


def test_cli_thread_event_renderer_prints_thread_messages(capsys) -> None:
    renderer = cli._ThreadEventRenderer()

    renderer.render(
        {
            "type": "run_command",
            "payload": {
                "kind": "start",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hello from web"}]},
            },
        }
    )
    renderer.render({"type": "part_delta", "payload": {"delta": {"type": "text", "text": "hi"}}})
    renderer.render({"type": "part_delta", "payload": {"delta": {"type": "text", "text": " there"}}})
    renderer.render({"type": "run_end", "payload": {"status": "finished"}})

    assert capsys.readouterr().out == "\nuser: hello from web\nassistant: hi there\n"


def test_cli_thread_event_renderer_redraws_prompt_for_remote_run(capsys) -> None:
    renderer = cli._ThreadEventRenderer(redraw_prompt=True)

    renderer.render({"type": "part_delta", "payload": {"run_id": "run_web", "delta": {"type": "text", "text": "hi"}}})
    renderer.render({"type": "run_end", "payload": {"run_id": "run_web", "status": "finished"}})

    assert capsys.readouterr().out == "assistant: hi\n> "


def test_cli_thread_event_renderer_skips_prompt_during_local_stream(capsys) -> None:
    local_streaming = cli.threading.Event()
    local_streaming.set()
    renderer = cli._ThreadEventRenderer(redraw_prompt=True, local_streaming=local_streaming)

    renderer.render({"type": "part_delta", "payload": {"run_id": "run_tui", "delta": {"type": "text", "text": "hi"}}})
    renderer.render({"type": "run_end", "payload": {"run_id": "run_tui", "status": "finished"}})

    assert capsys.readouterr().out == "assistant: hi\n"


def test_cli_thread_event_renderer_skips_prompt_for_local_request(capsys) -> None:
    local_request_ids = {"term_req"}
    renderer = cli._ThreadEventRenderer(redraw_prompt=True, local_request_ids=local_request_ids)

    renderer.render(
        {
            "type": "run_command",
            "payload": {
                "run_id": "run_tui",
                "request_id": "term_req",
                "kind": "start",
                "message": {"role": "user", "parts": [{"type": "text", "text": "local"}]},
            },
        }
    )
    renderer.render({"type": "part_delta", "payload": {"run_id": "run_tui", "delta": {"type": "text", "text": "hi"}}})
    renderer.render({"type": "run_end", "payload": {"run_id": "run_tui", "status": "finished"}})

    assert capsys.readouterr().out == "\nuser: local\nassistant: hi\n"


def test_cli_thread_event_renderer_prints_step_end_without_streaming_delta(capsys) -> None:
    renderer = cli._ThreadEventRenderer()

    renderer.render(
        {
            "type": "step_end",
            "payload": {
                "run_id": "run_non_streaming",
                "kind": "model",
                "output": [{"type": "text", "text": "complete answer"}],
            },
        }
    )
    renderer.render({"type": "run_end", "payload": {"run_id": "run_non_streaming", "status": "finished"}})

    assert capsys.readouterr().out == "assistant: complete answer\n"


def test_cli_chat_help_uses_thread_option() -> None:
    result = _invoke_app(["chat", "dev", "--help"])

    assert result.exit_code == 0
    assert "TARGET_OR_MESSAGE" not in result.stdout
    assert "[THREAD]" in result.stdout
    assert "[THREAD_OR_RUN]" not in result.stdout
    assert "[TARGET]" not in result.stdout
    assert "--thread" not in result.stdout
    assert "--tui" not in result.stdout
    assert "--ui" not in result.stdout
    assert "--models" in result.stdout
    assert "--tools" in result.stdout
    assert "--caps" in result.stdout
    assert "--thunk" in result.stdout
    assert "--flow" in result.stdout
    assert "--model         " not in result.stdout
    assert "thread      [THREAD]" in result.stdout
    assert "target      [THREAD]" not in result.stdout
    assert "Thread id to continue. Run id also accepted. Omit" in result.stdout
    assert "to start a new one." in result.stdout


def test_cli_hidden_lists_hidden_commands_without_help_leak() -> None:
    help_result = _invoke_app(["--help"])
    hidden_result = _invoke_app(["hidden"])

    assert help_result.exit_code == 0
    assert "hidden" not in help_result.stdout
    assert hidden_result.exit_code == 0
    assert "Usage:" in hidden_result.stdout
    assert " hidden [OPTIONS]" in hidden_result.stdout
    assert "Show commands hidden from the main help." in hidden_result.stdout
    assert "Run with: root COMMAND [OPTIONS]" in hidden_result.stdout
    assert "Advanced Commands" in hidden_result.stdout
    assert "Alias Commands" in hidden_result.stdout
    assert "send" in hidden_result.stdout
    assert "Send one message to a thread." in hidden_result.stdout
    assert "attach" in hidden_result.stdout
    assert "Open chat on a thread." in hidden_result.stdout
    assert "fmt" in hidden_result.stdout
    assert "Show hidden commands." not in hidden_result.stdout


def test_cli_inspect_run_tree_uses_run_graph(monkeypatch) -> None:
    calls: list[str] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(request_path)
        if request_path == "/api/v1/runs/run_parent":
            return _inspect_run_detail("run_parent", thread_id="term_thread", root_run_id="run_parent")
        if request_path == "/api/v1/threads/term_thread?limit=100":
            return {
                "info": {"id": "term_thread"},
                "runs": [
                    _inspect_run_detail(
                        "run_parent",
                        thread_id="term_thread",
                        root_run_id="run_parent",
                        steps=[
                            {
                                "record": {
                                    "step_index": 1,
                                    "kind": "step",
                                    "status": "finished",
                                    "payload": {
                                        "stage_index": 0,
                                        "stage_total": 2,
                                        "stage_kind": "rank",
                                        "stage_title": "Rank candidates",
                                        "op": "prepare_rank",
                                        "output_preview": {"count": 3},
                                        "metadata": {"input_preview": {"count": 3}, "parallelism": 2},
                                    },
                                    "output": [],
                                },
                                "message": None,
                            },
                            {
                                "record": {
                                    "step_index": 2,
                                    "kind": "run",
                                    "status": "finished",
                                    "payload": {
                                        "target_kind": "thunk",
                                        "target": "score",
                                        "child_run_ids": ["run_child"],
                                        "parallelism": 2,
                                        "lane_index": 0,
                                        "stage_index": 0,
                                        "stage_total": 2,
                                        "stage_kind": "rank",
                                        "item_indexes": [0],
                                        "metadata": {
                                            "stage_title": "Rank candidates",
                                            "parallelism": 2,
                                            "item_count": 3,
                                        },
                                    },
                                    "output": [],
                                },
                                "message": None,
                            },
                            {
                                "record": {
                                    "step_index": 3,
                                    "kind": "step",
                                    "status": "finished",
                                    "payload": {
                                        "stage_index": 0,
                                        "stage_total": 2,
                                        "stage_kind": "rank",
                                        "stage_title": "Rank candidates",
                                        "op": "set_current",
                                        "output_preview": {"count": 2},
                                        "metadata": {"input_preview": {"count": 3}, "parallelism": 2},
                                    },
                                    "output": [],
                                },
                                "message": None,
                            },
                            {
                                "record": {
                                    "step_index": 4,
                                    "kind": "run",
                                    "status": "finished",
                                    "payload": {
                                        "target_kind": "thunk",
                                        "child_run_ids": ["run_inline"],
                                        "stage_index": 1,
                                        "stage_total": 2,
                                        "stage_kind": "do",
                                        "metadata": {"source_line": 34, "stage_title": "Inline summary"},
                                    },
                                    "output": [],
                                },
                                "message": None,
                            },
                        ],
                    ),
                    _inspect_run_detail(
                        "run_child",
                        thread_id="term_thread",
                        root_run_id="run_parent",
                        parent_run_id="run_parent",
                        parent_step_index=2,
                        executable_kind="thunk",
                        executable_name="score",
                        call_kind="stage",
                        steps=[
                            {
                                "record": {
                                    "step_index": 1,
                                    "kind": "tool",
                                    "status": "finished",
                                    "payload": {},
                                    "input": [],
                                    "output": [
                                        {
                                            "type": "tool_result",
                                            "tool_name": "filesystem__read_text",
                                            "input": {"path": "tasks/qf7y0d8k.md"},
                                            "output": "tool output",
                                        }
                                    ],
                                },
                                "message": None,
                            }
                        ],
                    ),
                    _inspect_run_detail(
                        "run_inline",
                        thread_id="term_thread",
                        root_run_id="run_parent",
                        parent_run_id="run_parent",
                        parent_step_index=4,
                        executable_kind="thunk",
                        executable_name=None,
                        call_kind="stage",
                        metadata={"child": {"source_line": 34}},
                    ),
                ],
            }
        raise AssertionError(request_path)

    monkeypatch.setattr(inspect_cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["inspect", "dev", "run_parent"])

    assert result.exit_code == 0
    assert calls == ["/api/v1/runs/run_parent", "/api/v1/threads/term_thread?limit=100"]
    assert "# run" in result.stdout
    assert "run_parent  succeeded  flow:research  thread=term_thread" in result.stdout
    assert "run run_parent" not in result.stdout
    assert "# input\nquery" in result.stdout
    assert "# steps" in result.stdout
    assert "\n✓ 1   step    stage 1 rank prepare count=3" in result.stdout
    assert "\n✓ 2   run" in result.stdout
    assert "item 1/3 thunk:score run_child" in result.stdout
    assert "\n✓ 3   step    stage 1 rank done count=2" in result.stdout
    assert "\n✓ 4   run     stage 2 do -> thunk:<L34> run_inline" in result.stdout
    assert "2.1 tool" not in result.stdout

    tree_result = _invoke_app(["inspect", "dev", "run_parent", "--tree", "--depth", "2"])

    assert tree_result.exit_code == 0
    assert "\n  ✓ 2.1 tool    filesystem__read_text: path=tasks/qf7y0d8k.md" in tree_result.stdout

    parent_path_json = _invoke_app(["inspect", "dev", "run_parent:2", "--json"])

    assert parent_path_json.exit_code == 0
    parent_path_data = json.loads(parent_path_json.stdout)
    assert parent_path_data["step"]["variant"] == "compound"
    assert parent_path_data["step"]["children"][0]["path"] == "2.1"
    assert parent_path_data["step"]["children"][0]["summary"] == "filesystem__read_text: path=tasks/qf7y0d8k.md"
    assert "record" not in parent_path_data["step"]["children"][0]

    child_path_json = _invoke_app(["inspect", "dev", "run_parent:2.1", "--json"])

    assert child_path_json.exit_code == 0
    child_path_data = json.loads(child_path_json.stdout)
    assert child_path_data["step"]["variant"] == "tool"
    assert child_path_data["step"]["tool_calls"][0]["result"] == "tool output"

    parent_path_result = _invoke_app(["inspect", "dev", "run_parent:2"])

    assert parent_path_result.exit_code == 0
    assert "step 2 run  succeeded" in parent_path_result.stdout
    assert "children" in parent_path_result.stdout
    assert "\n  ✓ 2.1 tool    filesystem__read_text: path=tasks/qf7y0d8k.md" in parent_path_result.stdout
    assert "input_refs" not in parent_path_result.stdout

    path_result = _invoke_app(["inspect", "dev", "run_parent:2.1"])

    assert path_result.exit_code == 0
    assert "step 2.1 tool  succeeded" in path_result.stdout
    assert "run  run_child" in path_result.stdout
    assert "output" in path_result.stdout
    assert "tool output" in path_result.stdout


def test_cli_inspect_child_thunk_run_focuses_failure_details(monkeypatch) -> None:
    calls: list[str] = []

    child = _inspect_run_detail(
        "run_child",
        thread_id="term_thread",
        root_run_id="run_parent",
        parent_run_id="run_parent",
        parent_step_index=2,
        executable_kind="thunk",
        executable_name="expand_queries",
        call_kind="stage",
        steps=[
            {
                "record": {
                    "step_index": 1,
                    "kind": "model",
                    "status": "finished",
                    "payload": {"model_ref": "deepseek/deepseek-chat-v3"},
                    "output": [
                        {"type": "text", "text": "I should inspect services.\n"},
                        {
                            "type": "tool_call",
                            "tool_name": "service_use__service_list",
                            "tool_family": "service_use__service_list",
                            "input": {"visibility": "all"},
                        },
                    ],
                },
                "message": {
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "text": "I should inspect services.\n"},
                        {
                            "type": "tool_call",
                            "tool_name": "service_use__service_list",
                            "tool_family": "service_use__service_list",
                            "input": {"visibility": "all"},
                        },
                    ],
                },
            },
            {
                "record": {
                    "step_index": 2,
                    "kind": "system",
                    "status": "failed",
                    "payload": {},
                    "output": [{"type": "text", "text": "unknown tool call: service_use__service_list"}],
                    "error": "unknown tool call: service_use__service_list",
                },
                "message": None,
            },
        ],
    )
    child["output"] = {
        **cast(dict[str, object], child["output"]),
        "status": "failed",
        "error": "unknown tool call: service_use__service_list",
        "failure": {
            "reason": "unknown tool call: service_use__service_list",
            "step_index": 2,
            "step_kind": "system",
        },
    }

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(request_path)
        if request_path == "/api/v1/runs/run_child":
            return child
        if request_path == "/api/v1/threads/term_thread?limit=100":
            return {
                "info": {"id": "term_thread"},
                "runs": [
                    _inspect_run_detail("run_parent", thread_id="term_thread", root_run_id="run_parent"),
                    child,
                ],
            }
        raise AssertionError(request_path)

    monkeypatch.setattr(inspect_cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["inspect", "dev", "run_child"])

    assert result.exit_code == 0
    assert calls == ["/api/v1/runs/run_child", "/api/v1/threads/term_thread?limit=100"]
    assert "# run" in result.stdout
    assert "run_child  failed  thunk:expand_queries  thread=term_thread" in result.stdout
    assert "run run_child" not in result.stdout
    assert "# output" in result.stdout
    assert "error: unknown tool call: service_use__service_list (step 2 system)" in result.stdout
    assert "unknown tool call: service_use__service_list (step 2 system)" in result.stdout
    assert "\n✓ 1   model   deepseek/deepseek-chat-v3 I should inspect services." in result.stdout
    assert "requested service_use__service_list: visibility=all" in result.stdout
    assert "\n✗ 2   system  unknown tool call: service_use__service_list" in result.stdout
    assert "- run_parent flow:research" not in result.stdout


def test_cli_inspect_thunk_run_uses_chat_style_step_output(monkeypatch) -> None:
    calls: list[str] = []
    run = _inspect_run_detail(
        "run_thunk",
        thread_id="term_thread",
        executable_kind="thunk",
        executable_name="summarize",
        steps=[
            {
                "record": {
                    "step_index": 1,
                    "kind": "model",
                    "status": "finished",
                    "payload": {"model_ref": "deepseek/deepseek-chat-v3"},
                    "output": [
                        {"type": "text", "text": "Ready to read the task."},
                        {
                            "type": "tool_call",
                            "tool_name": "filesystem__read_text",
                            "input": {"path": "task.md"},
                        },
                    ],
                },
                "message": None,
            },
            {
                "record": {
                    "step_index": 2,
                    "kind": "tool",
                    "status": "finished",
                    "payload": {},
                    "input": [],
                    "output": [
                        {
                            "type": "tool_result",
                            "tool_name": "filesystem__read_text",
                            "input": {"path": "task.md"},
                            "output": "task body",
                        }
                    ],
                },
                "message": None,
            },
            {
                "record": {
                    "step_index": 3,
                    "kind": "model",
                    "status": "finished",
                    "payload": {"model_ref": "deepseek/deepseek-chat-v3"},
                    "output": [{"type": "text", "text": "Summary complete."}],
                },
                "message": None,
            },
        ],
    )

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(request_path)
        if request_path == "/api/v1/runs/run_thunk":
            return run
        if request_path == "/api/v1/threads/term_thread?limit=100":
            return {"info": {"id": "term_thread"}, "runs": [run]}
        raise AssertionError(request_path)

    monkeypatch.setattr(inspect_cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["inspect", "dev", "run_thunk"])

    assert result.exit_code == 0
    assert calls == ["/api/v1/runs/run_thunk", "/api/v1/threads/term_thread?limit=100"]
    assert "# run" in result.stdout
    assert "run_thunk  succeeded  thunk:summarize  thread=term_thread" in result.stdout
    assert "run run_thunk" not in result.stdout
    assert "# input\nquery" in result.stdout
    assert "# output\nSummary complete." in result.stdout
    assert "# steps" in result.stdout
    assert "\n✓ 1   model   deepseek/deepseek-chat-v3 Ready to read the task." in result.stdout
    assert "requested filesystem__read_text: path=task.md" in result.stdout
    assert "\n✓ 2   tool    filesystem__read_text: path=task.md" in result.stdout
    assert "\n✓ 3   model   deepseek/deepseek-chat-v3 Summary complete." in result.stdout

    focus_result = _invoke_app(["inspect", "dev", "run_thunk:1"])

    assert focus_result.exit_code == 0
    assert "step 1 model  succeeded" in focus_result.stdout
    assert "adapter_request" in focus_result.stdout
    assert "output" in focus_result.stdout
    assert '"tool_name": "filesystem__read_text"' in focus_result.stdout
    assert '"path": "task.md"' in focus_result.stdout

    tool_focus_result = _invoke_app(["inspect", "dev", "run_thunk:2"])

    assert tool_focus_result.exit_code == 0
    assert "step 2 tool  succeeded" in tool_focus_result.stdout
    assert "tool_call 1" in tool_focus_result.stdout
    assert "input" in tool_focus_result.stdout
    assert '"path": "task.md"' in tool_focus_result.stdout
    assert "result" in tool_focus_result.stdout
    assert "task body" in tool_focus_result.stdout


def test_cli_inspect_structured_views_render_preprocessed_document(monkeypatch) -> None:
    calls: list[str] = []
    run = _inspect_run_detail(
        "run_struct",
        thread_id="term_thread",
        executable_kind="thunk",
        executable_name="summarize",
        steps=[
            {
                "record": {
                    "step_index": 1,
                    "kind": "model",
                    "status": "finished",
                    "input": [{"kind": "command", "index": 0}],
                    "payload": {
                        "model_ref": "openai/gpt-5",
                        "instruct": "prompt_instruct",
                        "context": "prompt_context",
                    },
                    "output": [
                        {"type": "text", "text": "Ready."},
                        {
                            "type": "tool_call",
                            "tool_call_id": "call_read",
                            "tool_name": "filesystem__read_text",
                            "input": {"path": "task.md"},
                        },
                    ],
                },
                "message": None,
            },
            {
                "record": {
                    "step_index": 2,
                    "kind": "tool",
                    "status": "finished",
                    "payload": {},
                    "input": [{"kind": "step", "index": 1}],
                    "output": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call_read",
                            "tool_name": "filesystem__read_text",
                            "output": "task body",
                        }
                    ],
                },
                "message": None,
            },
        ],
    )
    run["prompts"] = {
        "prompt_instruct": "Answer concisely.",
        "prompt_context": "Use project context.",
    }

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(request_path)
        if request_path == "/api/v1/runs/run_struct":
            return run
        if request_path == "/api/v1/threads/term_thread?limit=100":
            return {"info": {"id": "term_thread"}, "runs": [run]}
        raise AssertionError(request_path)

    monkeypatch.setattr(inspect_cli, "_runtime_json", fake_runtime_json)

    json_result = _invoke_app(["inspect", "dev", "run_struct:1", "--json"])

    assert json_result.exit_code == 0
    json_data = json.loads(json_result.stdout)
    assert json_data["kind"] == "step"
    assert "steps" not in json_data["run"]
    assert json_data["step"]["variant"] == "model"
    assert json_data["step"]["record"]["payload"]["model_ref"] == "openai/gpt-5"
    assert json_data["step"]["adapter_request"] == {
        "instructions": "Answer concisely.",
        "context": "Use project context.",
        "messages": [{"role": "user", "parts": [{"type": "text", "text": "query"}]}],
        "tools": None,
        "state": None,
    }

    toml_result = _invoke_app(["inspect", "dev", "run_struct", "--toml"])

    assert toml_result.exit_code == 0
    toml_data = tomllib.loads(toml_result.stdout)
    assert toml_data["kind"] == "run"
    assert "steps" not in toml_data["run"]
    assert toml_data["steps"][0]["kind"] == "model"
    assert "record" not in toml_data["steps"][0]
    assert "adapter_request" not in toml_data["steps"][0]
    assert toml_data["steps"][1]["kind"] == "tool"
    assert toml_data["steps"][1]["summary"] == "filesystem__read_text: path=task.md"
    assert "tool_calls" not in toml_data["steps"][1]

    tool_json_result = _invoke_app(["inspect", "dev", "run_struct:2", "--json"])

    assert tool_json_result.exit_code == 0
    tool_json_data = json.loads(tool_json_result.stdout)
    assert tool_json_data["step"]["variant"] == "tool"
    assert tool_json_data["step"]["tool_calls"][0]["name"] == "filesystem__read_text"
    assert tool_json_data["step"]["tool_calls"][0]["input"] == {"path": "task.md"}
    assert tool_json_data["step"]["tool_calls"][0]["result"] == "task body"
    assert calls == [
        "/api/v1/runs/run_struct",
        "/api/v1/threads/term_thread?limit=100",
        "/api/v1/runs/run_struct",
        "/api/v1/threads/term_thread?limit=100",
        "/api/v1/runs/run_struct",
        "/api/v1/threads/term_thread?limit=100",
    ]


def test_cli_inspect_rejects_multiple_structured_views() -> None:
    result = _invoke_app(["inspect", "dev", "run_struct", "--json", "--toml"])

    assert result.exit_code == 1
    assert "--human, --json, and --toml are mutually exclusive" in result.stderr


def test_cli_inspect_thread_lists_top_level_runs_only(monkeypatch) -> None:
    calls: list[str] = []
    parent = _inspect_run_detail(
        "run_parent",
        thread_id="term_thread",
        root_run_id="run_parent",
        executable_kind="flow",
        executable_name="research",
    )
    parent["output"] = {
        **cast(dict[str, object], parent["output"]),
        "status": "failed",
        "error": "unknown tool call: service_use__service_list",
        "failure": {
            "reason": "unknown tool call: service_use__service_list",
            "step_index": 3,
            "step_kind": "system",
        },
    }
    child = _inspect_run_detail(
        "run_child",
        thread_id="term_thread",
        root_run_id="run_parent",
        parent_run_id="run_parent",
        parent_step_index=2,
        executable_kind="thunk",
        executable_name="expand_queries",
        call_kind="stage",
    )

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(request_path)
        if request_path == "/api/v1/threads/term_thread?limit=100":
            return {
                "info": {
                    "id": "term_thread",
                    "title": "agent framework implementations",
                    "status": "idle",
                    "origin": "chat",
                    "run_count": 2,
                    "latest_run": {"id": "run_child", "status": "failed"},
                },
                "runs": [parent, child],
            }
        raise AssertionError(request_path)

    monkeypatch.setattr(inspect_cli, "_runtime_json", fake_runtime_json)

    result = _invoke_app(["inspect", "dev", "term_thread"])

    assert result.exit_code == 0
    assert calls == ["/api/v1/threads/term_thread?limit=100"]
    assert "# thread" in result.stdout
    assert "term_thread  idle  runs=2" in result.stdout
    assert "# title\nagent framework implementations" in result.stdout
    assert "# runs" in result.stdout
    assert "\n✗  run_parent  flow:research" in result.stdout
    assert "\n  error: unknown tool call: service_use__service_list (step 3 system)" in result.stdout
    assert "run_child thunk:expand_queries" not in result.stdout
    assert "step 1" not in result.stdout


def test_script_progress_defaults_to_stage_summary() -> None:
    sink = cli_invoke._ScriptProgressSink(thunk_name="research", render=False)

    sink.on_event(
        RunStart(
            run_id="run_parent",
            origin="script",
            thread_id="script_1",
            input=Message.user("query"),
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
            executable_kind="flow",
            executable_name="research",
        )
    )
    sink.on_event(
        RunStart(
            run_id="run_child",
            origin="script",
            thread_id="script_1",
            input=Message.user("query"),
            created_at="2026-01-01T00:00:01Z",
            started_at="2026-01-01T00:00:01Z",
            parent_run_id="run_parent",
            parent_step_index=2,
            executable_kind="thunk",
            executable_name="search_web",
            call_kind="stage",
            metadata={
                "child": {
                    "stage_label": "each: Search the web",
                    "stage_index": 1,
                    "stage_kind": "each",
                    "parallelism": 2,
                    "lane_index": 0,
                    "item_index": 0,
                    "item_count": 3,
                }
            },
        )
    )
    sink.on_event(
        StepStart(
            run_id="run_child",
            thread_id="script_1",
            step_index=1,
            kind="model",
            input=(),
            started_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        StepEnd(
            run_id="run_parent",
            thread_id="script_1",
            step_index=1,
            kind="step",
            status="finished",
            output=(),
            payload=FlowOpStepPayload(
                op="prepare_each",
                stage_index=1,
                stage_kind="each",
                output_preview={"count": 3},
                metadata={"stage_label": "each: Search the web", "stage_index": 1, "stage_kind": "each"},
            ),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        StepEnd(
            run_id="run_parent",
            thread_id="script_1",
            step_index=2,
            kind="run",
            status="finished",
            output=(TextPart(text="done"),),
            payload=ChildCallStepPayload(
                call="stage",
                target_kind="thunk",
                target="search_web",
                child_run_ids=("run_child",),
                parallelism=2,
                lane_index=0,
                stage_index=1,
                stage_kind="each",
                item_indexes=(0,),
                metadata={
                    "stage_label": "each: Search the web",
                    "stage_index": 1,
                    "stage_kind": "each",
                    "parallelism": 2,
                    "item_count": 3,
                },
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    sink.on_event(
        RunEnd(
            run_id="run_unknown_child",
            thread_id="script_1",
            status="finished",
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    sink.on_event(
        StepEnd(
            run_id="run_parent",
            thread_id="script_1",
            step_index=3,
            kind="step",
            status="finished",
            output=(),
            payload=FlowOpStepPayload(
                op="set_current",
                stage_index=1,
                stage_kind="each",
                output_preview={"count": 3},
                metadata={
                    "stage_label": "each: Search the web",
                    "stage_index": 1,
                    "stage_kind": "each",
                    "input_preview": {"count": 3},
                    "parallelism": 2,
                },
            ),
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        )
    )
    sink.on_event(
        RunEnd(
            run_id="run_parent",
            thread_id="script_1",
            status="finished",
            finished_at="2026-01-01T00:00:03Z",
        )
    )

    assert sink._title == "Running flow:research: run_parent"
    lines = sink._render_lines()
    assert len(lines) == 3
    assert lines[1].startswith("[2] Search the web")
    assert "Search the web" in lines[1]
    assert "3 items -> 3 items" in lines[1]
    assert "2 lanes" in lines[1]
    assert lines[2] == "Done · 1 stages · 1 calls · 0 failed"
    assert "run_child" not in "\n".join(lines)


def test_script_progress_expands_lanes_with_verbosity() -> None:
    sink = cli_invoke._ScriptProgressSink(thunk_name="research", render=False, verbosity=2)
    sink.on_event(
        RunStart(
            run_id="run_parent",
            origin="script",
            thread_id="script_1",
            input=Message.user("query"),
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
            executable_kind="flow",
            executable_name="research",
        )
    )
    for run_id, item_index in (("run_second", 1), ("run_first", 0)):
        sink.on_event(
            RunStart(
                run_id=run_id,
                origin="script",
                thread_id="script_1",
                input=Message.user("query"),
                created_at="2026-01-01T00:00:01Z",
                started_at="2026-01-01T00:00:01Z",
                parent_run_id="run_parent",
                parent_step_index=item_index + 1,
                executable_kind="thunk",
                executable_name="search_web",
                call_kind="stage",
                metadata={
                    "child": {
                        "stage_label": "each: Search the web",
                        "stage_index": 0,
                        "stage_kind": "each",
                        "parallelism": 2,
                        "lane_index": item_index,
                        "item_index": item_index,
                        "item_count": 2,
                    }
                },
            )
        )

    lines = sink._render_lines()
    assert lines[0] == "Running flow:research: run_parent"
    assert any("lane 1/2" in line for line in lines)
    assert any("lane 2/2" in line for line in lines)
    assert "\n".join(lines).index("item 1/2") < "\n".join(lines).index("item 2/2")
    assert "batch" not in "\n".join(lines)


def test_script_progress_keeps_final_frame_visible(monkeypatch) -> None:
    live_kwargs: dict[str, object] = {}

    class FakeLive:
        def __init__(self, _text: object, **kwargs: object) -> None:
            live_kwargs.update(kwargs)

        def start(self, *, refresh: bool = False) -> None:
            del refresh

        def update(self, _text: object, *, refresh: bool = False) -> None:
            del refresh

        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli_invoke, "Live", FakeLive)
    sink = cli_invoke._ScriptProgressSink(thunk_name="research", render=True)

    sink.on_event(
        RunStart(
            run_id="run_parent",
            origin="script",
            thread_id="script_1",
            input=Message.user("query"),
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:00Z",
            executable_kind="flow",
            executable_name="research",
        )
    )

    assert live_kwargs["transient"] is False


def test_cli_inspect_no_longer_accepts_steps_view() -> None:
    result = _invoke_app(["inspect", "dev", "run_parent", "--view", "steps"])

    assert result.exit_code == 2
    assert "No such option: --view" in result.stderr


def test_cli_inspect_no_longer_accepts_events_view() -> None:
    result = _invoke_app(["inspect", "dev", "run_parent", "--view", "events", "--limit", "25"])

    assert result.exit_code == 2
    assert "No such option: --view" in result.stderr


def test_cli_inspect_rejects_invalid_step_paths() -> None:
    thread_result = _invoke_app(["inspect", "dev", "term_thread:1"])
    malformed_result = _invoke_app(["inspect", "dev", "run_parent:1.bad"])

    assert thread_result.exit_code == 1
    assert "step paths are only supported for run targets" in thread_result.stderr
    assert malformed_result.exit_code == 1
    assert "invalid step path: 1.bad" in malformed_result.stderr


def test_cli_thread_control_help_lists_agent_with_arguments() -> None:
    steer = _invoke_app(["steer", "dev", "--help"])
    cancel = _invoke_app(["cancel", "dev", "--help"])

    assert steer.exit_code == 0
    assert "Usage: root AGENT steer [OPTIONS] RUN MESSAGE" in steer.stdout
    assert "Scope" not in steer.stdout
    positions = [
        steer.stdout.index("Agent name."),
        steer.stdout.index("Run id to steer."),
        steer.stdout.index("Thread id means its active run."),
        steer.stdout.index("Instruction to steer the run."),
    ]
    assert positions == sorted(positions)
    assert cancel.exit_code == 0
    assert "Usage: root AGENT cancel [OPTIONS] RUN" in cancel.stdout
    assert "Run id to cancel." in cancel.stdout
    assert "Thread id means its active run." in cancel.stdout


def test_cli_rewind_and_fork_help_describe_latest_run_target() -> None:
    rewind = _invoke_app(["rewind", "dev", "--help"])
    fork = _invoke_app(["fork", "dev", "--help"])

    assert rewind.exit_code == 0
    assert "Usage: root AGENT rewind [OPTIONS] POINT" in rewind.stdout
    assert "Rewind a thread to an earlier point." in rewind.stdout
    assert "Run id to rewind before." in rewind.stdout
    assert "Thread id means rewind before" in rewind.stdout
    assert "its latest run." in rewind.stdout
    assert "Message to send after rewinding." not in rewind.stdout
    assert "Open chat on the rewound thread." in rewind.stdout
    assert fork.exit_code == 0
    assert "Usage: root AGENT fork [OPTIONS] POINT" in fork.stdout
    assert "Fork a thread from a branch point." in fork.stdout
    assert "Run id to fork before." in fork.stdout
    assert "Thread id means fork after its" in fork.stdout
    assert "latest run." in fork.stdout
    assert "[MESSAGE]" not in fork.stdout
    assert "Optional first message to send in the forked" not in fork.stdout
    assert "Open chat on the forked thread." in fork.stdout


def test_cli_chat_term_without_message_exits_without_creating_thread(monkeypatch) -> None:
    posts: list[tuple[str, object]] = []

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        posts.append((request_path, payload))
        return {"thread_id": "term_new"}

    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    result = _invoke_app(["chat", "dev"], input="/exit\n")

    assert result.exit_code == 0
    assert posts == []
    assert "thread term_new" not in result.stdout


@pytest.mark.parametrize("command", ("steer", "cancel", "rewind", "fork"))
def test_cli_thread_control_commands_show_help_without_target(command: str, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["too"])

    with pytest.raises(SystemExit) as exc:
        cli.main(["alice", command])

    assert exc.value.code == 0


def test_cli_rewind_accepts_thread_target(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(("json", request_path))
        return {"info": {"latest_run": {"id": "run_latest"}}}

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append(("post", (request_path, payload)))
        return {"thread_id": "term_thread", "run_id": None}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    result = _invoke_app(["rewind", "dev", "term_thread"])

    assert result.exit_code == 0
    assert calls == [
        ("json", "/api/v1/threads/term_thread"),
        (
            "post",
            (
                "/api/v1/runs/run_latest/rewind",
                {},
            ),
        ),
    ]


def test_cli_rewind_without_message_does_not_send_empty_message(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(("json", request_path))
        return {"info": {"latest_run": {"id": "run_latest"}}}

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append(("post", (request_path, payload)))
        return {"thread_id": "term_thread", "run_id": None}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    result = _invoke_app(["rewind", "dev", "term_thread"])

    assert result.exit_code == 0
    assert calls == [
        ("json", "/api/v1/threads/term_thread"),
        ("post", ("/api/v1/runs/run_latest/rewind", {})),
    ]


def test_cli_rewind_chat_opens_rewound_thread(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(("json", request_path))
        return {"info": {"latest_run": {"id": "run_latest"}}}

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append(("post", (request_path, payload)))
        return {"thread_id": "term_thread", "run_id": None}

    def fake_chat_interactive(
        _ctx: Any,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        del selector_payload
        calls.append(("chat", thread_id))

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_interactive", fake_chat_interactive)

    result = _invoke_app(["rewind", "dev", "term_thread", "--chat"])

    assert result.exit_code == 0
    assert calls == [
        ("json", "/api/v1/threads/term_thread"),
        ("post", ("/api/v1/runs/run_latest/rewind", {})),
        ("chat", "term_thread"),
    ]


def test_cli_fork_thread_target_copies_through_latest_run(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(("json", request_path))
        return {"info": {"latest_run": {"id": "run_latest"}}}

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append(("post", (request_path, payload)))
        return {"thread_id": "term_fork", "run_id": None}

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)

    result = _invoke_app(["fork", "dev", "term_thread"])

    assert result.exit_code == 0
    assert calls == [
        ("json", "/api/v1/threads/term_thread"),
        (
            "post",
            (
                "/api/v1/runs/run_latest/fork",
                {"include_anchor": True},
            ),
        ),
    ]
    assert "forked term_fork through run_latest" in result.stdout


def test_cli_fork_chat_opens_forked_thread(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_runtime_json(_ctx: Any, request_path: str) -> dict[str, object]:
        calls.append(("json", request_path))
        return {"info": {"latest_run": {"id": "run_latest"}}}

    def fake_runtime_post(_ctx: Any, request_path: str, *, payload: dict[str, object]) -> dict[str, object]:
        calls.append(("post", (request_path, payload)))
        return {"thread_id": "term_fork", "run_id": None}

    def fake_chat_interactive(
        _ctx: Any,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
    ) -> None:
        del selector_payload
        calls.append(("chat", thread_id))

    monkeypatch.setattr(cli, "_runtime_json", fake_runtime_json)
    monkeypatch.setattr(cli, "_runtime_post", fake_runtime_post)
    monkeypatch.setattr(cli, "_chat_interactive", fake_chat_interactive)

    result = _invoke_app(["fork", "dev", "term_thread", "--chat"])

    assert result.exit_code == 0
    assert calls == [
        ("json", "/api/v1/threads/term_thread"),
        ("post", ("/api/v1/runs/run_latest/fork", {"include_anchor": True})),
        ("chat", "term_fork"),
    ]


def test_standalone_caps_list_supports_agent_prefix(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    result = _invoke_caps_app(
        ["--root", str(toolang_root), "skill", "list"],
        prefix_agent="alice",
        env={},
    )

    assert result.exit_code == 0
    assert "SKILL" in result.stdout
    assert "SOURCE" in result.stdout
    assert "FORM" in result.stdout
    assert "SCOPE" in result.stdout
    assert "reviewer" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout


def test_standalone_caps_help_shows_agent_prefix_usage() -> None:
    result = runner.invoke(caps_cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Manage composable agent primitives." in result.stdout
    assert "caps [AGENT] [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "Scope" not in result.stdout
    assert "agent      TEXT  Apply to this agent's home caps instead of root caps." in result.stdout
    assert "--agent" not in result.stdout


def test_standalone_caps_list_help_mentions_agent_inclusion() -> None:
    result = runner.invoke(caps_cli.app, ["list", "--help"])

    assert result.exit_code == 0
    assert "Inspect available caps." in result.stdout
    assert "caps [AGENT] list [OPTIONS]" in result.stdout
    assert "--filter" in result.stdout
    assert "-f" in result.stdout
    assert "Filter caps with selector-list syntax." in result.stdout
    assert "--kind" not in result.stdout
    assert "--global" not in result.stdout
    assert "agent      TEXT  Also include this agent's home caps." in result.stdout


def test_standalone_cap_kind_list_help_omits_kind_filters() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "list", "--help"])

    assert result.exit_code == 0
    assert "List skills." in result.stdout
    assert "Filter caps with selector-list syntax." in result.stdout
    assert "psyche, skill" not in result.stdout
    assert "service, prompt" not in result.stdout


def test_standalone_cap_group_help_shows_agent_prefix_usage() -> None:
    result = runner.invoke(caps_cli.app, ["psyche", "--help"])

    assert result.exit_code == 0
    assert "caps [AGENT] psyche [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "caps [AGENT] TEXT psyche" not in result.stdout
    assert "Scope" not in result.stdout
    assert "Manage psyche caps." in result.stdout
    assert "List psyches." in result.stdout
    assert "agent      TEXT  Apply to this agent's home psyches instead of root" in result.stdout
    assert "psyches." in result.stdout
    assert "--agent" not in result.stdout


def test_standalone_cap_template_help_uses_inspect_description() -> None:
    result = runner.invoke(caps_cli.app, ["psyche", "template", "--help"])

    assert result.exit_code == 0
    assert "caps [AGENT] psyche template [OPTIONS] [NAME]" in result.stdout
    assert "Inspect psyche templates." in result.stdout
    assert "name       TEXT  Template name." in result.stdout
    assert "Scope" not in result.stdout
    assert "agent      TEXT  Apply to this agent's home psyches instead of root" in result.stdout
    assert "psyches." in result.stdout


def test_standalone_caps_main_supports_agent_prefix(monkeypatch) -> None:
    captured: list[tuple[list[str], str | None]] = []

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        del prog_name, standalone_mode
        captured.append((args, caps_cli._CLI_PREFIX_AGENT))

    monkeypatch.setattr(caps_cli, "app", cast(object, fake_app))
    monkeypatch.setattr(caps_cli.sys, "argv", ["caps"])

    result = caps_cli.main(["alice", "skill", "list"])

    assert result == 0
    assert captured == [(["skill", "list"], "alice")]
    assert caps_cli._CLI_PREFIX_AGENT is None


def test_standalone_caps_main_rejects_removed_agent_option() -> None:
    result = runner.invoke(caps_cli.app, ["list", "--agent", "alice"])

    assert result.exit_code != 0


def test_standalone_caps_list_prepares_agent_once_with_progress(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )
    calls = 0
    original_build_prepared_state = caps_commands.watch_feature.build_prepared_state

    def counted_build_prepared_state(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build_prepared_state(*args, **kwargs)

    monkeypatch.setattr(caps_commands.watch_feature, "build_prepared_state", counted_build_prepared_state)

    result = _invoke_caps_app(
        ["--root", str(toolang_root), "list"],
        prefix_agent="alice",
        env={},
    )

    assert result.exit_code == 0
    assert calls == 1
    assert "reviewer" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout
    assert "Resolved 1 caps" in result.stderr


def test_standalone_cap_kind_list_prepares_agent_once_with_progress(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )
    calls = 0
    original_build_prepared_state = caps_commands.watch_feature.build_prepared_state

    def counted_build_prepared_state(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build_prepared_state(*args, **kwargs)

    monkeypatch.setattr(caps_commands.watch_feature, "build_prepared_state", counted_build_prepared_state)

    result = _invoke_caps_app(
        ["--root", str(toolang_root), "skill", "list"],
        prefix_agent="alice",
        env={},
    )

    assert result.exit_code == 0
    assert calls == 1
    assert "reviewer" in result.stdout
    assert "agents/alice/skills/reviewer" in result.stdout
    assert "Resolved 1 caps" in result.stderr


def test_standalone_cap_kind_list_summarizes_updated_remote_caps(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    config_path = toolang_root / "agents" / "alice" / "config.toml"
    config_path.write_text(
        '[skills]\nreview = { ref = "github://acme/agents/skills/review@main" }\n',
        encoding="utf-8",
    )

    def fake_remote_materialized_files(*, relative_entry_path, kind, name, ref, progress=None):
        del name
        if progress is not None:
            progress(
                ProgressEvent(
                    id=f"cap.fetch:{kind}:{ref}",
                    phase="cap.fetch",
                    label=f"Fetch {kind}",
                    status="ok",
                    detail="1 file",
                )
            )
        return {str(relative_entry_path): b"---\ndescription: Review changes\n---\n# Review\n"}

    monkeypatch.setattr(caps, "_remote_materialized_files", fake_remote_materialized_files)

    result = _invoke_caps_app(
        ["--root", str(toolang_root), "skill", "list"],
        prefix_agent="alice",
        env={},
    )

    assert result.exit_code == 0
    assert "review" in result.stdout
    assert "https://github.com/acme/agents/tree/main/skills/review" in result.stdout
    assert "Resolved 1 caps" in result.stderr
    assert "Updated 1 caps" in result.stderr


def test_standalone_cap_kind_list_hides_cached_prepare_progress(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="reviewer",
        text="---\ndescription: Review changes\n---\n# Reviewer\n",
    )

    first_result = _invoke_caps_app(
        ["--root", str(toolang_root), "skill", "list"],
        prefix_agent="alice",
        env={},
    )
    second_result = _invoke_caps_app(
        ["--root", str(toolang_root), "skill", "list"],
        prefix_agent="alice",
        env={},
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert "reviewer" in second_result.stdout
    assert second_result.stderr == ""


def test_cli_cap_list_rejects_invalid_filter(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        caps_cli.app,
        ["--root", str(toolang_root), "psyche", "list", "--filter", "[kind:psyche]"],
        env={},
    )

    assert result.exit_code == 1
    assert "selector identity belongs in the pattern" in result.stderr


def test_cli_version_option_exits_before_other_parsing(monkeypatch) -> None:
    monkeypatch.setattr(cli, "package_version", lambda _name: "0.1.2")
    monkeypatch.setattr(cli, "_source_state_suffix", lambda: "")

    result = runner.invoke(cli.app, ["--version", "list"])
    short_result = runner.invoke(cli.app, ["-V", "list"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "toolang 0.1.2"
    assert short_result.exit_code == 0
    assert short_result.stdout.strip() == "toolang 0.1.2"


def test_cli_version_includes_source_revision_suffix(monkeypatch) -> None:
    monkeypatch.setattr(cli, "package_version", lambda _name: "0.1.2")
    monkeypatch.setattr(cli, "_source_state_suffix", lambda: "+abc1234*")

    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "toolang 0.1.2+abc1234*"


def test_cli_help_lists_cap_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Run and manage Toolang agents." in result.stdout
    assert "--root" in result.stdout
    assert "Use a custom Toolang root." in result.stdout
    assert "--version" in result.stdout
    assert "-V" in result.stdout
    assert "Show current version and exit." in result.stdout
    assert "--log" not in result.stdout
    assert "Create an agent." in result.stdout
    assert "Clone an agent." in result.stdout
    assert "Remove an agent." in result.stdout
    assert "Show agents and their status." in result.stdout
    assert "Show agent info." in result.stdout
    assert "Run an agent in the foreground." in result.stdout
    assert "Manage agent chores." in result.stdout
    assert "Manage agent tasks." in result.stdout
    assert "Inspect available models." in result.stdout
    assert "Inspect available tools." in result.stdout
    assert "Inspect available channels." in result.stdout
    assert "Inspect available sandboxes." in result.stdout
    assert "Agent Commands" in result.stdout
    assert "Work Commands" not in result.stdout
    assert "Thread Commands" in result.stdout
    assert "Runtime Commands" in result.stdout
    assert "Cap Commands" in result.stdout
    assert "Runtime Components" not in result.stdout
    assert "Agent Capabilities" not in result.stdout
    assert "Inspect available caps." in result.stdout
    assert "Manage psyche caps." in result.stdout
    assert "Manage skill caps." in result.stdout
    assert "Manage service caps." in result.stdout
    assert "Manage prompt caps." in result.stdout
    assert "Open a terminal chat session." in result.stdout
    assert "Steer an active run." in result.stdout
    assert "Cancel an active run." in result.stdout
    assert "Rewind a thread to an earlier point." in result.stdout
    assert "Fork a thread from a branch point." in result.stdout
    assert "Inspect a thread or run." in result.stdout
    assert "send" not in result.stdout
    assert "attach" not in result.stdout
    chore_index = result.stdout.index("chore")
    task_index = result.stdout.index("task")
    stop_index = result.stdout.index("stop")
    chat_index = result.stdout.index("chat")
    steer_index = result.stdout.index("steer")
    cancel_index = result.stdout.index("cancel")
    rewind_index = result.stdout.index("rewind")
    fork_index = result.stdout.index("fork")
    runs_index = result.stdout.index("runs")
    threads_index = result.stdout.index("threads")
    inspect_index = result.stdout.index("inspect")
    model_index = result.stdout.index("model")
    tool_index = result.stdout.index("tool")
    channel_index = result.stdout.index("channel")
    sandbox_index = result.stdout.index("sandbox")
    psyche_index = result.stdout.index("psyche")
    skill_index = result.stdout.index("skill")
    service_index = result.stdout.index("service")
    prompt_index = result.stdout.index("prompt")
    caps_index = result.stdout.rindex("caps")
    assert "plugin" not in result.stdout
    assert result.stdout.index("Agent Commands") < stop_index < chore_index < task_index < result.stdout.index("Thread Commands")
    assert (
        result.stdout.index("Thread Commands")
        < chat_index
        < cancel_index
        < steer_index
        < rewind_index
        < fork_index
        < inspect_index
        < runs_index
        < threads_index
        < result.stdout.index("Runtime Commands")
        < model_index
        < tool_index
        < channel_index
        < sandbox_index
    )
    assert result.stdout.index("Cap Commands") < psyche_index
    assert psyche_index < skill_index < service_index < prompt_index < caps_index


def _inspect_run_detail(
    run_id: str,
    *,
    thread_id: str,
    root_run_id: str | None = None,
    parent_run_id: str | None = None,
    parent_step_index: int | None = None,
    executable_kind: str = "flow",
    executable_name: str | None = "research",
    call_kind: str = "root",
    metadata: dict[str, object] | None = None,
    steps: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "info": {
            "id": run_id,
            "origin": "script",
            "thread_id": thread_id,
            "root_run_id": root_run_id or run_id,
            "parent_run_id": parent_run_id,
            "parent_step_index": parent_step_index,
            "executable_kind": executable_kind,
            "executable_name": executable_name,
            "call_kind": call_kind,
            "metadata": metadata or {},
            "superseded": None,
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "updated_at": "2026-01-01T00:00:01Z",
        },
        "input": {"role": "user", "parts": [{"type": "text", "text": "query"}]},
        "inputs": [],
        "output": {
            "status": "finished",
            "error": None,
            "steps": steps or [],
        },
    }


def _write_roaming_program(tmp_path: Path, body_text: str, *, name: str = "demo") -> Path:
    path = tmp_path / f"{name}.too"
    path.write_text(body_text + "\n", encoding="utf-8")
    return path
