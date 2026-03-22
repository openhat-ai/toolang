from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping

from .docker import docker_container_name, docker_container_running

HOST_SANDBOX = "host"


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    kind: Literal["host", "docker"]
    image: str | None = None

    @property
    def spec(self) -> str:
        if self.kind == "docker" and self.image:
            return f"docker:{self.image}"
        return HOST_SANDBOX

    @property
    def execution_host(self) -> str:
        if self.kind == "docker":
            return "docker"
        return "local"


def normalize_sandbox_spec(value: str | None, *, fallback: str = HOST_SANDBOX) -> str:
    raw = (value or fallback).strip()
    if not raw or raw == "none":
        return HOST_SANDBOX
    return raw


def parse_sandbox_spec(value: str | None, *, fallback: str = HOST_SANDBOX) -> SandboxSpec:
    spec = normalize_sandbox_spec(value, fallback=fallback)
    if spec == HOST_SANDBOX:
        return SandboxSpec(kind="host")
    if not spec.startswith("docker:"):
        raise ValueError("unsupported sandbox value; use 'host' or 'docker:<image>'")
    image = spec.split(":", 1)[1].strip()
    if not image:
        raise ValueError("docker sandbox must include an image")
    return SandboxSpec(kind="docker", image=image)


def sandbox_key(agent_name: str, agent_id: str) -> str:
    return f"{agent_name}-{agent_id[:12]}"


def host_pid_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def sandbox_process_alive(
    *,
    sandbox_spec: str,
    pid: int | None,
    agent_name: str,
    agent_id: str,
) -> bool:
    try:
        parsed = parse_sandbox_spec(sandbox_spec)
    except ValueError:
        return host_pid_exists(pid)
    if parsed.kind == "docker":
        return docker_container_running(docker_container_name(agent_name, agent_id))
    return host_pid_exists(pid)


def write_sandbox_args_file(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_sandbox_exec_file(
    path: Path,
    *,
    command: Iterable[str] | None = None,
    shell_command: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if shell_command is None:
        if command is None:
            raise ValueError("sandbox exec file requires command or shell_command")
        shell_command = "exec " + shlex.join(list(command))
    script = "#!/bin/sh\nset -eu\n" + shell_command + "\n"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def forwarded_sandbox_env_names(environment: Mapping[str, str]) -> list[str]:
    names: set[str] = set()
    for name in environment:
        if name.startswith("TOOLANG_"):
            names.add(name)
            continue
        if name.startswith("OPENAI_"):
            names.add(name)
            continue
        if name.endswith("_API_KEY"):
            names.add(name)
            continue
        if name in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        }:
            names.add(name)
    return sorted(names)
