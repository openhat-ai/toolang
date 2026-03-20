from __future__ import annotations

import io
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from toolang.agent.refs import resolve_agent_ref
from toolang.agent.registry import (
    RunningAgentRecord,
    find_known_agents_by_name,
    get_running_agent,
    upsert_running_agent,
)
from toolang.cli import (
    _default_runtime_cap_scopes,
    _drop_stale_running_agent,
    _remember_agent,
    _resolve_cli_agent,
    app,
    main,
)
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.layout import (
    agent_run_path,
    agents_db_path,
    global_caps_dir,
    global_source_path,
    shared_caps_dir,
    shared_source_path,
    sandbox_args_path,
    sandbox_exec_path,
    sandbox_host,
)
from toolang_caps.models import CapKind

runner = CliRunner()
SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"
REMOTE_SKILL_FIXTURE = Path(__file__).parent / "fixtures" / "remote-skill" / "pdf-processing"
REMOTE_SERVICE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-service" / "github.md"
REMOTE_PROMPT_FIXTURE = Path(__file__).parent / "fixtures" / "remote-prompt" / "rewrite.md"
REMOTE_PSYCHE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-psyche" / "reviewer.md"


def test_cli_has_expected_subcommands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "new" in result.output
    assert "Create a new agent." in result.output
    assert "clone" in result.output
    assert "Clone an existing agent." in result.output
    assert "remove" in result.output
    assert "Remove an agent and its local state." in result.output
    assert "list" in result.output
    assert "List known agents and their current status." in result.output
    assert "sync" in result.output
    assert "Sync one agent state." in result.output
    assert "invoke" in result.output
    assert "Run one non-interactive agent turn." in result.output
    assert "serve" in result.output
    assert "Serve one agent in the foreground." in result.output
    assert "start" in result.output
    assert "Start serving one agent in the background." in result.output
    assert "psyche" in result.output
    assert "skill" in result.output
    assert "service" in result.output
    assert "prompt" in result.output
    assert "bus" in result.output
    command_lines = []
    for line in result.output.splitlines():
        if not line.startswith("│ "):
            continue
        content = line.strip("│ ").rstrip()
        if not content:
            continue
        name = content.split()[0]
        if name in {
            "list",
            "new",
            "clone",
            "sync",
            "invoke",
            "serve",
            "start",
            "remove",
            "skill",
            "service",
            "prompt",
            "psyche",
            "bus",
        }:
            command_lines.append(name)
    assert command_lines == [
        "new",
        "clone",
        "remove",
        "list",
        "sync",
        "invoke",
        "serve",
        "start",
        "psyche",
        "skill",
        "service",
        "prompt",
        "bus",
    ]
    assert "home" not in command_lines
    assert "source" not in command_lines
    assert "room" not in command_lines
    assert "init" not in command_lines
    assert "check" not in command_lines
    assert "dump-ast" not in command_lines


def test_cli_can_show_hidden_commands_in_help() -> None:
    result = runner.invoke(app, ["--hidden"])

    assert result.exit_code == 0
    assert "--hidden" in result.output
    assert "Helper Commands" in result.output
    assert "home" in result.output
    assert "Print the Toolang root or an agent home path." in result.output
    assert "source" in result.output
    assert "Print an agent source file path." in result.output
    assert "room" in result.output
    assert "Print an agent room path." in result.output
    assert "init" in result.output
    assert "Print shell helper setup." in result.output


def test_cli_shows_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"toolang {package_version('toolang')}"


def test_cli_commands_show_help_when_called_without_required_arguments() -> None:
    cases = [
        ["bus"],
        ["new"],
        ["clone"],
        ["remove"],
        ["invoke"],
        ["sync"],
        ["serve"],
        ["start"],
        ["skill"],
        ["skill", "add"],
        ["skill", "remove"],
        ["skill", "local"],
        ["skill", "local", "new"],
        ["skill", "local", "path"],
        ["skill", "local", "delete"],
        ["service"],
        ["service", "add"],
        ["service", "remove"],
        ["service", "local"],
        ["service", "local", "new"],
        ["service", "local", "path"],
        ["service", "local", "delete"],
        ["prompt"],
        ["prompt", "add"],
        ["prompt", "remove"],
        ["prompt", "local"],
        ["prompt", "local", "new"],
        ["prompt", "local", "path"],
        ["prompt", "local", "delete"],
        ["psyche"],
        ["psyche", "add"],
        ["psyche", "remove"],
        ["psyche", "local"],
        ["psyche", "local", "new"],
        ["psyche", "local", "path"],
        ["psyche", "local", "delete"],
        ["source"],
        ["room"],
        ["init"],
    ]

    for argv in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        assert "Usage:" in result.output, argv
        assert "Missing argument" not in result.output, argv
        assert "Try '" not in result.output, argv


def test_cli_sync_resolves_resident_agent_from_toolang_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "source_only.too"
    (home / "alice.too").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["sync", "alice"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "synced"


def test_cli_resolves_known_agent_by_name_from_registry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "reviewer.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("team/reviewer", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    resolved = _resolve_cli_agent("reviewer", db_path=db_path)

    assert resolved.agent_uri == agent.agent_uri


def test_cli_resolves_known_agent_by_id_prefix(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "worker.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice/worker", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    resolved = _resolve_cli_agent(agent.agent_id[:8], db_path=db_path)

    assert resolved.agent_uri == agent.agent_uri


def test_cli_runtime_cap_scope_defaults_follow_agent_kind(tmp_path: Path) -> None:
    root = tmp_path / "toolang-root"
    roaming_path = tmp_path / "project" / "bob.too"
    roaming_path.parent.mkdir(parents=True)
    roaming_path.write_text("thunk chat(user):\n    Hello.\n", encoding="utf-8")

    resident = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    roaming = resolve_agent_ref(str(roaming_path), cwd=tmp_path, toolang_root=root)
    visiting = resolve_agent_ref("https://example.com/alice.too", cwd=tmp_path, toolang_root=root)

    assert _default_runtime_cap_scopes(resident).labels() == ("agent", "shared", "global")
    assert _default_runtime_cap_scopes(roaming).labels() == ("agent", "shared")
    assert _default_runtime_cap_scopes(visiting).labels() == ("agent",)


def test_cli_rejects_ambiguous_known_agent_name(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    db_path = agents_db_path(root)
    for home_name in ("alice", "team"):
        home = root / "agents" / home_name
        home.mkdir(parents=True)
        (home / "reviewer.too").write_text(
            SOURCE_FIXTURE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        agent = resolve_agent_ref(f"{home_name}/reviewer", cwd=tmp_path, toolang_root=root)
        _remember_agent(agent, db_path=db_path)

    with pytest.raises(ToolangError, match="Ambiguous agent name"):
        _resolve_cli_agent("reviewer", db_path=db_path)


def test_cli_drops_stale_running_agent_and_updates_run_file(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)
    upsert_running_agent(
        db_path,
        RunningAgentRecord(
            agent_uri=agent.agent_uri,
            pid=999999,
            status="running",
            endpoint="http://127.0.0.1:8778",
            started_at=datetime(2026, 3, 19, 9, 0, 0, tzinfo=timezone.utc),
            heartbeat_at=datetime(2026, 3, 19, 9, 1, 0, tzinfo=timezone.utc),
        ),
    )
    run_path = agent_run_path(home, "alice")
    run_path.parent.mkdir(parents=True, exist_ok=True)
    AgentRunState(
        agent_uri=agent.agent_uri,
        agent_id=agent.agent_id[:12],
        agent_name=agent.agent_name,
        agent_home=str(agent.agent_home),
        source_file=agent.source_path.name,
        pid=999999,
        status="running",
        endpoint="http://127.0.0.1:8778",
        started_at=datetime(2026, 3, 19, 9, 0, 0, tzinfo=timezone.utc),
        heartbeat_at=datetime(2026, 3, 19, 9, 1, 0, tzinfo=timezone.utc),
    ).save(run_path)

    _drop_stale_running_agent(db_path, agent)

    assert get_running_agent(db_path, agent.agent_uri) is None
    assert AgentRunState.load(run_path).status == "stopped"


def test_cli_list_shows_known_agents_and_status(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "ID" in result.output
    assert "STATUS" in result.output
    assert "alice" in result.output
    assert "stopped" in result.output
    assert "agent://alice/alice.too" in result.output


def test_cli_list_marks_active_agent_running(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)
    upsert_running_agent(
        db_path,
        RunningAgentRecord(
            agent_uri=agent.agent_uri,
            pid=os.getpid(),
            status="running",
            endpoint="http://127.0.0.1:8778",
            started_at=datetime(2026, 3, 19, 9, 0, 0, tzinfo=timezone.utc),
            heartbeat_at=datetime(2026, 3, 19, 9, 1, 0, tzinfo=timezone.utc),
        ),
    )

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "running" in result.output
    assert "http://127.0.0.1:8778" in result.output


def test_cli_serve_rejects_docker_sandbox(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["serve", "alice", "--sandbox", "docker:python:3.13-slim"])

    assert result.exit_code == 1
    assert isinstance(result.exception, ToolangError)
    assert "toolang serve only supports host sandbox" in str(result.exception)


def test_cli_start_docker_stages_sandbox_launch(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    calls: dict[str, object] = {}

    def fake_remove(name: str) -> None:
        calls["removed"] = name

    def fake_run(**kwargs):
        calls.update(kwargs)
        return "container-123"

    monkeypatch.setattr("toolang.cli.runtime.docker_remove_container", fake_remove)
    monkeypatch.setattr("toolang.cli.runtime.docker_run_detached", fake_run)
    monkeypatch.setattr(
        "toolang.cli.runtime._wait_for_running_agent_sandbox",
        lambda **kwargs: None,
    )

    result = runner.invoke(
        app,
        [
            "start",
            "alice",
            "--sandbox",
            "docker:python:3.13-slim",
            "--port",
            "8779",
        ],
    )

    key = f"alice-{agent.agent_id[:12]}"
    args_path = sandbox_args_path(root, key)
    exec_path = sandbox_exec_path(root, key)
    stage_dir = sandbox_host(root, key)

    assert result.exit_code == 0
    assert result.stdout.strip() == f"started {agent.agent_id[:12]} http://127.0.0.1:8779"
    assert args_path.exists()
    assert exec_path.exists()
    assert stage_dir.exists()
    assert calls["image"] == "python:3.13-slim"
    assert calls["container_name"] == f"toolang-agent-alice-{agent.agent_id[:12]}"
    assert calls["published_host"] == "127.0.0.1"
    assert calls["published_port"] == 8779
    assert calls["workdir"] == home.resolve()
    assert calls["env_values"] == {"TOOLANG_ROOT": str(root.resolve())}
    mounts = [(str(source), str(target)) for source, target in cast(list[tuple[Path, Path]], calls["mounts"])]
    assert (str(root.resolve()), str(root.resolve())) in mounts
    assert (str(stage_dir.resolve()), str((home / ".toolang" / "agents" / "alice" / "sandbox").resolve())) in mounts

    args_payload = __import__("json").loads(args_path.read_text(encoding="utf-8"))
    assert args_payload["sandbox"]["image_name"] == "python:3.13-slim"
    exec_text = exec_path.read_text(encoding="utf-8")
    assert "toolang serve" in exec_text
    assert "--host 0.0.0.0" in exec_text
    assert "--sandbox docker:python:3.13-slim" in exec_text
    assert "--shared" in exec_text
    assert "--global" in exec_text


def test_hidden_path_commands_resolve_agent_paths(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    home_result = runner.invoke(app, ["home", "alice"])
    source_result = runner.invoke(app, ["source", "alice"])
    room_result = runner.invoke(app, ["room", "alice"])
    root_result = runner.invoke(app, ["home"])

    assert home_result.exit_code == 0
    assert source_result.exit_code == 0
    assert room_result.exit_code == 0
    assert root_result.exit_code == 0
    assert home_result.stdout.strip() == str(home.resolve())
    assert source_result.stdout.strip() == str(source_path.resolve())
    assert room_result.stdout.strip() == str((home / ".toolang" / "agents" / "alice").resolve())
    assert root_result.stdout.strip() == str(root.resolve())


def test_agent_new_creates_resident_agent_source_and_registry_entry(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["new", "alice"])

    source_path = root / "agents" / "alice" / "alice.too"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(source_path.resolve())
    assert "thunk chat(user):" in source_path.read_text(encoding="utf-8")
    assert not (root / "agents" / "alice" / ".toolang").exists()

    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0
    assert "agent://alice/alice.too" in listed.output


def test_agent_clone_copies_source_into_new_resident_home(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    source_home = root / "agents" / "source"
    source_home.mkdir(parents=True)
    source_path = source_home / "reviewer.too"
    source_text = SOURCE_FIXTURE.read_text(encoding="utf-8")
    source_path.write_text(source_text, encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["clone", "source/reviewer", "team/reviewer"])

    cloned_path = root / "agents" / "team" / "reviewer.too"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(cloned_path.resolve())
    assert cloned_path.read_text(encoding="utf-8") == source_text


def test_agent_clone_fetches_visiting_source_when_not_materialized(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    class FakeResponse:
        text = SOURCE_FIXTURE.read_text(encoding="utf-8")

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("toolang.cli.support.httpx.get", lambda *args, **kwargs: FakeResponse())

    result = runner.invoke(
        app,
        ["clone", "https://example.com/alice.too", "team/alice"],
    )

    cloned_path = root / "agents" / "team" / "alice.too"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(cloned_path.resolve())
    assert cloned_path.read_text(encoding="utf-8") == SOURCE_FIXTURE.read_text(encoding="utf-8")


def test_agent_remove_deletes_resident_agent_state_and_registry_entry(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    source_path = home / "alice.too"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    state_path = home / ".toolang" / "sync" / "alice.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    room = home / ".toolang" / "agents" / "alice"
    room.mkdir(parents=True)
    (room / "agent.log").write_text("", encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    result = runner.invoke(app, ["remove", "alice"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(source_path.resolve())
    assert not source_path.exists()
    assert not state_path.exists()
    assert not room.exists()
    assert not home.exists()
    assert find_known_agents_by_name(db_path, "alice") == []


def test_skill_add_writes_agent_source_from_current_home(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["skill", "add", "by3gus/pdf-processing"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(source_path.resolve())
    assert source_path.read_text(encoding="utf-8").startswith("use skill by3gus/pdf-processing\n\n")


def test_skill_add_writes_shared_source(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["skill", "add", "by3gus/pdf-processing", "--scope", "shared"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(shared_source_path(home).resolve())
    assert shared_source_path(home).read_text(encoding="utf-8") == "use skill by3gus/pdf-processing\n"


def test_skill_add_writes_global_source(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["skill", "add", "by3gus/pdf-processing", "--scope", "global"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(global_source_path(root).resolve())
    assert global_source_path(root).read_text(encoding="utf-8") == "use skill by3gus/pdf-processing\n"


def test_skill_remove_deletes_empty_shared_source(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    shared_source_path(home).write_text("use skill by3gus/pdf-processing\n", encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["skill", "remove", "pdf-processing", "--scope", "shared"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(shared_source_path(home).resolve())
    assert not shared_source_path(home).exists()


def test_skill_local_new_creates_shared_skill_dir_lazily(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["skill", "local", "new", "repo-search"])

    skill_dir = shared_caps_dir(home, "skill") / "repo-search"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(skill_dir.resolve())
    assert (skill_dir / "SKILL.md").exists()


def test_skill_local_path_prints_global_target_without_creating_dirs(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["skill", "local", "path", "repo-search", "--scope", "global"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str((global_caps_dir(root, "skill") / "repo-search").resolve())
    assert not global_caps_dir(root, "skill").exists()


def test_skill_local_new_from_ref_copies_remote_skill(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    def fake_resolve(kind: str, ref: str):
        from toolang_caps.models import ResolvedCapRef

        assert kind == "skill"
        return ResolvedCapRef(
            kind="skill",
            name="repo-search",
            ref=ref,
            repo="by3gus/agent-skills",
            path="skills/repo-search",
            rev="abc123",
        )

    def fake_fetch(_resolved):
        fetched_root = tmp_path / "fetched" / "materialized" / "repo-search"
        fetched_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REMOTE_SKILL_FIXTURE, fetched_root)
        files = sorted(
            str(path.relative_to(fetched_root))
            for path in fetched_root.rglob("*")
            if path.is_file()
        )
        return fetched_root, files

    monkeypatch.setattr("toolang.cli.caps.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.cli.caps.fetch_github_artifact", fake_fetch)

    result = runner.invoke(
        app,
        ["skill", "local", "new", "repo-search", "--from", "by3gus/repo-search"],
    )

    skill_dir = shared_caps_dir(home, "skill") / "repo-search"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(skill_dir.resolve())
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == (
        REMOTE_SKILL_FIXTURE / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_skill_local_delete_prunes_empty_kind_dir(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    skill_dir = shared_caps_dir(home, "skill") / "repo-search"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Repo Search\n", encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["skill", "local", "delete", "repo-search"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(skill_dir.resolve())
    assert not skill_dir.exists()
    assert not shared_caps_dir(home, "skill").exists()


@pytest.mark.parametrize(
    ("kind", "ref", "scope", "expected_name"),
    [
        ("service", "by3gus/github", "agent", "use service by3gus/github\n\n"),
        ("prompt", "by3gus/rewrite", "shared", "use prompt by3gus/rewrite\n"),
        ("psyche", "by3gus/reviewer", "global", "use psyche by3gus/reviewer\n"),
    ],
)
def test_text_cap_add_writes_expected_scope_source(
    tmp_path: Path,
    monkeypatch,
    kind: str,
    ref: str,
    scope: str,
    expected_name: str,
) -> None:
    typed_kind = cast(CapKind, kind)
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, [typed_kind, "add", ref, "--scope", scope])

    assert result.exit_code == 0
    if scope == "agent":
        target = home / "alice.too"
    elif scope == "shared":
        target = shared_source_path(home)
    else:
        target = global_source_path(root)
    assert result.stdout.strip() == str(target.resolve())
    assert target.read_text(encoding="utf-8").startswith(expected_name)


@pytest.mark.parametrize(
    ("kind", "ref", "fixture", "scope"),
    [
        ("service", "by3gus/github", REMOTE_SERVICE_FIXTURE, "shared"),
        ("prompt", "by3gus/rewrite", REMOTE_PROMPT_FIXTURE, "global"),
        ("psyche", "by3gus/reviewer", REMOTE_PSYCHE_FIXTURE, "shared"),
    ],
)
def test_text_cap_local_new_from_ref_copies_remote_file(
    tmp_path: Path,
    monkeypatch,
    kind: str,
    ref: str,
    fixture: Path,
    scope: str,
) -> None:
    typed_kind = cast(CapKind, kind)
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    def fake_resolve(actual_kind: CapKind, actual_ref: str):
        from toolang_caps.models import ResolvedCapRef

        assert actual_kind == typed_kind
        assert actual_ref == ref
        return ResolvedCapRef(
            kind=typed_kind,
            name=Path(fixture).stem,
            ref=ref,
            repo=f"by3gus/agent-{typed_kind}s",
            path=f"{typed_kind}s/{Path(fixture).name}",
            rev="abc123",
        )

    def fake_fetch(_resolved):
        fetched_file = tmp_path / "fetched" / "materialized" / fixture.name
        fetched_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, fetched_file)
        return fetched_file, [fixture.name]

    monkeypatch.setattr("toolang.cli.caps.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.cli.caps.fetch_github_artifact", fake_fetch)

    result = runner.invoke(
        app,
        [typed_kind, "local", "new", Path(fixture).stem, "--from", ref, "--scope", scope],
    )

    if scope == "shared":
        target = shared_caps_dir(home, typed_kind) / f"{Path(fixture).stem}.md"
    else:
        target = global_caps_dir(root, typed_kind) / f"{Path(fixture).stem}.md"
    assert result.exit_code == 0
    assert result.stdout.strip() == str(target.resolve())
    assert target.read_text(encoding="utf-8") == fixture.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("kind", "scope"),
    [
        ("service", "shared"),
        ("prompt", "global"),
        ("psyche", "shared"),
    ],
)
def test_text_cap_local_delete_prunes_empty_kind_dir(
    tmp_path: Path,
    monkeypatch,
    kind: str,
    scope: str,
) -> None:
    typed_kind = cast(CapKind, kind)
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    target_dir = (
        shared_caps_dir(home, typed_kind)
        if scope == "shared"
        else global_caps_dir(root, typed_kind)
    )
    target_dir.mkdir(parents=True)
    cap_path = target_dir / "demo.md"
    cap_path.write_text("demo\n", encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    monkeypatch.chdir(home)

    result = runner.invoke(app, [typed_kind, "local", "delete", "demo", "--scope", scope])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(cap_path.resolve())
    assert not cap_path.exists()
    assert not target_dir.exists()


def test_hidden_init_zsh_outputs_cd_helpers() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["init", "zsh"])

    assert exit_code == 0
    assert "# Add the emitted block to ~/.zshrc." in stderr.getvalue()
    assert "# Remove everything between the toolang markers to uninstall." in stderr.getvalue()
    assert "# Append it with:" in stderr.getvalue()
    assert "#   toolang init zsh >> ~/.zshrc" in stderr.getvalue()
    assert stderr.getvalue().endswith("\n\n")
    assert "# Add this block to ~/.zshrc." not in stdout.getvalue()
    assert "# >>> toolang shell helpers >>>" in stdout.getvalue()
    assert "# <<< toolang shell helpers <<<" in stdout.getvalue()
    assert "toohome() {" in stdout.getvalue()
    assert "tooroom() {" in stdout.getvalue()
    assert "{{" not in stdout.getvalue()
    assert "}}" not in stdout.getvalue()
    assert 'builtin cd -- "$(command toolang home "$@")"' in stdout.getvalue()
    assert 'builtin cd -- "$(command toolang room "$@")"' in stdout.getvalue()
    assert "toolang source" not in stdout.getvalue()


def test_hidden_init_fish_outputs_cd_helpers() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(["init", "fish"])

    assert exit_code == 0
    assert "# Add the emitted block to ~/.config/fish/config.fish." in stderr.getvalue()
    assert "# Remove everything between the toolang markers to uninstall." in stderr.getvalue()
    assert "# Append it with:" in stderr.getvalue()
    assert "#   toolang init fish >> ~/.config/fish/config.fish" in stderr.getvalue()
    assert stderr.getvalue().endswith("\n\n")
    assert "# Add this block to ~/.config/fish/config.fish." not in stdout.getvalue()
    assert "# >>> toolang shell helpers >>>" in stdout.getvalue()
    assert "# <<< toolang shell helpers <<<" in stdout.getvalue()
    assert "function toohome" in stdout.getvalue()
    assert "function tooroom" in stdout.getvalue()
    assert "cd (command toolang home $argv)" in stdout.getvalue()
    assert "cd (command toolang room $argv)" in stdout.getvalue()
    assert "toolang source" not in stdout.getvalue()
