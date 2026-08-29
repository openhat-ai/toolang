from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"


def test_harness_forwards_dev_wheel_and_command_without_requoting(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path)
    dist = tmp_path / "wheel directory"
    dist.mkdir()
    older = dist / "toolang-0.2.0-py3-none-any.whl"
    newer = dist / "toolang-0.3.0-py3-none-any.whl"
    older.touch()
    newer.touch()
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    staging_root = tmp_path / "staging root"
    staging_root.mkdir()

    completed = subprocess.run(
        (
            str(_harness()),
            "alpine:3.22",
            "--dev",
            str(dist),
            "--",
            "printf",
            "argument with spaces",
            "$literal",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(staging_root),
            "TOOLANG_TEST_DOCKER_LOG": str(log),
            "TOOLANG_TEST_DOCKER_STATUS": "7",
        },
    )

    assert completed.returncode == 7
    assert completed.stdout == "guest output\n"
    assert completed.stderr.splitlines() == [
        "Creating container · alpine:3.22...",
        f"Created container · {_CONTAINER_ID[:12]}",
        "Starting container...",
        f"Container stopped · {_CONTAINER_ID[:12]}",
    ]
    calls = _docker_calls(log)
    create = calls[0]
    assert create[0] == "create"
    assert any(
        item.endswith(":/tmp/toolang-guest/toolang-0.3.0-py3-none-any.whl:ro")
        for item in create
    )
    assert create[-3:] == ["printf", "argument with spaces", "$literal"]
    assert calls[1] == ["start", "--attach", _CONTAINER_ID]
    assert calls[2] == ["rm", "--force", _CONTAINER_ID]
    assert not list(staging_root.iterdir())


def test_harness_keep_retains_container_and_staging(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)

    completed = subprocess.run(
        (str(_harness()), "alpine:3.22", "--keep"),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "TOOLANG_TEST_DOCKER_LOG": str(log),
            "TOOLANG_TEST_DOCKER_STATUS": "0",
        },
    )

    assert completed.returncode == 0
    assert f"Container retained · {_CONTAINER_ID}" in completed.stderr
    staging_line = next(
        line
        for line in completed.stderr.splitlines()
        if line.startswith("Staging retained · ")
    )
    staging = Path(staging_line.removeprefix("Staging retained · "))
    assert staging.is_dir()
    assert not any(call[0] == "rm" for call in _docker_calls(log))
    shutil.rmtree(staging)


def test_harness_preserves_create_failure_and_cleans_staging(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)

    completed = subprocess.run(
        (str(_harness()), "missing:image"),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "TOOLANG_TEST_DOCKER_CREATE_STATUS": "9",
            "TOOLANG_TEST_DOCKER_LOG": str(log),
        },
    )

    assert completed.returncode == 9
    assert completed.stderr.splitlines() == ["Creating container · missing:image..."]
    assert [call[0] for call in _docker_calls(log)] == ["create"]
    assert not list(tmp_path.glob("toolang-docker-guest.*"))


def test_harness_preserves_guest_failure_diagnostics(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)

    completed = subprocess.run(
        (str(_harness()), "alpine:3.22"),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "TOOLANG_TEST_DOCKER_LOG": str(log),
            "TOOLANG_TEST_DOCKER_STATUS": "8",
            "TOOLANG_TEST_WRITE_DIAGNOSTIC": "1",
        },
    )

    assert completed.returncode == 8
    retained_line = next(
        line
        for line in completed.stderr.splitlines()
        if line.startswith("Diagnostic retained · ")
    )
    diagnostic = Path(retained_line.removeprefix("Diagnostic retained · "))
    assert diagnostic.read_text(encoding="utf-8") == "installer details\n"
    assert not any(path.is_dir() for path in tmp_path.glob("toolang-docker-guest.*"))


def test_harness_stops_and_removes_container_when_interrupted(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    ready = tmp_path / "docker-started"
    process = subprocess.Popen(
        (str(_harness()), "alpine:3.22"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={
            **os.environ,
            "PATH": f"{docker.parent}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "TOOLANG_TEST_DOCKER_BLOCK": "1",
            "TOOLANG_TEST_DOCKER_LOG": str(log),
            "TOOLANG_TEST_DOCKER_READY": str(ready),
        },
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    os.killpg(process.pid, signal.SIGINT)
    _stdout, _stderr = process.communicate(timeout=5)

    assert process.returncode == 130
    assert [call[0] for call in _docker_calls(log)] == [
        "create",
        "start",
        "stop",
        "rm",
    ]
    assert not list(tmp_path.glob("toolang-docker-guest.*"))


def _harness() -> Path:
    return Path(__file__).parents[3] / "scripts" / "try_docker_guest.sh"


def _fake_docker(root: Path) -> tuple[Path, Path]:
    bin_dir = root / "fake bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        '{ printf "CALL\\n"; for ARG in "$@"; do printf "ARG:%s\\n" "$ARG"; done; } '
        '>>"$TOOLANG_TEST_DOCKER_LOG"\n'
        'case "$1" in\n'
        "  create)\n"
        '    if [ "${TOOLANG_TEST_WRITE_DIAGNOSTIC:-0}" -eq 1 ]; then\n'
        '      for ARG in "$@"; do\n'
        '        case "$ARG" in\n'
        '          *:/tmp/toolang-guest-state) printf "%s\\n" '
        '"${ARG%:/tmp/toolang-guest-state}" >"$TOOLANG_TEST_DOCKER_LOG.state" ;;\n'
        "        esac\n"
        "      done\n"
        "    fi\n"
        '    [ "${TOOLANG_TEST_DOCKER_CREATE_STATUS:-0}" -eq 0 ] || '
        'exit "$TOOLANG_TEST_DOCKER_CREATE_STATUS"\n'
        f"    printf '{_CONTAINER_ID}\\n'\n"
        "    ;;\n"
        "  start)\n"
        '    if [ "${TOOLANG_TEST_DOCKER_BLOCK:-0}" -eq 1 ]; then\n'
        '      : >"$TOOLANG_TEST_DOCKER_READY"\n'
        "      trap 'exit 130' INT TERM HUP\n"
        "      while :; do sleep 1; done\n"
        "    fi\n"
        '    if [ "${TOOLANG_TEST_WRITE_DIAGNOSTIC:-0}" -eq 1 ]; then\n'
        '      STATE_DIR=$(cat "$TOOLANG_TEST_DOCKER_LOG.state")\n'
        '      printf "installer details\\n" >"$STATE_DIR/diagnostic.log"\n'
        "    fi\n"
        "    printf 'guest output\\n'\n"
        '    exit "${TOOLANG_TEST_DOCKER_STATUS:-0}"\n'
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker, root / "docker.log"


def _docker_calls(path: Path) -> list[list[str]]:
    calls: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "CALL":
            current = []
            calls.append(current)
        else:
            assert current is not None
            current.append(line.removeprefix("ARG:"))
    return calls
