"""Opt-in checks for the fixed Docker guest across supported image shapes.

Run with ``uv run pytest -m live_docker --live-docker``.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
import shutil
import subprocess

from dotenv import dotenv_values
import pytest

from toolang.base.types.sandbox import SandboxRequest
from toolang.common.environment import (
    resolve_agent_environment,
    resolve_root_environment,
)
from toolang.common.layout import AgentLayout
from toolang.plugin.loading import create_sandbox


pytestmark = pytest.mark.live_docker


@pytest.mark.parametrize("content", [None, "", "EXCLUDED=home-only\n"])
def test_docker_mounts_preserve_host_environment_selection(tmp_path, content):
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    if content is not None:
        layout.env.write_text(content)
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "synthetic-key"})
    selected = resolve_agent_environment(root, layout)
    hosted_place = Path("/root/.toolang")
    hosted_home = hosted_place / "agents/alice"
    request = SandboxRequest(
        local_place=tmp_path,
        local_home=layout.home,
        hosted_place=hosted_place,
        hosted_home=hosted_home,
        agent_name="alice",
        bind_host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        endpoint="http://localhost:8123",
        command=("too", "_serve", "alice"),
        working_directory=layout.home,
        output="inherit",
        log_path=None,
        envs=dict(selected.guest_values),
    )
    plan = create_sandbox("docker", config={}).prepare(None, request)
    command = ["docker", "run", "--rm", "--network", "none"]
    for mount in plan.mounts:
        suffix = ":ro" if mount.read_only else ""
        command.extend(["--volume", f"{mount.local_path}:{mount.hosted_path}{suffix}"])
    command.extend(
        [
            "python:3.13-slim",
            "python",
            "-c",
            "from pathlib import Path; import sys; "
            "staged = Path(sys.argv[1]); "
            "assert staged.is_file(); "
            "home_env = Path(sys.argv[2]); "
            "assert not home_env.exists() or home_env.read_bytes() == staged.read_bytes(); "
            "print(staged.read_text(), end='')",
            plan.command[3],
            str(hosted_home / ".env"),
        ]
    )
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)

    assert completed.returncode == 0, completed.stderr
    assert dotenv_values(stream=StringIO(completed.stdout), interpolate=False) == dict(
        selected.guest_values
    )
    assert layout.env.exists() == (content is not None)
    if content is not None:
        assert layout.env.read_text() == content
    after = resolve_agent_environment(root, layout)
    assert after.values == selected.values
    assert after.picked == selected.picked


@pytest.mark.parametrize(
    ("image", "uv_line", "python_line"),
    (
        (
            "ghcr.io/astral-sh/uv:python3.13-bookworm-slim",
            "Using uv · ",
            "Using Python · ",
        ),
        ("python:3.13-slim", "Installed uv · ", "Using Python · "),
        (
            "buildpack-deps:bookworm-curl",
            "Installed uv · ",
            "Installed Python · ",
        ),
    ),
)
def test_guest_harness_installs_and_runs_current_wheel(
    image: str,
    uv_line: str,
    python_line: str,
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    repository = Path(__file__).parents[3]
    wheel_dir = tmp_path / "wheel"
    built = subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stderr

    completed = subprocess.run(
        (
            str(repository / "scripts" / "try_docker_guest.sh"),
            image,
            "--dev",
            str(wheel_dir),
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert uv_line in completed.stderr
    assert python_line in completed.stderr
    assert "Installed Toolang · v" in completed.stderr
    assert completed.stdout.startswith("v")
