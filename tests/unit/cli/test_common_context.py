from __future__ import annotations

from pathlib import Path

from toolang.cli.common.context import load_runtime_environ
from toolang.common.layout import AgentLayout


def test_runtime_environ_treats_dotenv_values_as_literals(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("LITERAL='${HOME}/agent'\n", encoding="utf-8")

    environ = load_runtime_environ(
        layout,
        base_environ={
            "HOME": "/host/home",
        },
    )

    assert environ["LITERAL"] == "${HOME}/agent"


def test_controller_settings_are_resolved_without_forwarding_them(
    tmp_path, monkeypatch
):
    import os
    from typer._click import Context
    from typer.core import TyperCommand
    from toolang.cli.common.context import (
        CliContext,
        command_environment,
        startup_environ,
        resolve_model_catalog_option,
    )

    shell = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "synthetic",
        "TOOLANG_AGENT_PORT": "8123",
        "TOOLANG_LIMIT_TOKENS": "123",
        "TOOLANG_MODEL_CATALOG": str(tmp_path / "models.json"),
        "TOOLANG_CORS_ALLOWED_ORIGINS": "https://ui.example",
        "TOOLANG_UNRECOGNIZED": "excluded",
    }
    state = CliContext(root=tmp_path, agent="alice", shell_environment=shell)
    context = Context(TyperCommand("serve"), obj=state, info_name="serve")
    before = dict(os.environ)
    with command_environment(context):
        assert dict(os.environ) == {"PATH": "/bin", "OPENAI_API_KEY": "synthetic"}
        settings = startup_environ(os.environ)
        assert settings["TOOLANG_AGENT_PORT"] == "8123"
        assert settings["TOOLANG_LIMIT_TOKENS"] == "123"
        assert settings["TOOLANG_CORS_ALLOWED_ORIGINS"] == "https://ui.example"
        assert "TOOLANG_UNRECOGNIZED" not in settings
        assert resolve_model_catalog_option(None) == tmp_path / "models.json"
    assert dict(os.environ) == before
    assert "TOOLANG_AGENT_PORT" not in startup_environ({})


def test_setup_captures_environment_before_cross_thread_refresh(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from toolang.common.environment import (
        resolve_root_environment,
        resolve_agent_environment,
        environment_scope,
    )
    from toolang.setup.watcher import SetupWatcher

    layout = AgentLayout.resident(tmp_path, "alice")
    (tmp_path / ".alice.env").write_text("TOKEN\n")
    root = resolve_root_environment(tmp_path, {"TOKEN": "startup"})
    with environment_scope(resolve_agent_environment(root, layout)):
        watcher = SetupWatcher(layout)
    (tmp_path / ".alice.env").write_text("TOKEN=changed\nEXCLUDED=not-admitted\n")
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(watcher._load_inputs).result().envs == {"TOKEN": "startup"}


def test_launch_parses_controller_settings_without_widening_workload(
    tmp_path, monkeypatch
):
    from toolang.cli.common import startup

    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(startup.sandbox_runtime, "resolve_launch", launch)
    startup.resolve_runtime_launch(
        AgentLayout.resident(tmp_path, "alice"),
        toolang_root=tmp_path,
        environ={"ADMITTED": "synthetic"},
        guest_environ={"ADMITTED": "synthetic"},
        settings_environ={
            "TOOLANG_AGENT_PORT": "8123",
            "TOOLANG_LIMIT_TOKENS": "123",
            "TOOLANG_MODEL_CATALOG": str(tmp_path / "models.json"),
            "TOOLANG_CORS_ALLOWED_ORIGINS": "https://ui.example, https://other.example",
            "TOOLANG_UI_BASE_URL": "https://ui.example",
            "PRIVATE_SENTINEL": "excluded",
        },
        sandbox="host",
        dev=None,
        lifetime="persistent",
        background=True,
    )
    assert captured["environ"] == {
        "ADMITTED": "synthetic",
        "TOOLANG_ROOT": str(tmp_path),
        "TOOLANG_MODEL_CATALOG": str(tmp_path / "models.json"),
    }
    assert captured["port"] == 8123
    assert captured["guest_environ"] == {"ADMITTED": "synthetic"}
    assert captured["limit_overrides"] == {"tokens": 123}
    assert captured["cors_allowed_origins"] == (
        "https://ui.example",
        "https://other.example",
    )
    assert captured["ui_base_url"] == "https://ui.example"
