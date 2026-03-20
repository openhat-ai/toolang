from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Mapping


def docker_container_name(agent_name: str, agent_id: str) -> str:
    return f"toolang-agent-{agent_name}-{agent_id[:12]}"


def docker_container_running(container_name: str) -> bool:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                container_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def docker_remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return


def docker_run_detached(
    *,
    image: str,
    container_name: str,
    workdir: Path,
    command: list[str],
    mounts: list[tuple[Path, Path]],
    published_host: str,
    published_port: int,
    env_names: Iterable[str],
    env_values: Mapping[str, str],
) -> str:
    args = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--workdir",
        str(workdir),
        "--publish",
        f"{published_host}:{published_port}:{published_port}",
    ]
    for source, target in mounts:
        args.extend(["--volume", f"{source}:{target}"])
    for name in env_names:
        args.extend(["--env", name])
    for name, value in env_values.items():
        args.extend(["--env", f"{name}={value}"])
    args.append(image)
    args.extend(command)
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or f"docker exited with code {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout.strip()
