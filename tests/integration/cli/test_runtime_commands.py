from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from toolang.base.types.hosting import HostingRef
import toolang.cli.toolang.main as cli
from toolang.common.layout import AgentLayout
from toolang.execution.executor import CeilingSpec
from toolang.up import hosting
from toolang.up.server import ServeSpec


runner = CliRunner()


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
    models: Sequence[str] | None,
    tools: Sequence[str] | None,
    caps: Sequence[str] | None,
    file_inboxes: Sequence[Path] | None,
    dev: Path | None,
    log_spec: str | None,
    environ: Mapping[str, str],
    **_kwargs: object,
) -> hosting.LaunchSpec:
    return hosting.LaunchSpec(
        serve=ServeSpec(
            layout=layout,
            host=host,
            endpoint_host=endpoint_host
            or ("localhost" if host == "127.0.0.1" else host),
            port=port or 7123,
            ceiling=CeilingSpec(
                models=tuple(models or ()) or None,
                tools=None if tools is None else tuple(tools),
                caps=tuple(caps or ()) or None,
            ),
            file_inboxes=tuple(file_inboxes or ()),
            log_spec=log_spec,
        ),
        sandbox=sandbox or "none",
        config={},
        environ=dict(environ),
        dev_artifact=dev,
    )


def test_run_resolves_hosting_inputs_and_runs_in_foreground(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    layout = _create_agent(root)
    captured: dict[str, Any] = {}

    async def resolve_launch(**kwargs: Any) -> hosting.LaunchSpec:
        captured["resolve"] = kwargs
        return _launch_spec(**kwargs)

    async def run(spec: hosting.LaunchSpec) -> int:
        captured["run"] = spec
        return 0

    monkeypatch.setattr(hosting, "resolve_launch", resolve_launch)
    monkeypatch.setattr(hosting, "run", run)

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
            "--models",
            "openai/gpt-5[openai],o3",
            "--tools",
            "filesystem,shell",
            "--caps",
            "skill/reviewer",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    resolved = captured["resolve"]
    assert resolved["layout"] == layout
    assert resolved["sandbox"] == "docker:registry.example/a:b"
    assert resolved["host"] == "0.0.0.0"
    assert resolved["port"] == 8123
    assert resolved["models"] == ["openai/gpt-5[openai],o3"]
    assert resolved["tools"] == ["filesystem,shell"]
    assert resolved["caps"] == ["skill/reviewer"]
    assert captured["run"] == _launch_spec(**resolved)


def test_start_launches_in_background_and_reports_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    async def resolve_launch(**kwargs: Any) -> hosting.LaunchSpec:
        return _launch_spec(**kwargs)

    async def launch(spec: hosting.LaunchSpec) -> object:
        return type(
            "Handle",
            (),
            {
                "state": hosting.HostingState(
                    sandbox=spec.sandbox,
                    ref=HostingRef(
                        runtime_id="workload-1",
                        endpoint=spec.serve.endpoint,
                    ),
                )
            },
        )()

    monkeypatch.setattr(hosting, "resolve_launch", resolve_launch)
    monkeypatch.setattr(hosting, "launch", launch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(root),
            "start",
            "alice",
            "--port",
            "8124",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Started agent alice: http://localhost:8124"


def test_stop_forwards_force_to_hosting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    layout = _create_agent(root)
    captured: dict[str, object] = {}

    async def stop(target: AgentLayout, *, force: bool = False) -> bool:
        captured.update(target=target, force=force)
        return True

    monkeypatch.setattr(hosting, "stop", stop)

    result = runner.invoke(
        cli.app,
        ["--root", str(root), "stop", "alice", "--force"],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Stopped agent alice"
    assert captured == {"target": layout, "force": True}


def test_stop_rejects_agent_without_hosting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    _create_agent(root)

    async def stop(_target: AgentLayout, *, force: bool = False) -> bool:
        del force
        return False

    monkeypatch.setattr(hosting, "stop", stop)

    result = runner.invoke(
        cli.app,
        ["--root", str(root), "stop", "alice"],
    )

    assert result.exit_code == 1
    assert "Agent alice not running" in result.stderr
