"""Opt-in checks for the fixed Docker guest across supported image shapes.

Run with ``uv run pytest -m live_docker --live-docker``.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.live_docker


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
    assert "Installed Toolang · " in completed.stderr
    assert "toolang " in completed.stdout
