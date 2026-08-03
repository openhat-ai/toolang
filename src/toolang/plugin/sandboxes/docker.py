"""Hosting inside Docker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any
from uuid import uuid4

from toolang.base.errors import ToolangError
from toolang.base.protocols.hosting import Hosting
from toolang.base.types.hosting import (
    HostingMount,
    HostingPlan,
    HostingPort,
    HostingRef,
    HostingRequest,
)

DEFAULT_IMAGE = "python:3.13-slim"


@dataclass(slots=True)
class DockerHosting:
    """Stage and run the AgentServer as a Docker container's main workload."""

    config: dict[str, Any]
    name: str = "docker"
    _default_image: str | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        image = str(self.config.get("image", "")).strip()
        self._default_image = image or None

    def prepare(self, spec: str | None, request: HostingRequest) -> HostingPlan:
        image = _image(spec, self._default_image)
        stage_dir = request.local_root / ".sandbox" / request.agent_name
        runtime_dir = request.hosted_home / ".runtime" / "sandbox"
        stage_dir.mkdir(parents=True, exist_ok=True)

        hosted_dev_artifact: Path | None = None
        extra_mounts: list[HostingMount] = []
        if request.local_dev_artifact is not None:
            artifact = request.local_dev_artifact
            if artifact.is_file():
                staged = stage_dir / artifact.name
                if artifact.resolve() != staged.resolve():
                    shutil.copy2(artifact, staged)
                hosted_dev_artifact = runtime_dir / staged.name
            elif _path_is_within(artifact, request.local_root):
                hosted_dev_artifact = _translate_path(
                    artifact,
                    local_root=request.local_root,
                    hosted_root=request.hosted_root,
                )
            else:
                hosted_dev_artifact = runtime_dir / "dev"
                extra_mounts.append(
                    HostingMount(
                        local_path=artifact,
                        hosted_path=hosted_dev_artifact,
                        read_only=True,
                    )
                )

        script_path = stage_dir / "start.sh"
        _write_start_script(
            script_path,
            command=request.command,
            hosted_dev_artifact=hosted_dev_artifact,
        )
        (stage_dir / "start.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "agent": request.agent_name,
                    "image": image,
                    "local_root": str(request.local_root),
                    "hosted_root": str(request.hosted_root),
                    "hosted_home": str(request.hosted_home),
                    "endpoint": request.endpoint,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        mounts = [
            *request.mounts,
            HostingMount(request.local_home, request.hosted_home),
            HostingMount(stage_dir, runtime_dir),
            *extra_mounts,
        ]
        container_name = (
            f"toolang-{_container_label(request.agent_name)}-{uuid4().hex[:8]}"
        )
        return HostingPlan(
            sandbox=f"{self.name}:{image}",
            command=("/bin/sh", str(runtime_dir / "start.sh")),
            working_directory=request.hosted_home,
            log_path=request.log_path,
            endpoint=request.endpoint,
            envs={
                **request.envs,
                "TOOLANG_ROOT": str(request.hosted_root),
                "TOOLANG_SANDBOX": f"{self.name}:{image}",
            },
            mounts=tuple(mounts),
            ports=(
                HostingPort(
                    bind_host=request.bind_host,
                    local_port=request.port,
                    hosted_port=request.port,
                ),
            ),
            meta={
                "container_name": container_name,
                "image": image,
                "stage_dir": str(stage_dir),
            },
        )

    async def launch(self, plan: HostingPlan) -> HostingRef:
        container_name = _plan_text(plan, "container_name")
        image = _plan_text(plan, "image")
        if len(plan.ports) != 1:
            raise ValueError("docker hosting requires exactly one published port")
        port = plan.ports[0]
        try:
            container_id = await asyncio.to_thread(
                docker_run_detached,
                image=image,
                container_name=container_name,
                workdir=str(plan.working_directory),
                command=list(plan.command),
                mounts=plan.mounts,
                bind_host=port.bind_host,
                published_port=port.local_port,
                hosted_port=port.hosted_port,
                env_values=plan.envs,
            )
        except RuntimeError as exc:
            raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
        return HostingRef(
            runtime_id=container_name,
            endpoint=plan.endpoint,
            meta={
                "container_id": container_id,
                "image": image,
                "stage_dir": _plan_text(plan, "stage_dir"),
                "follow_logs": plan.log_path is None,
            },
        )

    async def running(self, ref: HostingRef) -> bool:
        return await asyncio.to_thread(docker_container_running, ref.runtime_id)

    async def wait(self, ref: HostingRef) -> int:
        if ref.meta.get("follow_logs") is True:
            await asyncio.to_thread(docker_follow_container_logs, ref.runtime_id)
        return await asyncio.to_thread(docker_wait_container, ref.runtime_id)

    async def stop(self, ref: HostingRef, *, force: bool = False) -> None:
        await asyncio.to_thread(
            docker_stop_container,
            ref.runtime_id,
            force=force,
        )

    async def release(self, ref: HostingRef) -> None:
        await asyncio.to_thread(docker_remove_container, ref.runtime_id)
        stage_dir = ref.meta.get("stage_dir")
        if isinstance(stage_dir, str) and stage_dir:
            await asyncio.to_thread(shutil.rmtree, stage_dir, True)


def create_hosting(config: Mapping[str, Any]) -> Hosting:
    """Create built-in Docker hosting."""

    return DockerHosting(dict(config))


def _image(spec: str | None, configured: str | None) -> str:
    if spec is not None:
        image = spec.strip()
        if not image:
            raise ValueError("docker sandbox spec cannot be empty")
        return image
    return configured or DEFAULT_IMAGE


def _container_label(value: str) -> str:
    label = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value
    ).strip("-_.")
    return label or "agent"


def _plan_text(plan: HostingPlan, key: str) -> str:
    value = plan.meta.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"docker hosting plan is missing {key}")
    return value


def _write_start_script(
    path: Path,
    *,
    command: tuple[str, ...],
    hosted_dev_artifact: Path | None,
) -> None:
    if not command:
        raise ValueError("docker hosting requires a command")
    source = str(hosted_dev_artifact) if hosted_dev_artifact is not None else "toolang"
    tool_command = command if command[0] in {"too", "toolang"} else ("too", *command)
    lines = [
        "#!/bin/sh",
        "set -eu",
        'export PATH="$HOME/.local/bin:$PATH"',
        'have() { command -v "$1" >/dev/null 2>&1; }',
        'PYTHON_BIN=""',
        'if have python; then PYTHON_BIN="python"; elif have python3; then PYTHON_BIN="python3"; fi',
        "ensure_uv() {",
        "  have uv && return 0",
        '  [ -n "$PYTHON_BIN" ] || { echo "python not available" >&2; exit 127; }',
        '  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true',
        '  "$PYTHON_BIN" -m pip install --disable-pip-version-check --user -U uv >/dev/null 2>&1 || true',
        "  have uv && return 0",
        "  if have curl; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi",
        "  have uv || { echo 'uv not available' >&2; exit 127; }",
        "}",
        "ensure_uv",
        "exec uv tool run --from "
        + shlex.quote(source)
        + " "
        + shlex.join(tool_command),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _translate_path(path: Path, *, local_root: Path, hosted_root: Path) -> Path:
    return hosted_root / path.resolve().relative_to(local_root.resolve())


def docker_container_running(container_name: str) -> bool:
    result = _docker(
        "inspect",
        "--format",
        "{{.State.Running}}",
        container_name,
        check=False,
    )
    return (
        result is not None
        and result.returncode == 0
        and result.stdout.strip() == "true"
    )


def docker_wait_container(container_name: str) -> int:
    result = _docker("wait", container_name, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker wait failed")
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("docker wait returned an invalid exit code") from exc


def docker_follow_container_logs(container_name: str) -> None:
    try:
        result = subprocess.run(
            ("docker", "logs", "--follow", container_name),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found") from exc
    if result.returncode != 0:
        raise RuntimeError("docker logs failed")


def docker_stop_container(container_name: str, *, force: bool) -> None:
    command = "kill" if force else "stop"
    result = _docker(command, container_name, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "No such container" not in detail:
            raise RuntimeError(detail or f"docker {command} failed")


def docker_remove_container(container_name: str) -> None:
    _docker("rm", "--force", container_name, check=False)


def docker_run_detached(
    *,
    image: str,
    container_name: str,
    workdir: str,
    command: list[str],
    mounts: tuple[HostingMount, ...],
    bind_host: str,
    published_port: int,
    hosted_port: int,
    env_values: Mapping[str, str],
) -> str:
    args = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--workdir",
        workdir,
        "--publish",
        f"{bind_host}:{published_port}:{hosted_port}",
    ]
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        args.extend(["--volume", f"{mount.local_path}:{mount.hosted_path}{suffix}"])
    for name, value in env_values.items():
        args.extend(["--env", f"{name}={value}"])
    args.append(image)
    args.extend(command)
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found") from exc
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"docker exited with code {result.returncode}"
        )
    return result.stdout.strip()


def _docker(
    *args: str,
    check: bool,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("docker", *args),
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
