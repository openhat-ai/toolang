from __future__ import annotations

import io
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolang.agent_refs import resolve_agent_ref
from toolang.agent_registry import RunningAgentRecord, get_running_agent, upsert_running_agent
from toolang.cli import _drop_stale_running_agent, _remember_agent, _resolve_cli_agent, app, main
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.layout import (
    agent_run_path,
    agents_db_path,
    global_caps_dir,
    global_source_path,
    shared_caps_dir,
    shared_source_path,
)

runner = CliRunner()
SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"
REMOTE_SKILL_FIXTURE = Path(__file__).parent / "fixtures" / "remote-skill" / "pdf-processing"


def test_cli_has_expected_subcommands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "invoke" in result.output
    assert "sync" in result.output
    assert "serve" in result.output
    assert "start" in result.output
    assert "skill" in result.output
    assert "bus" in result.output
    assert "home" not in result.output
    assert "source" not in result.output
    assert "room" not in result.output
    assert "init" not in result.output
    assert "check" not in result.output
    assert "dump-ast" not in result.output


def test_cli_shows_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"toolang {package_version('toolang')}"


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

    def fake_resolve(ref: str):
        from toolang_caps.models import ResolvedCapRef

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

    monkeypatch.setattr("toolang.cli.resolve_github_skill_ref", fake_resolve)
    monkeypatch.setattr("toolang.cli.fetch_github_tree", fake_fetch)

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
