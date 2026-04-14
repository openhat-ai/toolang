from __future__ import annotations

from pathlib import Path
import os
from datetime import datetime, timezone
from typing import cast

from typer.testing import CliRunner

from toolang import agents
from toolang import caps
from toolang import cli
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


def test_cli_main_normalizes_agent_prefix_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args
        captured["prog_name"] = prog_name
        captured["standalone_mode"] = standalone_mode

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["alice", "stop"])

    assert result == 0
    assert captured["args"] == ["stop", "alice"]
    assert captured["prog_name"] == "too"
    assert captured["standalone_mode"] is True


def test_cli_main_normalizes_agent_postfix_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args, prog_name: str, standalone_mode: bool) -> None:
        captured["args"] = args

    monkeypatch.setattr(cli, "app", cast(object, fake_app))

    result = cli.main(["stop", "alice"])

    assert result == 0
    assert captured["args"] == ["stop", "alice"]


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
        'pdf-processing = { locator = "github://by3gus/agent-skills/skills/pdf-processing" }\n',
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
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        scope="global",
        kind="skill",
        name="hello",
        text="# Hello\n",
    )
    caps.put_local_entry_text(
        toolang_root,
        "alice",
        scope="agent",
        kind="service",
        name="github",
        text="---\ntransport: http\ntarget: https://example.com/mcp\n---\n",
    )
    work.put_task_text(
        toolang_root,
        "alice",
        "review",
        "---\nstatus: todo\nrequester: owner\n---\n\nReview this change.\n",
    )
    work.put_chore_text(
        toolang_root,
        "alice",
        "sync",
        "---\ntitle: Sync\nrrule: FREQ=HOURLY;INTERVAL=1\n---\n\nSync the service.\n",
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
    assert "1 service" in result.stdout
    assert "Jobs" in result.stdout
    assert "1 chore" in result.stdout
    assert "1 task" in result.stdout
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
        model: str | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["host"] = host
        captured["public_host"] = public_host
        captured["port"] = port
        captured["sandbox"] = sandbox
        captured["model"] = model
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["loop_names"] = loop_names
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
    assert captured["model"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["loop_names"] == ["chat", "inspect"]
    assert cast(dict[str, str], captured["environ"])["TOOLANG_ROOT"] == str(toolang_root)


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
        model: str | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["host"] = host
        captured["public_host"] = public_host
        captured["port"] = port
        captured["sandbox"] = sandbox
        captured["model"] = model
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["loop_names"] = loop_names
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
    assert captured["model"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False
    assert captured["loop_names"] == ["chat"]
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
        model: str | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        environ: dict[str, str],
    ) -> int:
        del toolang_root, agent_name, host, public_host, port, sandbox, model, dev, sandbox_child, environ
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


def test_cli_run_passes_model_selector_to_agent_up(tmp_path: Path, monkeypatch) -> None:
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
        model: str | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        environ: dict[str, str],
    ) -> int:
        del toolang_root, agent_name, host, public_host, port, sandbox, dev, sandbox_child, loop_names, environ
        captured["model"] = model
        return 0

    monkeypatch.setattr(cli.agent_up, "up", fake_up)

    result = runner.invoke(
        cli.app,
        ["run", "alice", "--model", "openai/gpt-5@openai"],
        env={"TOOLANG_ROOT": str(toolang_root)},
    )

    assert result.exit_code == 0
    assert captured["model"] == "openai/gpt-5@openai"


def test_cli_run_requires_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "run"],
        env={},
    )

    assert result.exit_code in {0, 2}
    assert "Usage:" in result.stdout
    assert "AGENT run [OPTIONS]" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name." in result.stdout


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
        model: str | None = None,
        dev: Path | None = None,
        sandbox_child: bool = False,
        loop_names: list[str] | None = None,
        environ: dict[str, str],
    ) -> int:
        captured["environ"] = environ
        captured["public_host"] = public_host
        captured["sandbox"] = sandbox
        captured["model"] = model
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
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
    assert captured["model"] is None
    assert captured["dev"] is None
    assert captured["sandbox_child"] is False


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
        "toolang.cli",
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
    assert captured["cwd"] == str(Path.cwd())
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert agents.agent_runtime_log_path(toolang_root, "alice").read_text(encoding="utf-8") == "launcher\n"


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


def test_cli_start_includes_model_selector_in_background_command(tmp_path: Path, monkeypatch) -> None:
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
        ["--root", str(toolang_root), "start", "alice", "--model", "gpt-5"],
        env={},
    )

    assert result.exit_code == 0
    command = cast(list[str], captured["command"])
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gpt-5"


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

    add_result = _invoke_app(
        ["skill", "add", "acme/reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert add_result.exit_code == 0
    assert add_result.stdout.strip() == str(toolang_root / "agents" / "alice" / "config.toml")

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(encoding="utf-8")
    assert "[skills]" in config_text
    assert 'reviewer = { locator = "github://acme/agent-skills/skills/reviewer" }' in config_text

    list_remote_result = _invoke_app(
        ["skill", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert list_remote_result.exit_code == 0
    assert "SKILL" in list_remote_result.stdout
    assert "REF" in list_remote_result.stdout
    assert "SCOPE" in list_remote_result.stdout
    assert "LOCATION" in list_remote_result.stdout
    assert "reviewer" in list_remote_result.stdout
    assert "acme/reviewer" in list_remote_result.stdout
    assert "agent" in list_remote_result.stdout
    assert "github://acme/agent-skills/skills/reviewer" in list_remote_result.stdout

    remove_result = _invoke_app(
        ["skill", "remove", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert remove_result.exit_code == 0
    assert (
        remove_result.stdout.strip()
        == "Removed skill reviewer from github://acme/agent-skills/skills/reviewer"
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
    assert "SCOPE" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "agent" in list_result.stdout
    assert str(toolang_root / "agents" / "alice" / "skills" / "reviewer") in list_result.stdout


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
    assert "SCOPE" in list_result.stdout
    assert "reviewer" in list_result.stdout
    assert "agent" in list_result.stdout
    assert str(toolang_root / "agents" / "alice" / "skills" / "reviewer") in list_result.stdout

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

    remove_result = _invoke_app(
        ["skill", "remove", "reviewer"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert remove_result.exit_code == 0
    assert remove_result.stdout.strip() == (
        f"Removed skill reviewer from {toolang_root / 'agents' / 'alice' / 'skills' / 'reviewer'}"
    )
    assert not (toolang_root / "agents" / "alice" / "skills" / "reviewer").exists()


def test_cli_cap_add_preserves_unrelated_config_sections(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
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
        'pdf-processing = { locator = "github://by3gus/agent-skills/skills/pdf-processing" }'
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
    assert "command: uvx" in cast(str, captured["text"])


def test_cli_task_new_supports_template_alias_and_persists_id(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    result = _invoke_app(
        ["task", "new", "review", "-t", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    saved = (toolang_root / "agents" / "alice" / "tasks" / "review.md").read_text(encoding="utf-8")
    assert "status: todo" in saved
    assert "requester: owner" in saved
    assert "\nid: " in saved


def test_cli_task_list_shows_task_rows(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(
        cli.click,
        "edit",
        lambda *_args, **_kwargs: (
            "---\n"
            "requester: bryan\n"
            "status: doing\n"
            "paused: true\n"
            "---\n"
            "Review the current plan.\n"
        ),
    )
    _invoke_app(
        ["task", "new", "review"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["task", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "TASK" in result.stdout
    assert "STATUS" in result.stdout
    assert "REQUESTER" in result.stdout
    assert "review" in result.stdout
    assert "doing" in result.stdout
    assert "bryan" in result.stdout
    assert "yes" in result.stdout


def test_cli_chore_new_and_list_show_rrule(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)
    _invoke_app(
        ["chore", "new", "sync", "-t", "default"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    result = _invoke_app(
        ["chore", "list"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )

    assert result.exit_code == 0
    assert "CHORE" in result.stdout
    assert "RRULE" in result.stdout
    assert "sync" in result.stdout
    assert "FREQ=HOURLY;INTERVAL=1" in result.stdout


def test_cli_task_new_records_task_changed_update(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(cli.click, "edit", lambda text, **_kwargs: text)

    result = _invoke_app(
        ["task", "new", "review", "-t", "default"],
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
    assert updates[0].payload["name"] == "review"


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


def test_cli_work_template_commands_show_templates() -> None:
    task_result = runner.invoke(cli.app, ["task", "template"])
    chore_list_result = runner.invoke(cli.app, ["chore", "templates"])

    assert task_result.exit_code == 0
    assert chore_list_result.exit_code == 0
    assert "status: todo" in task_result.stdout
    assert "TEMPLATE" in chore_list_result.stdout
    assert "default" in chore_list_result.stdout


def test_cli_task_template_help_shows_plain_text_metavar() -> None:
    result = runner.invoke(cli.app, ["task", "template", "--help"])

    assert result.exit_code == 0
    assert "template      TEXT" in result.stdout
    assert "Template name. [default: default]" in result.stdout


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


def test_cli_cap_commands_cover_file_backed_kinds(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    cases = (
        ("psyche", "reviewer", toolang_root / "psyches" / "reviewer.md"),
        ("prompt", "rewrite", toolang_root / "prompts" / "rewrite.md"),
        ("service", "search", toolang_root / "services" / "search.md"),
    )

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
        assert "SCOPE" in list_result.stdout
        assert "LOCATION" in list_result.stdout
        assert name in list_result.stdout
        assert "global" in list_result.stdout
        assert str(path) in list_result.stdout

        remove_result = runner.invoke(
            cli.app,
            [kind, "remove", name],
            env={"TOOLANG_ROOT": str(toolang_root)},
        )
        assert remove_result.exit_code == 0
        assert remove_result.stdout.strip() == f"Removed {kind} {name} from {path}"
        assert not path.exists()


def test_cli_cap_template_outputs_template() -> None:
    skill_result = runner.invoke(cli.app, ["skill", "template"])
    prompt_result = runner.invoke(cli.app, ["prompt", "template"])
    service_result = runner.invoke(cli.app, ["service", "template"])
    psyche_result = runner.invoke(cli.app, ["psyche", "template"])

    assert skill_result.exit_code == 0
    assert prompt_result.exit_code == 0
    assert service_result.exit_code == 0
    assert psyche_result.exit_code == 0
    assert skill_result.stdout.strip().startswith(
        "---\ndescription: What this skill is for.\n---\n\n# Skill\n"
    )
    assert prompt_result.stdout.strip().startswith("Write the reusable prompt text here.\n")
    assert "transport: http" in service_result.stdout
    assert "Prefer:" in psyche_result.stdout


def test_cli_cap_template_list_shows_named_templates() -> None:
    result = runner.invoke(cli.app, ["service", "templates"])

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
    assert "list" in result.stdout


def test_cli_run_help_mentions_how_to_select_agent() -> None:
    result = runner.invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "AGENT run [OPTIONS]" in result.stdout
    assert "agent      TEXT" in result.stdout
    assert "Agent name." in result.stdout


def test_cli_info_help_mentions_required_agent() -> None:
    result = runner.invoke(cli.app, ["info", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "info [OPTIONS] AGENT" in result.stdout
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
    assert "Apply to this agent instead of global scope." in result.stdout


def test_cli_skill_template_help_shows_plain_text_metavar() -> None:
    result = runner.invoke(cli.app, ["skill", "template", "--help"])

    assert result.exit_code == 0
    assert "template      TEXT" in result.stdout
    assert "Template name. [default: default]" in result.stdout


def test_cli_skill_remove_help_mentions_agent_scope() -> None:
    result = runner.invoke(cli.app, ["skill", "remove", "--help"])

    assert result.exit_code == 0
    assert "Remove a skill." in result.stdout
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


def test_cli_cap_list_with_agent_defaults_to_global_and_agent_scopes(tmp_path: Path, monkeypatch) -> None:
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
    assert "global" in result.stdout
    assert "agent" in result.stdout
    assert str(toolang_root / "psyches" / "abc.md") in result.stdout
    assert str(toolang_root / "agents" / "alice" / "psyches" / "def.md") in result.stdout


def test_cli_cap_list_filter_filters_results(tmp_path: Path, monkeypatch) -> None:
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

    global_result = _invoke_app(
        ["psyche", "list", "--filter", "global"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert global_result.exit_code == 0
    assert "abc" in global_result.stdout
    assert "def" not in global_result.stdout
    assert "global" in global_result.stdout

    agent_result = _invoke_app(
        ["psyche", "list", "--filter", "agent"],
        env={"TOOLANG_ROOT": str(toolang_root)},
        prefix_agent="alice",
    )
    assert agent_result.exit_code == 0
    assert "abc" not in agent_result.stdout
    assert "def" in agent_result.stdout
    assert "agent" in agent_result.stdout


def test_cli_cap_list_rejects_agent_scope_without_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    result = runner.invoke(
        cli.app,
        ["--root", str(toolang_root), "psyche", "list", "--filter", "agent"],
        env={},
    )

    assert result.exit_code == 1
    assert "an agent prefix is required when --filter is agent" in result.stderr


def test_cli_help_orders_cap_groups() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Run and manage Toolang agents." in result.stdout
    assert "--root  -r" in result.stdout or "--root        -r" in result.stdout
    assert "Root directory for all agents." in result.stdout
    assert "Create an agent." in result.stdout
    assert "Clone an agent." in result.stdout
    assert "Remove an agent." in result.stdout
    assert "Show agents and their status." in result.stdout
    assert "Show agent info." in result.stdout
    assert "Run an agent in the foreground." in result.stdout
    psyche_index = result.stdout.index("psyche")
    skill_index = result.stdout.index("skill")
    service_index = result.stdout.index("service")
    prompt_index = result.stdout.index("prompt")
    chore_index = result.stdout.index("chore")
    task_index = result.stdout.index("task")
    assert psyche_index < skill_index < service_index < prompt_index
    assert chore_index < task_index
