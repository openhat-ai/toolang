from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

from toolang.base.types.sandbox import SandboxOutput, SandboxRef
import toolang.cli.toolang.main as cli
from toolang.cli.toolang.commands import runtime as runtime_commands
from toolang.common.layout import AgentLayout
from toolang.up import sandbox as sandbox_runtime
from toolang.up.server import ServeSpec


runner = CliRunner()


@pytest.mark.parametrize(
    ("args", "options"),
    [
        (["run", "--help"], ("--sandbox", "--allow", "--limit", "--default")),
        (["start", "--help"], ("--sandbox", "--allow", "--limit", "--default")),
        (
            ["chat", "alice", "--help"],
            ("--sandbox", "--allow", "--limit", "--default"),
        ),
        (["retry", "alice", "--help"], ("--allow", "--limit", "--default")),
        (["rerun", "alice", "--help"], ("--allow", "--limit", "--default")),
    ],
)
def test_policy_options_follow_cli_display_order(
    args: list[str],
    options: tuple[str, ...],
) -> None:
    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0, result.stderr
    output = strip_ansi(result.stdout)
    positions = tuple(output.index(option) for option in options)
    assert positions == tuple(sorted(positions))


def _create_agent(root: Path, name: str = "alice") -> AgentLayout:
    layout = AgentLayout.resident(root, name)
    layout.home.mkdir(parents=True)
    layout.program.write_text(f"agent {name}\n", encoding="utf-8")
    return layout


def _launch_spec(
    *,
    layout: AgentLayout,
    host: str,
    endpoint_host: str | None,
    port: int | None,
    sandbox: str | None,
    ceiling_overrides: Mapping[str, tuple[str, ...] | None],
    binding_overrides: Mapping[str, str | None],
    limit_overrides: Mapping[str, int | Decimal | None],
    file_inboxes: Sequence[Path] | None,
    dev: Path | None,
    log_spec: str | None,
    output: SandboxOutput,
    log_path: Path | None,
    environ: Mapping[str, str],
    **_kwargs: object,
) -> sandbox_runtime.LaunchSpec:
    return sandbox_runtime.LaunchSpec(
        serve=ServeSpec(
            layout=layout,
            host=host,
            endpoint_host=endpoint_host
            or ("localhost" if host == "127.0.0.1" else host),
            port=port or 7123,
            ceiling_overrides=ceiling_overrides,
            binding_overrides=binding_overrides,
            limit_overrides=limit_overrides,
            file_inboxes=tuple(file_inboxes or ()),
            log_spec=log_spec,
        ),
        sandbox=sandbox or "host",
        config={},
        environ=dict(environ),
        output=output,
        log_path=log_path,
        dev_artifact=dev,
    )


def test_run_resolves_sandbox_inputs_and_runs_in_foreground(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    layout = _create_agent(root)
    (root / "config.toml").write_text(
        '[limit]\ntokens = 1000\ncost = "5"\n',
        encoding="utf-8",
    )
    layout.config.write_text(
        "[limit]\ntokens = 2000\ntime = 120\n",
        encoding="utf-8",
    )
    dev = tmp_path / "dist"
    captured: dict[str, Any] = {}

    async def resolve_launch(**kwargs: Any) -> sandbox_runtime.LaunchSpec:
        captured["resolve"] = kwargs
        return _launch_spec(**kwargs)

    async def run(
        spec: sandbox_runtime.LaunchSpec,
        *,
        on_ready: Any,
    ) -> int:
        captured["run"] = spec
        on_ready(
            sandbox_runtime.SandboxState(
                sandbox=spec.sandbox,
                ref=SandboxRef(
                    runtime_id="workload-1",
                    endpoint=spec.serve.endpoint,
                ),
            )
        )
        return 0

    monkeypatch.setattr(sandbox_runtime, "resolve_launch", resolve_launch)
    monkeypatch.setattr(sandbox_runtime, "run", run)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(root),
            "run",
            "alice",
            "--sandbox",
            "docker:registry.example/a:b",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--allow",
            "models=openai/gpt-5[openai],o3",
            "--allow",
            "tools=filesystem,shell",
            "--allow",
            "caps=skill/reviewer",
            "--default",
            "model=openai/gpt-5",
            "--limit",
            "tokens=3000",
            "--limit",
            "cost=none",
            "--limit",
            "agic_tool_calls=40",
            "--dev",
            str(dev),
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    resolved = captured["resolve"]
    assert resolved["layout"] == layout
    assert resolved["sandbox"] == "docker:registry.example/a:b"
    assert resolved["host"] == "0.0.0.0"
    assert resolved["port"] == 8123
    assert resolved["dev"] == dev
    assert resolved["ceiling_overrides"] == {
        "models": ("openai/gpt-5[openai]", "o3"),
        "tools": ("filesystem", "shell"),
        "caps": ("skill/reviewer",),
    }
    assert resolved["binding_overrides"] == {"model": "openai/gpt-5"}
    assert resolved["limit_overrides"] == {
        "agic_tool_calls": 40,
        "tokens": 3000,
        "cost": None,
    }
    assert resolved["log_path"] is None
    assert resolved["output"] == "inherit"
    assert captured["run"] == _launch_spec(**resolved)
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Running agent alice: http://0.0.0.0:8123 (Ctrl+C to stop)"
    )


def test_runtime_dev_help_describes_wheel_selection() -> None:
    for command in ("run", "start"):
        result = runner.invoke(cli.app, [command, "--help"])

        assert result.exit_code == 0
        output = " ".join(strip_ansi(result.stdout).split())
        assert "Use a Toolang wheel" in output
        assert "newest wheel found" in output
        assert "recursively in a directory" in output


@pytest.mark.parametrize(
    ("sandbox", "dev", "development", "warns"),
    (
        ("docker:python:3.13", None, (True, Path("/source/toolang")), True),
        ("docker", None, (True, None), True),
        ("docker", None, (False, None), False),
        ("host", None, (True, Path("/source/toolang")), False),
        ("docker", Path("dist/toolang.whl"), (True, None), False),
    ),
)
def test_runtime_warns_when_development_source_uses_index_package(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sandbox: str,
    dev: Path | None,
    development: tuple[bool, Path | None],
    warns: bool,
) -> None:
    monkeypatch.setattr(
        runtime_commands,
        "development_source",
        lambda: development,
    )
    startup = cast(
        sandbox_runtime.LaunchSpec,
        SimpleNamespace(sandbox=sandbox),
    )

    runtime_commands._warn_development_sandbox_package(startup, dev=dev)

    stderr = capsys.readouterr().err
    assert ("may run a different version" in stderr) is warns
    if warns:
        assert "--dev dist" in stderr
        if development[1] is not None:
            assert str(development[1]) in stderr
        else:
            assert "source at" not in stderr


def test_roaming_file_options_accept_dev_wheel_directory() -> None:
    options = runtime_commands._parse_roaming_file_options(
        ["--inbox", "inbox", "--sandbox", "docker", "--dev", "dist"]
    )

    assert options.sandbox == "docker"
    assert options.dev == Path("dist")


def test_start_launches_in_background_and_reports_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)
    dev = tmp_path / "dist" / "toolang-1.2.3-py3-none-any.whl"
    captured: dict[str, Any] = {}

    async def resolve_launch(**kwargs: Any) -> sandbox_runtime.LaunchSpec:
        captured["resolve"] = kwargs
        return _launch_spec(**kwargs)

    async def launch(spec: sandbox_runtime.LaunchSpec) -> object:
        return type(
            "Handle",
            (),
            {
                "state": sandbox_runtime.SandboxState(
                    sandbox=spec.sandbox,
                    ref=SandboxRef(
                        runtime_id="workload-1",
                        endpoint=spec.serve.endpoint,
                    ),
                )
            },
        )()

    monkeypatch.setattr(sandbox_runtime, "resolve_launch", resolve_launch)
    monkeypatch.setattr(sandbox_runtime, "launch", launch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(root),
            "start",
            "alice",
            "--sandbox",
            "docker",
            "--dev",
            str(dev),
            "--port",
            "8124",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Started agent alice: http://localhost:8124"
    resolved = captured["resolve"]
    assert resolved["sandbox"] == "docker"
    assert resolved["dev"] == dev
    assert resolved["log_path"] == root / "agents" / "alice" / ".runtime" / "agent.log"
    assert resolved["output"] == "file"


def test_stop_forwards_force_to_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    layout = _create_agent(root)
    captured: dict[str, object] = {}

    async def stop(target: AgentLayout, *, force: bool = False) -> bool:
        captured.update(target=target, force=force)
        return True

    monkeypatch.setattr(sandbox_runtime, "stop", stop)

    result = runner.invoke(
        cli.app,
        ["--root", str(root), "stop", "alice", "--force"],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Stopped agent alice"
    assert captured == {"target": layout, "force": True}


def test_stop_rejects_agent_without_sandbox_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    async def stop(_target: AgentLayout, *, force: bool = False) -> bool:
        del force
        return False

    monkeypatch.setattr(sandbox_runtime, "stop", stop)

    result = runner.invoke(
        cli.app,
        ["--root", str(root), "stop", "alice"],
    )

    assert result.exit_code == 1
    assert "Agent alice not running" in result.stderr


def test_remove_releases_sandbox_resources_before_deleting_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    layout = _create_agent(root)
    calls: list[AgentLayout] = []

    async def release_for_removal(target: AgentLayout) -> None:
        assert target.home.is_dir()
        calls.append(target)

    monkeypatch.setattr(
        sandbox_runtime,
        "release_for_removal",
        release_for_removal,
    )

    result = runner.invoke(cli.app, ["--root", str(root), "remove", "alice"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Removed agent alice"
    assert calls == [layout]
    assert not layout.home.exists()
