"""Opt-in checks for the fixed Docker guest across supported image shapes.

Run with ``uv run pytest -m live_docker --live-docker``.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

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
        ("python:3.10-slim", "Installed uv · ", "Installed Python · "),
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


@pytest.mark.parametrize("native_uv", [False, True])
def test_guest_preserves_managed_image_packages_as_nonroot(
    native_uv: bool, tmp_path: Path
) -> None:
    """System Python protection and pip's target must not redirect Toolang."""
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    repository = Path(__file__).parents[3]
    resource = f"toolang-test-guest-isolation-{uuid4().hex}"
    image = f"{resource}:test"
    uv_layer = (
        "COPY --from=ghcr.io/astral-sh/uv:debian-slim "
        "/usr/local/bin/uv /usr/local/bin/uv\n"
        if native_uv
        else ""
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim\n"
        + uv_layer
        + "RUN python -m pip install --no-cache-dir --disable-pip-version-check httpx==0.27.0\n"
        "COPY EXTERNALLY-MANAGED /usr/local/lib/python3.13/EXTERNALLY-MANAGED\n"
        "ENV PIP_TARGET=/opt/application-dependencies\n"
        "USER 65534:65534\n"
    )
    (tmp_path / "EXTERNALLY-MANAGED").write_text(
        "[externally-managed]\nError=Image-owned Python environment\n"
    )
    try:
        built = subprocess.run(
            ("docker", "build", "--tag", image, str(tmp_path)),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        wheel_dir = tmp_path / "wheel"
        wheel = subprocess.run(
            ("uv", "build", "--wheel", "--out-dir", str(wheel_dir)),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert wheel.returncode == 0, wheel.stderr
        state = tmp_path / "state"
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        source = repository / "src/toolang/plugin/sandboxes/docker"
        package = next(wheel_dir.glob("*.whl"))
        completed = subprocess.run(
            (
                "docker",
                "run",
                "--rm",
                "--name",
                resource,
                "--volume",
                f"{source}:/bootstrap:ro",
                "--volume",
                f"{wheel_dir}:/packages:ro",
                "--volume",
                f"{state}:/state",
                image,
                "/bin/sh",
                "/bootstrap/guest.sh",
                "/state/diagnostic.log",
                str(state / "diagnostic.log"),
                "-",
                f"/packages/{package.name}",
                "-",
                "--",
                "/bin/sh",
                "-c",
                'too --version && python3 -c "import httpx; print(httpx.__version__)" '
                '&& test ! -e "$PIP_TARGET"',
            ),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.splitlines()[0].startswith("v")
        assert completed.stdout.splitlines()[1:] == ["0.27.0"]
        assert ("Using uv" in completed.stderr) == native_uv
    finally:
        subprocess.run(
            ("docker", "rm", "--force", resource),
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ("docker", "image", "rm", image),
            capture_output=True,
            timeout=30,
        )
