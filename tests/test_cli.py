from __future__ import annotations

from collections.abc import Mapping
import hashlib
import io
from pathlib import Path
import os
import time
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4
import pytest
from typer.testing import CliRunner

from toolang import agents
from toolang import caps
from toolang.base.types.message import Message
from toolang.base.types.model import ModelInfo
from toolang.base.types.tool import ToolContext, ToolDefinition
import toolang.cli.main as cli
import toolang.cli.caps_main as caps_cli
import toolang.cli.caps as caps_commands
from toolang.cli.progress import CliProgress
from toolang.components.trigger import watch
from toolang.config.log import DEFAULT_AGENT_LOG_SPEC
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang.execution.events import RunStart
from toolang.common.progress import ProgressEvent
from toolang import work
from toolang.execution.db import ExecutionStore, execution_db_path
runner = CliRunner()
DEFAULT_AGENT_SOURCE = "# Customize this agent here.\n# Docs: https://toolang.ai/docs\n"


def _invoke_app(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    prefix_agent: str | None = None,
):
    previous = cli._CLI_PREFIX_AGENT
    cli._CLI_PREFIX_AGENT = prefix_agent
    try:
        return runner.invoke(cli.app, args, env=env)
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


def test_cli_roaming_program_help_lists_available_thunks(capsys, tmp_path: Path) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk:
  Reply directly.

thunk summarize(_, style?):
  Summarize the current workspace in a concise style.
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
    assert "SCRIPT THUNK [OPTIONS] [PARAMS] [INPUT]..." in captured.out
    assert "Invoke a thunk from a Toolang script." in captured.out
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
    assert "Thunks" in captured.out
    assert "main" in captured.out
    assert "summarize" in captured.out
    assert captured.out.index("Options") < captured.out.index("Thunks") < captured.out.index("Params") < captured.out.index("Input")


def test_cli_roaming_thunk_help_is_dynamic(capsys, tmp_path: Path) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk summarize(_, style?, audience?):
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
    assert "SCRIPT THUNK" in captured.out
    assert "Summarize the current workspace in a concise style." in captured.out
    assert "Script:" in captured.out
    assert program_path.name in captured.out
    assert "Thunk:  summarize" in captured.out
    assert "* THUNK" not in captured.out
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
thunk(_, tone?, retries?: number, dry_run?: boolean):
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
            "main",
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
    assert captured["thunk_name"] == "main"
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
thunk(_):
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
            "main",
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
thunk(_):
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
            "main",
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

    result = cli.main([str(program_path), "main", "--quiet", "hello"])
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

    result = cli.main([str(program_path), "main", "hello"])
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

    result = cli.main([str(program_path), "main", "hello"])
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

    result = cli.main([str(program_path), "main", "hello", "-q"])
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

    result = cli.main([str(program_path), "main", "hello"])
    output = capsys.readouterr()

    assert result == 130
    assert output.err.splitlines() == [
        "toolang interrupted",
        "Run: run_test",
        f"Log: {agents.agent_script_run_log_path(program_path.parent / '.toolang', 'demo', thunk_name='main', run_id='run_test')}",
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

    result = cli.main([str(program_path), "main", "hello"])
    output = capsys.readouterr()

    assert result == 1
    assert "toolang error: No available models." in output.err
    assert "toolang model providers" in output.err
    assert "Run: run_" not in output.err


def test_cli_roaming_invoke_requires_explicit_thunk_name(tmp_path: Path, capsys) -> None:
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
    assert "SCRIPT THUNK [OPTIONS] [PARAMS] [INPUT]..." in output.out
    assert "Thunks" in output.out


def test_cli_roaming_invoke_requires_part_for_message_input(tmp_path: Path, capsys) -> None:
    program_path = _write_roaming_program(
        tmp_path,
        """
thunk summarize(_):
  Summarize the current workspace in a concise style.
""".strip(),
    )

    result = cli.main([str(program_path), "summarize"])
    output = capsys.readouterr()

    assert result == 0
    assert "Usage:" in output.out
    assert "SCRIPT THUNK [OPTIONS] [INPUT]..." in output.out
    assert "Summarize the current workspace in a concise style." in output.out
    assert "Thunk:  summarize" in output.out
    assert output.err == ""


def test_cli_roaming_invoke_rejects_unknown_thunk_name(tmp_path: Path, capsys) -> None:
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
    assert "unknown thunk: summarize" in output.err


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
            "main",
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
thunk(_, tone?):
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

    result = cli.main([str(program_path), "main", "style=concise", "tone=direct"])
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
thunk(_):
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

    result = cli.main([str(program_path), "main", f"@{note}"])
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
    assert "▄▄▄▄▄▄▄▄▄" in result.stdout
    assert "alice" in result.stdout
    assert "-----" in result.stdout
    assert "Home" in result.stdout
    assert str(toolang_root / "agents" / "alice") in result.stdout
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
    assert "o3, gpt-5" in result.stdout
    assert "Status" in result.stdout
    assert "running (up a day)" in result.stdout
    assert "Sandbox" in result.stdout
    assert "none" in result.stdout
    assert "Components" in result.stdout
    assert "router.chat, runner.chat, trigger.pulse" in result.stdout
    assert "PID" in result.stdout
    assert str(os.getpid()) in result.stdout
    assert "Started" in result.stdout
    assert "2026-04-07T11:00:00Z" in result.stdout
    assert "Created" in result.stdout
    assert "ONLINE" not in result.stdout
    assert "ENDPOINT" not in result.stdout
    assert "API" in result.stdout
    assert "http://127.0.0.1:8765/docs" in result.stdout
    assert "WebUI" in result.stdout
    assert "https://too.run/8765" in result.stdout
    assert "Updated" not in result.stdout
    assert result.stdout.index("PID") < result.stdout.index("API")
    assert result.stdout.index("WebUI") < result.stdout.index("Started")
    assert result.stdout.index("Started") < result.stdout.index("Created")


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
    assert "▄▄▄▄▄▄▄▄▄" in result.stdout
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


def test_cli_info_prefers_runtime_models_for_active_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    (toolang_root / "agents" / "alice" / "config.toml").write_text(
        '[models]\n'
        'default = ["o3", "gpt-5"]\n',
        encoding="utf-8",
    )
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
    assert "claude, gpt-5" in result.stdout


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
        "toolang.cli.main",
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
        "toolang.cli.main",
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
    assert "STATE" in result.stdout
    assert "STAGE" in result.stdout
    assert "Review the current plan." in result.stdout
    assert "inactive" in result.stdout
    assert "running" in result.stdout


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


def test_cli_task_pause_and_resume_update_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["task", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    task_id = work.list_tasks(toolang_root, "alice")[0].document.task_id()

    pause_result = _invoke_app(
        ["task", "pause", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    paused = work.find_task(toolang_root, "alice", task_id)
    resume_result = _invoke_app(
        ["task", "resume", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    resumed = work.find_task(toolang_root, "alice", task_id)

    assert pause_result.exit_code == 0
    assert f"task {task_id} paused" in pause_result.stdout
    assert paused is not None
    assert paused.document.state == "inactive"
    assert resume_result.exit_code == 0
    assert f"task {task_id} resumed" in resume_result.stdout
    assert resumed is not None
    assert resumed.document.state == "active"


def test_cli_task_restore_moves_archived_task_back(tmp_path: Path, monkeypatch) -> None:
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
        ["task", "restore", task_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert f"task {task_id} restored" in result.stdout
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


def test_cli_chore_pause_and_resume_update_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    chore_id = work.list_chores(toolang_root, "alice")[0].document.chore_id()

    pause_result = _invoke_app(
        ["chore", "pause", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    paused = work.find_chore(toolang_root, "alice", chore_id)
    resume_result = _invoke_app(
        ["chore", "resume", chore_id],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    resumed = work.find_chore(toolang_root, "alice", chore_id)

    assert pause_result.exit_code == 0
    assert f"chore {chore_id} paused" in pause_result.stdout
    assert paused is not None
    assert paused.document.state == "inactive"
    assert resume_result.exit_code == 0
    assert f"chore {chore_id} resumed" in resume_result.stdout
    assert resumed is not None
    assert resumed.document.state == "active"


def test_cli_chore_restore_can_restore_as_inactive(tmp_path: Path, monkeypatch) -> None:
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
        ["chore", "restore", chore_id, "--inactive"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    chore = work.find_chore(toolang_root, "alice", chore_id)
    assert chore is not None
    assert chore.document.state == "inactive"
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


def test_cli_fmt_formats_too_file(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.too"
    source_path.write_text(
        "#!/usr/bin/env toolang\n"
        "\n"
        "struct Result:\n"
        "    title:string\n"
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
        "  title: string\n"
        "\n"
        "thunk review(input: Message) -> Json:\n"
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
        "thunk review(input: Message):\n"
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
        "thunk review(input: Message):\n"
        "  user: Review it.\n"
    )


def test_cli_fmt_formats_stdin_with_tab_size() -> None:
    result = runner.invoke(
        cli.app,
        ["fmt", "--tab-size", "4", "-"],
        input="thunk review( input:Message):\n  user:\n    Review it.\n",
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "thunk review(input: Message):\n"
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
    assert "Scope" in result.stdout
    assert "Apply to this agent's skills instead of root skills." in result.stdout


def test_cli_skill_template_help_shows_plain_text_metavar() -> None:
    result = runner.invoke(caps_cli.app, ["skill", "template", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "[AGENT] skill template [OPTIONS] [NAME]" in result.stdout
    assert "name      TEXT" in result.stdout
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
    assert "agent      TEXT  Also include this agent's skills." in result.stdout


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
            "psyche reviewer: ```md\n"
            "Prefer concrete findings.\n"
            "```\n"
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
    assert "Scope" in result.stdout
    assert "agent      TEXT  Apply to this agent's caps instead of root caps." in result.stdout
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
    assert "agent      TEXT  Also include this agent's caps." in result.stdout


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
    assert "Scope" in result.stdout
    assert "Manage psyche caps." in result.stdout
    assert "List psyches." in result.stdout
    assert "agent      TEXT  Apply to this agent's psyches instead of root psyches." in result.stdout
    assert "--agent" not in result.stdout


def test_standalone_cap_template_help_uses_inspect_description() -> None:
    result = runner.invoke(caps_cli.app, ["psyche", "template", "--help"])

    assert result.exit_code == 0
    assert "caps [AGENT] psyche template [OPTIONS] [NAME]" in result.stdout
    assert "Inspect psyche templates." in result.stdout
    assert "name      TEXT  Template name." in result.stdout
    assert "Scope" in result.stdout
    assert "agent      TEXT  Apply to this agent's psyches instead of root psyches." in result.stdout


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
    assert "Runtime Commands" in result.stdout
    assert "Caps Commands" in result.stdout
    assert "Runtime Components" not in result.stdout
    assert "Agent Capabilities" not in result.stdout
    assert "Work Commands" not in result.stdout
    assert "Inspect available caps." in result.stdout
    assert "Manage psyche caps." in result.stdout
    assert "Manage skill caps." in result.stdout
    assert "Manage service caps." in result.stdout
    assert "Manage prompt caps." in result.stdout
    chore_index = result.stdout.index("chore")
    task_index = result.stdout.index("task")
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
    assert result.stdout.index("Agent Commands") < chore_index
    assert chore_index < task_index
    assert (
        task_index
        < result.stdout.index("Runtime Commands")
        < model_index
        < tool_index
        < channel_index
        < sandbox_index
    )
    assert result.stdout.index("Caps Commands") < psyche_index
    assert psyche_index < skill_index < service_index < prompt_index < caps_index


def _write_roaming_program(tmp_path: Path, body_text: str, *, name: str = "demo") -> Path:
    path = tmp_path / f"{name}.too"
    path.write_text(body_text + "\n", encoding="utf-8")
    return path
