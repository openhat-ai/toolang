from __future__ import annotations

from pathlib import Path
import os
from datetime import datetime, timezone
from typing import cast

from typer.testing import CliRunner

from toolang import agents
from toolang import caps
from toolang.base.types.model import ModelInfo
import toolang.cli.main as cli
from toolang.config.log import DEFAULT_AGENT_LOG_SPEC
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang import work
from toolang.execution.db import ExecutionStore, execution_db_path
runner = CliRunner()


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


def test_cli_main_configures_logging_for_roaming_invoke(monkeypatch, tmp_path: Path) -> None:
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

    result = cli.main(["--log", "toolang.run=debug", str(program_path), "--help"])

    assert result == 0
    assert captured["spec"] == "toolang.run=debug"
    assert captured["global_args"] == ["--log", "toolang.run=debug"]
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


def test_cli_main_keeps_postfix_cap_command_without_agent_prefix(monkeypatch) -> None:
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
    program_path = toolang_root / "agents" / "alice" / "alice.too"
    assert result.stdout.strip() == str(program_path)
    assert program_path.read_text(encoding="utf-8") == "agent alice\n"


def test_cli_new_uses_named_template(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["new", "alice", "--template", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    assert (toolang_root / "agents" / "alice" / "alice.too").read_text(encoding="utf-8") == "agent alice\n"


def test_cli_callback_configures_logging_for_standard_commands(monkeypatch, tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    calls: list[tuple[str | None, dict[str, str]]] = []

    def fake_configure_logging(*, spec: str | None, environ) -> None:
        calls.append((spec, dict(environ)))

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "--log", "toolang.run=debug", "list"],
        env={},
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "toolang.run=debug"


def test_cli_new_supports_template_alias(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["new", "alice", "-t", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    assert (toolang_root / "agents" / "alice" / "alice.too").read_text(encoding="utf-8") == "agent alice\n"


def test_cli_clone_copies_agent_without_prepared(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    source_home = toolang_root / "agents" / "alice"
    (source_home / "skills" / "reviewer").mkdir(parents=True, exist_ok=True)
    (source_home / ".prepared").mkdir(parents=True, exist_ok=True)
    (source_home / "alice.too").write_text("agent alice\n", encoding="utf-8")
    (source_home / "skills" / "reviewer" / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
    (source_home / ".prepared" / "lock.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["clone", "alice", "bob"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    target_program = toolang_root / "agents" / "bob" / "bob.too"
    assert result.stdout.strip() == str(target_program)
    assert target_program.read_text(encoding="utf-8") == "agent bob\n"
    assert (toolang_root / "agents" / "bob" / "skills" / "reviewer" / "SKILL.md").is_file()
    assert not (toolang_root / "agents" / "bob" / ".prepared").exists()


def test_agent_selector_parsing_supports_name_shorthand_and_ref() -> None:
    local = agents.parse_agent_selector("alice")
    github_short = agents.parse_agent_selector("brice/alice")
    host_short = agents.parse_agent_selector("toolang.ai/alice")
    github_ref = agents.parse_agent_selector("github://brice/agents/team/alice.too@main")

    assert local.form == "name"
    assert local.name == "alice"
    assert github_short.form == "shorthand"
    assert github_short.resolved_ref().render() == "github://brice/agents/alice.too"
    assert host_short.form == "shorthand"
    assert host_short.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.form == "ref"
    assert github_ref.resolved_ref().render() == "github://brice/agents/team/alice.too@main"


def test_cli_clone_remote_shorthand_defaults_target_name(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "github://brice/agents/alice.too"
        return "agent source-name\n"

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)

    result = runner.invoke(
        cli.app,
        ["clone", "brice/alice"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code in {0, 2}
    program_path = toolang_root / "agents" / "alice" / "alice.too"
    assert result.stdout.strip() == str(program_path)
    assert program_path.read_text(encoding="utf-8") == "agent alice\n"


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
    program_path = toolang_root / "agents" / "researcher" / "researcher.too"
    assert result.stdout.strip() == str(program_path)
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


def test_cli_run_supports_remote_selector(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "github://brice/agents/alice.too"
        return "agent remote-source\n"

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str,
        public_host: str | None,
        port: int | None,
        sandbox: str | None,
        models: list[str] | None,
        dev: Path | None,
        sandbox_child: bool,
        loop_names: tuple[str, ...] | None,
        log_spec: str | None,
        environ: dict[str, str],
    ) -> int:
        del host, public_host, port, sandbox, models, dev, sandbox_child, loop_names, log_spec, environ
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        program_path = toolang_root / "agents" / agent_name / f"{agent_name}.too"
        captured["program_exists"] = program_path.is_file()
        captured["program_text"] = program_path.read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)
    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "brice/alice"],
        env={},
    )

    assert result.exit_code == 0
    assert captured["agent_name"] == "alice"
    assert captured["program_exists"] is True
    assert captured["program_text"] == "agent alice\n"
    assert Path(cast(Path, captured["toolang_root"])).name.startswith("toolang-run-")


def test_cli_run_supports_remote_url_selector(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_fetch(ref: agents.AgentRef) -> str:
        assert ref.render() == "https://toolang.ai/demo/researcher.too"
        return "agent researcher\n"

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str,
        public_host: str | None,
        port: int | None,
        sandbox: str | None,
        models: list[str] | None,
        dev: Path | None,
        sandbox_child: bool,
        loop_names: tuple[str, ...] | None,
        log_spec: str | None,
        environ: dict[str, str],
    ) -> int:
        del host, public_host, port, sandbox, models, dev, sandbox_child, loop_names, log_spec, environ
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        return 0

    monkeypatch.setattr(agents, "fetch_agent_ref", fake_fetch)
    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "https://toolang.ai/demo/researcher.too"],
        env={},
    )

    assert result.exit_code == 0
    assert captured["agent_name"] == "researcher"
    assert Path(cast(Path, captured["toolang_root"])).name.startswith("toolang-run-")


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
    assert "THUNK [OPTIONS] [PARAMS] [PARTS]" in captured.out
    assert "Thunks" in captured.out
    assert "main" in captured.out
    assert "summarize" in captured.out


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
    assert "summarize" in captured.out
    assert "[OPTIONS]" in captured.out
    assert "[PARAMS]" in captured.out
    assert "style=TEXT" in captured.out
    assert "audience=TEXT" in captured.out
    assert "PARTS" in captured.out


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
    ):
        del environ
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["thunk_name"] = thunk_name
        captured["input_text"] = input_text
        captured["models"] = models
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            status = "finished"
            output_text = "done"
            error = None

        return _Outcome()

    monkeypatch.setattr(cli.cli_invoke.agent_up, "invoke", fake_invoke)

    result = cli.main(
        [
            str(program_path),
            "main",
            "rewrite this",
            f"@{attachment}",
            "tone=concise",
            "retries=3",
            "dry_run=true",
            "--model",
            "gpt-5",
            "--model",
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
    assert "THUNK [OPTIONS] [PARAMS] [PARTS]" in output.out
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

    assert result == 1
    assert "requires at least one PART" in output.err


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
    ):
        del toolang_root, agent_name, thunk_name, models, environ
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            status = "finished"
            output_text = "done"
            error = None

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
    ):
        del toolang_root, agent_name, thunk_name, models, environ
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            status = "finished"
            output_text = "done"
            error = None

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
    ):
        del toolang_root, agent_name, thunk_name, models, environ
        captured["input_text"] = input_text
        captured["metadata"] = dict(metadata or {})

        class _Outcome:
            status = "finished"
            output_text = "done"
            error = None

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


def test_cli_remove_deletes_stopped_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "remove", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "alice\tremoved"
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
    assert "agent is still active: alice" in result.stderr


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
    assert "agent is still active: alice" in result.stderr
    assert (toolang_root / "agents" / "alice").is_dir()


def test_cli_stop_stops_orphan_runtime_process_without_state(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    stopped: list[tuple[int, bool]] = []
    monkeypatch.setattr(agents, "agent_runtime_process_pids", lambda *_args: (12345,))
    monkeypatch.setattr(
        agents,
        "_stop_pid",
        lambda pid, *, force: stopped.append((pid, force)),
    )

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "stop", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "alice\tstopped"
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
    assert "API" in result.stdout
    assert "WEBUI" in result.stdout
    assert "alice" in result.stdout
    assert "running" in result.stdout
    assert "none" in result.stdout
    assert "http://127.0.0.1:8765/docs" in result.stdout
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
        'pdf-processing = { ref = "github://by3gus/agent-skills/skills/pdf-processing" }\n',
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
        loops=("chat", "pulse"),
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
    assert "Loops" in result.stdout
    assert "chat, pulse" in result.stdout
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
    assert "http://localhost:3000/8765" in result.stdout
    assert "Updated" not in result.stdout
    assert result.stdout.index("PID") < result.stdout.index("API")
    assert result.stdout.index("WebUI") < result.stdout.index("Started")
    assert result.stdout.index("Started") < result.stdout.index("Created")


def test_cli_info_for_stopped_agent_shows_created_only(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-07T11:00:00Z",
        pid=os.getpid(),
        loops=("chat", "pulse"),
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
    assert "Loops" not in result.stdout
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
        loops=("chat", "pulse"),
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
        loops=("chat",),
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


def test_cli_plugin_list_shows_installed_plugins(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda: {
            "openai": _FakeModelProvider(
                name="openai",
                description="Use OpenAI-hosted models.",
                required_env=("OPENAI_API_KEY",),
                base_url="https://api.openai.com/v1",
                api_key_env="OPENAI_API_KEY",
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
                    ),
                ),
            ),
            "ollama": _FakeModelProvider(
                name="ollama",
                description="Use local Ollama-hosted models.",
                base_url="http://127.0.0.1:11434/v1",
                models=(),
            ),
        },
    )

    def fake_list_plugin_infos(*, group: str) -> list[cli.agent_up.PluginInfo]:
        infos = {
            "toolang.model": [
                cli.agent_up.PluginInfo(name="openai", source="built-in"),
                cli.agent_up.PluginInfo(name="ollama", source="built-in"),
            ],
            "toolang.tool": [
                cli.agent_up.PluginInfo(name="filesystem", source="built-in"),
                cli.agent_up.PluginInfo(name="shell", source="external"),
            ],
            "toolang.channel": [cli.agent_up.PluginInfo(name="telegram", source="external")],
            "toolang.sandbox": [
                cli.agent_up.PluginInfo(name="docker", source="external"),
                cli.agent_up.PluginInfo(name="none", source="built-in"),
            ],
        }
        return list(infos[group])

    monkeypatch.setattr(cli.agent_up, "list_plugin_infos", fake_list_plugin_infos)

    result = runner.invoke(
        cli.app,
        ["plugin", "list"],
        env={"OPENAI_API_KEY": "secret"},
    )

    assert result.exit_code == 0
    assert "FAMILY" in result.stdout
    assert "NAME" in result.stdout
    assert "SOURCE" in result.stdout
    assert "CONFIG" in result.stdout
    assert "model" in result.stdout
    assert "openai" in result.stdout
    assert "ollama" in result.stdout
    assert "built-in" in result.stdout
    assert "external" in result.stdout
    assert "configured" in result.stdout
    assert "available" in result.stdout
    assert "base URL https://api.openai.com/v1" in result.stdout
    assert "env OPENAI_API_KEY" in result.stdout
    assert "1 discovered model" in result.stdout
    assert "tool" in result.stdout
    assert "filesystem" in result.stdout
    assert "shell" in result.stdout
    assert "channel" in result.stdout
    assert "telegram" in result.stdout
    assert "sandbox" in result.stdout
    assert "docker" in result.stdout
    assert "none" in result.stdout


def test_cli_model_list_shows_discovered_models(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.agent_up,
        "load_model_providers",
        lambda: {
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
    assert "PROVIDER" in result.stdout
    assert "MODEL" in result.stdout
    assert "ADAPTER" in result.stdout
    assert "FEATURES" in result.stdout
    assert "STATUS" not in result.stdout
    assert "SELECTOR" not in result.stdout
    assert "openai" in result.stdout
    assert "openrouter" in result.stdout
    assert "tools=yes" in result.stdout
    assert "streaming=yes" in result.stdout
    assert "responses" in result.stdout


def test_cli_run_delegates_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["host"] = host
        captured["public_host"] = public_host
        captured["port"] = port
        captured["sandbox"] = sandbox
        captured["models"] = models
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["loop_names"] = loop_names
        captured["log_spec"] = log_spec
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "alice",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--loop",
            "chat",
            "--loop",
            "inspect",
        ],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["toolang_root"] == toolang_root
    assert captured["agent_name"] == "alice"
    assert captured["host"] == "0.0.0.0"
    assert captured["public_host"] is None
    assert captured["port"] == 9000
    assert captured["sandbox"] is None
    assert captured["models"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["loop_names"] == ["chat", "inspect"]
    assert captured["log_spec"] == DEFAULT_AGENT_LOG_SPEC
    assert cast(dict[str, str], captured["environ"])["TOOLANG_ROOT"] == str(toolang_root)
    assert cast(dict[str, str], captured["environ"])[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC


def test_cli_run_omits_port_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["host"] = host
        captured["public_host"] = public_host
        captured["port"] = port
        captured["sandbox"] = sandbox
        captured["models"] = models
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["loop_names"] = loop_names
        captured["log_spec"] = log_spec
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--loop", "chat"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["toolang_root"] == toolang_root
    assert captured["agent_name"] == "alice"
    assert captured["host"] == "127.0.0.1"
    assert captured["public_host"] is None
    assert captured["port"] is None
    assert captured["sandbox"] is None
    assert captured["models"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["loop_names"] == ["chat"]
    assert captured["log_spec"] == DEFAULT_AGENT_LOG_SPEC
    assert cast(dict[str, str], captured["environ"])["TOOLANG_ROOT"] == str(toolang_root)


def test_cli_run_supports_csv_loop_option(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        del toolang_root, agent_name, host, public_host, port, sandbox, models, dev, sandbox_child, log_spec, environ
        captured["loop_names"] = loop_names
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--loop", "chat,inspect,poll"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["loop_names"] == ["chat", "inspect", "poll"]


def test_cli_run_passes_model_selectors_to_agent_up(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        del toolang_root, agent_name, host, public_host, port, sandbox, dev, sandbox_child, loop_names, log_spec, environ
        captured["models"] = models
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--model", "openai/gpt-5@openai", "--model", "o3"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["models"] == ["openai/gpt-5@openai", "o3"]


def test_cli_run_does_not_override_explicit_log_spec(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        del toolang_root, agent_name, host, public_host, port, sandbox, models, dev, sandbox_child, loop_names
        captured["environ"] = environ
        captured["log_spec"] = log_spec
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "--log", "toolang.run=debug", "run", "alice"],
        env={},
    )

    assert result.exit_code == 0
    assert captured["log_spec"] == "toolang.run=debug"
    assert PY_LOG_ENV_VAR not in cast(dict[str, str], captured["environ"])


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
    assert "Agent selector." in result.stdout
    assert "remote URLs" in result.stdout


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

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str = "127.0.0.1",
        public_host: str | None = None,
        port: int | None = None,
        sandbox: str | None = None,
        models: list[str] | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        log_spec: str | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["environ"] = environ
        captured["public_host"] = public_host
        captured["sandbox"] = sandbox
        captured["models"] = models
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["log_spec"] = log_spec
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run", "alice", "--loop", "inspect"],
        env={},
    )

    assert result.exit_code == 0
    environ = cast(dict[str, str], captured["environ"])
    assert environ["TELEGRAM_BOT_TOKEN"] == "agent-token"
    assert environ["ROOT_ONLY"] == "1"
    assert environ["AGENT_ONLY"] == "1"
    assert captured["public_host"] is None
    assert captured["sandbox"] is None
    assert captured["models"] is None
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
            endpoint="http://127.0.0.1:8765",
            started_at="2026-04-07T11:00:01Z",
            pid=os.getpid(),
        )
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "start", "alice", "--sandbox", "none", "--loop", "inspect"],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "alice\trunning\thttp://127.0.0.1:8765/docs\thttp://localhost:3000/8765"
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
        "--public-host",
        "127.0.0.1",
        "--port",
        "8765",
        "--sandbox",
        "none",
        "--loop",
        "inspect",
    ]
    assert cast(dict[str, str], captured["env"])["TOOLANG_ROOT"] == str(toolang_root)
    assert cast(dict[str, str], captured["env"])[PY_LOG_ENV_VAR] == DEFAULT_AGENT_LOG_SPEC
    assert captured["cwd"] == str(Path.cwd())
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert agents.agent_runtime_log_path(toolang_root, "alice").read_text(encoding="utf-8") == "launcher\n"


def test_cli_start_propagates_explicit_log_spec_to_agent_process(tmp_path: Path, monkeypatch) -> None:
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
            "--log",
            "toolang.run=debug,httpx=off",
            "start",
            "alice",
            "--sandbox",
            "none",
        ],
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert command[0:7] == [
        cli.sys.executable,
        "-m",
        "toolang.cli.main",
        "--log",
        "toolang.run=debug,httpx=off",
        "--root",
        str(toolang_root),
    ]
    assert PY_LOG_ENV_VAR not in cast(dict[str, str], captured["env"])


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
        env={},
    )

    assert result.exit_code == 1
    assert "agent is already active: alice" in result.stderr


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
        env={},
    )

    assert result.exit_code == 0
    assert "agent is already active: alice" not in result.stderr


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
        ["--root", str(toolang_root), "start", "alice", "--loop", "chat,inspect"],
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--port" in command
    assert command[command.index("--port") + 1] == "8765"
    assert command[-4:] == ["--loop", "chat", "--loop", "inspect"]


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
            "--model",
            "gpt-5",
            "--model",
            "o3",
        ],
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    first_flag = command.index("--model")
    assert command[first_flag + 1] == "gpt-5"
    second_flag = command.index("--model", first_flag + 1)
    assert command[second_flag + 1] == "o3"


def test_cli_start_preserves_host_public_host_and_sandbox_in_background_command(
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
            "--public-host",
            "agent.example.com",
            "--port",
            "8765",
            "--sandbox",
            "docker:python:3.13-slim",
        ],
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--host" in command
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert "--public-host" in command
    assert command[command.index("--public-host") + 1] == "agent.example.com"
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
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--port" in command
    assert command[command.index("--port") + 1] == "63295"
    assert "--loop" in command
    loop_names = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--loop"
    ]
    assert loop_names == ["chat", "pulse", "control", "inspect", "prepare", "reload"]


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
        env={},
    )

    assert result.exit_code == 1
    assert "agent failed during startup: alice" in result.stderr


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
    assert "Agent name." in result.stdout


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
    assert result.stdout.strip() == "alice\tstopped"
    assert captured["runtime_id"] == "sandbox-alice"
    assert captured["force"] is False


def test_cli_cap_remote_add_list_remove_round_trip(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        caps,
        "_remote_materialized_files",
        lambda *, relative_entry_path, kind, name, ref: {
            str(relative_entry_path): b"---\ndescription: Review code\n---\n# Reviewer\n"
        },
    )

    add_result = _invoke_app(
        ["skill", "add", "acme/reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == str(toolang_root / "agents" / "alice" / "config.toml")

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(encoding="utf-8")
    assert "[skills]" in config_text
    assert 'reviewer = { ref = "github://acme/agent-skills/skills/reviewer" }' in config_text
    assert (
        toolang_root / "agents" / "alice" / ".prepared" / "remote" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8") == "---\ndescription: Review code\n---\n# Reviewer\n"

    list_remote_result = _invoke_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_remote_result.exit_code == 0
    assert "SKILL" in list_remote_result.stdout
    assert "REF" in list_remote_result.stdout
    assert "VISIBILITY" in list_remote_result.stdout
    assert "ORIGIN" in list_remote_result.stdout
    assert "INCLUSION" in list_remote_result.stdout
    assert "DESCRIPTION" not in list_remote_result.stdout
    assert "reviewer" in list_remote_result.stdout
    assert "private" in list_remote_result.stdout
    assert "remote" in list_remote_result.stdout
    assert "configured" in list_remote_result.stdout
    assert "github://acme/agent-skills/skills/reviewer" in list_remote_result.stdout

    remove_result = _invoke_app(
        ["skill", "remove", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert remove_result.exit_code == 0
    assert (
        remove_result.stdout.strip()
        == "Removed remote skill reviewer from github://acme/agent-skills/skills/reviewer"
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

    add_result = _invoke_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == str(
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    )

    list_result = _invoke_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "SKILL" in list_result.stdout
    assert "REF" in list_result.stdout
    assert "VISIBILITY" in list_result.stdout
    assert "ORIGIN" in list_result.stdout
    assert "INCLUSION" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "private" in list_result.stdout
    assert "local" in list_result.stdout
    assert "home://skills/reviewer" in list_result.stdout


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
    new_result = _invoke_app(
        ["skill", "new", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert new_result.exit_code == 0
    assert new_result.stdout.strip() == str(
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    )
    assert (
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8").startswith(
        "---\ndescription: Review code\n---\n# Reviewer\n"
    )

    list_result = _invoke_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_result.exit_code == 0
    assert "SKILL" in list_result.stdout
    assert "REF" in list_result.stdout
    assert "VISIBILITY" in list_result.stdout
    assert "ORIGIN" in list_result.stdout
    assert "INCLUSION" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "private" in list_result.stdout
    assert "local" in list_result.stdout
    assert "home://skills/reviewer" in list_result.stdout

    edited_text = (
        "---\n"
        "description: Review code deeply\n"
        "---\n"
        "# Reviewer\n\n"
        "Review code even more carefully.\n"
    )
    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: edited_text)
    edit_result = _invoke_app(
        ["skill", "edit", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert edit_result.exit_code == 0
    assert edit_result.stdout.strip() == str(
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    )
    assert (
        toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"
    ).read_text(encoding="utf-8") == edited_text

    delete_result = _invoke_app(
        ["skill", "delete", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert delete_result.exit_code == 0
    assert delete_result.stdout.strip() == (
        f"Deleted local skill reviewer from {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer'}"
    )
    assert not (toolang_root / "agents" / "alice" / "skills" / "reviewer").exists()


def test_cli_cap_add_preserves_unrelated_config_sections(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    config_path = toolang_root / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[web]\n'
        'cors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["skill", "add", "by3gus/pdf-processing"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    text = config_path.read_text(encoding="utf-8")
    assert "[web]" in text
    assert "cors_allowed_origins" in text
    assert "http://localhost:3000" in text
    assert "https://too.run" in text
    assert "[skills]" in text
    assert (
        'pdf-processing = { ref = "github://by3gus/agent-skills/skills/pdf-processing" }'
        in text
    )


def test_cli_cap_new_cancel_does_not_create(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    captured: dict[str, object] = {}

    def fake_edit(*_args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(cli.click, "edit", fake_edit)
    result = runner.invoke(
        cli.app,
        ["prompt", "new", "rewrite"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert captured["require_save"] is True
    assert captured["extension"] == ".md"
    assert not (toolang_root / "prompts" / "rewrite.md").exists()


def test_cli_cap_new_unchanged_template_does_not_create(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(cli.click, "edit", lambda *_args, **_kwargs: None)
    result = runner.invoke(
        cli.app,
        ["prompt", "new", "rewrite"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert not (toolang_root / "prompts" / "rewrite.md").exists()


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
        cli.app,
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
        cli.app,
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
    assert "Usage:" in chore_result.stdout
    assert "AGENT chore" in chore_result.stdout


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
            cli.app,
            [kind, "new", name],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert add_result.exit_code == 0
        assert add_result.stdout.strip() == str(path)

        list_result = runner.invoke(
            cli.app,
            [kind, "list"],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert list_result.exit_code == 0
        assert kind.upper() in list_result.stdout
        assert "REF" in list_result.stdout
        assert "VISIBILITY" in list_result.stdout
        assert "ORIGIN" in list_result.stdout
        assert name in list_result.stdout
        assert "shared" in list_result.stdout
        assert "local" in list_result.stdout
        assert f"root://{kind}s/{name}" in list_result.stdout

        delete_result = runner.invoke(
            cli.app,
            [kind, "delete", name],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert delete_result.exit_code == 0
        assert delete_result.stdout.strip() == f"Deleted local {kind} {name} from {path}"
        assert not path.exists()


def test_cli_cap_template_outputs_named_template() -> None:
    skill_result = runner.invoke(cli.app, ["skill", "template", "default"])
    prompt_result = runner.invoke(cli.app, ["prompt", "template", "default"])
    service_result = runner.invoke(cli.app, ["service", "template", "default"])
    psyche_result = runner.invoke(cli.app, ["psyche", "template", "default"])

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
    result = runner.invoke(cli.app, ["service", "template"])

    assert result.exit_code == 0
    assert "TEMPLATE" in result.stdout
    assert "default" in result.stdout
    assert "stdio" in result.stdout


def test_cli_skill_help_describes_remote_and_local_commands() -> None:
    result = runner.invoke(cli.app, ["skill", "--help"])

    assert result.exit_code == 0
    assert "Manage skills." in result.stdout
    assert "add" in result.stdout
    assert "remove" in result.stdout
    assert "new" in result.stdout
    assert "edit" in result.stdout
    assert "delete" in result.stdout
    assert "list" in result.stdout


def test_cli_run_help_mentions_how_to_select_agent() -> None:
    result = runner.invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "run [OPTIONS] AGENT" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent selector." in result.stdout
    assert "remote URLs" in result.stdout


def test_cli_info_help_mentions_required_agent() -> None:
    result = runner.invoke(cli.app, ["info", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "AGENT info [OPTIONS]" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name" in result.stdout


def test_cli_skill_add_help_mentions_agent_scope() -> None:
    result = runner.invoke(cli.app, ["skill", "add", "--help"])

    assert result.exit_code == 0
    assert "Add a remote skill." in result.stdout
    assert "[AGENT] skill add" in result.stdout


def test_cli_skill_new_help_mentions_agent_scope() -> None:
    result = runner.invoke(cli.app, ["skill", "new", "--help"])

    assert result.exit_code == 0
    assert "Create a local skill." in result.stdout
    assert "[AGENT] skill new" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Apply with private visibility for this agent." in result.stdout


def test_cli_skill_template_help_shows_plain_text_metavar() -> None:
    result = runner.invoke(cli.app, ["skill", "template", "--help"])

    assert result.exit_code == 0
    assert "template      TEXT" in result.stdout
    assert "Template name." in result.stdout


def test_cli_skill_remove_help_mentions_agent_scope() -> None:
    result = runner.invoke(cli.app, ["skill", "remove", "--help"])

    assert result.exit_code == 0
    assert "Remove a remote skill." in result.stdout
    assert "[AGENT] skill remove" in result.stdout


def test_cli_skill_edit_help_mentions_agent_scope() -> None:
    result = runner.invoke(cli.app, ["skill", "edit", "--help"])

    assert result.exit_code == 0
    assert "Edit a local skill." in result.stdout
    assert "[AGENT] skill edit" in result.stdout


def test_cli_skill_list_help_mentions_agent_scope_concisely() -> None:
    result = runner.invoke(cli.app, ["skill", "list", "--help"])

    assert result.exit_code == 0
    assert "List available skills." in result.stdout
    assert "[AGENT] skill list" in result.stdout


def test_cli_cap_list_with_agent_defaults_to_shared_and_private_visibility(tmp_path: Path, monkeypatch) -> None:
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
        cli.app,
        ["psyche", "new", "abc"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )
    _invoke_app(
        ["psyche", "new", "def"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["psyche", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "abc" in result.stdout
    assert "def" in result.stdout
    assert "shared" in result.stdout
    assert "private" in result.stdout
    assert "root://psyches/abc" in result.stdout
    assert "home://psyches/def" in result.stdout


def test_cli_cap_list_visibility_filters_results(tmp_path: Path, monkeypatch) -> None:
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
        cli.app,
        ["psyche", "new", "abc"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )
    _invoke_app(
        ["psyche", "new", "def"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    shared_result = _invoke_app(
        ["psyche", "list", "--visibility", "shared"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert shared_result.exit_code == 0
    assert "abc" in shared_result.stdout
    assert "def" not in shared_result.stdout
    assert "shared" in shared_result.stdout

    private_result = _invoke_app(
        ["psyche", "list", "--visibility", "private"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert private_result.exit_code == 0
    assert "abc" not in private_result.stdout
    assert "def" in private_result.stdout
    assert "private" in private_result.stdout


def test_cli_cap_list_rejects_private_visibility_without_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "psyche", "list", "--visibility", "private"],
        env={},
    )

    assert result.exit_code == 1
    assert "an agent prefix is required when --visibility is private" in result.stderr


def test_cli_help_orders_cap_groups() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Run and manage Toolang agents." in result.stdout
    assert "--root" in result.stdout
    assert "Root directory for all agents." in result.stdout
    assert "--log" in result.stdout
    assert "Set logging directives. Uses PY_LOG when omitted." in result.stdout
    assert "Create an agent." in result.stdout
    assert "Clone an agent." in result.stdout
    assert "Remove an agent." in result.stdout
    assert "Show agents and their status." in result.stdout
    assert "Show agent info." in result.stdout
    assert "Run an agent in the foreground." in result.stdout
    assert "Agent Commands" in result.stdout
    assert "Runtime Commands" in result.stdout
    assert "Runtime Components" not in result.stdout
    assert "Agent Capabilities" not in result.stdout
    assert "Work Commands" not in result.stdout
    psyche_index = result.stdout.index("psyche")
    skill_index = result.stdout.index("skill")
    service_index = result.stdout.index("service")
    prompt_index = result.stdout.index("prompt")
    chore_index = result.stdout.index("chore")
    task_index = result.stdout.index("task")
    plugin_index = result.stdout.index("plugin")
    model_index = result.stdout.index("model")
    assert result.stdout.index("Agent Commands") < psyche_index
    assert psyche_index < skill_index < service_index < prompt_index
    assert prompt_index < chore_index
    assert chore_index < task_index
    assert task_index < result.stdout.index("Runtime Commands") < plugin_index < model_index


def _write_roaming_program(tmp_path: Path, body_text: str, *, name: str = "demo") -> Path:
    path = tmp_path / f"{name}.too"
    path.write_text(body_text + "\n", encoding="utf-8")
    return path
