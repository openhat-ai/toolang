"""Sandbox inside Docker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any
from uuid import uuid4

from toolang.base.errors import SandboxLaunchError, ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.progress import ProgressSink, ProgressStatus
from toolang.base.types.sandbox import (
    SandboxLocation,
    SandboxMount,
    SandboxPlan,
    SandboxPort,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.progress import emit_progress

from ._docker_cli import (
    DEFAULT_HOST_GATEWAY,
    docker_append_container_logs,
    docker_container_running,
    docker_follow_container_logs,
    docker_remove_container,
    docker_run_detached,
    docker_stop_container,
    docker_wait_container,
    finish_process,
)
from ._docker_guest import (
    DOCKER_TOOLANG_COMPATIBILITY_ERROR,
    prepare_background_log,
    prepare_sandbox_instance,
    prepare_stage_directory,
    prepare_startup_events,
    remove_stage_directory,
    remove_startup_events,
    validate_guest_environment,
    write_agent_script,
    write_bootstrap,
    write_guest_env,
    write_sandbox_instance,
    write_start_script,
)

DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_ENVIRONMENT_ALLOW_PATTERN = (
    r"(?i)^(?:"
    r"TOOLANG_[A-Z0-9_]+|PY_LOG|"
    r"(?:HTTP|HTTPS|ALL|NO)_PROXY|"
    r"SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|"
    r"(?:PIP|UV)_[A-Z0-9_]+|"
    r"(?:OPENAI|ANTHROPIC|GOOGLE|GEMINI|MISTRAL|GROQ|COHERE|XAI|"
    r"DEEPSEEK|OPENROUTER|TOGETHER|FIREWORKS|PERPLEXITY|AZURE|AWS|"
    r"BEDROCK|VERTEX|HF|HUGGING_FACE|OLLAMA|LLAMA_CPP)_[A-Z0-9_]+"
    r")$"
)
_CONTROL_ENV_NAMES = frozenset(
    {
        "TOOLANG_HOST_GATEWAY",
        "TOOLANG_ROOT",
        "TOOLANG_SANDBOX",
        "TOOLANG_SANDBOX_DESCRIPTION",
        "TOOLANG_SANDBOX_INSTANCE",
    }
)
_STARTUP_EVENT_INTERVAL_SEC = 0.05
_STARTUP_EVENT_MAX_BYTES = 1024
_STARTUP_EVENT_MAP: dict[str, tuple[str, str, ProgressStatus]] = {
    "install.running": ("install", "Installing Toolang", "running"),
    "install.ok": ("install", "Installing Toolang", "ok"),
    "install.failed": ("install", "Installing Toolang", "failed"),
    "validate.running": ("validate", "Validating Toolang", "running"),
    "validate.ok": ("validate", "Validating Toolang", "ok"),
    "validate.failed": ("validate", "Validating Toolang", "failed"),
    "server.running": ("server", "Starting agent server", "running"),
}
_STARTUP_EVENT_TRANSITIONS = {
    None: frozenset({"install.running"}),
    "install.running": frozenset({"install.ok", "install.failed"}),
    "install.ok": frozenset({"validate.running"}),
    "validate.running": frozenset({"validate.ok", "validate.failed"}),
    "validate.ok": frozenset({"server.running"}),
}


def _linked_agent_file_mounts(
    request: SandboxRequest,
) -> tuple[SandboxMount, ...]:
    """Keep source-local roaming agent links readable in the guest."""

    mounts: list[SandboxMount] = []
    for name in ("agent.too", "config.toml"):
        local_path = request.local_home / name
        if not local_path.is_symlink():
            continue
        target = local_path.resolve(strict=True)
        if not target.is_file():
            raise ValueError(f"linked agent file is not a regular file: {local_path}")
        mounts.append(
            SandboxMount(
                target,
                request.hosted_home / name,
                read_only=True,
            )
        )
    return tuple(mounts)


@dataclass(slots=True)
class DockerSandbox:
    """Stage and run the AgentServer as a Docker container's main workload."""

    config: dict[str, Any]
    name: str = "docker"
    location: SandboxLocation = "guest"
    _default_image: str | None = field(init=False, repr=False, default=None)
    _runtime_root: Path = field(init=False, repr=False)
    _environment_allow_pattern: re.Pattern[str] = field(init=False, repr=False)
    _log_followers: dict[str, asyncio.subprocess.Process] = field(
        init=False,
        repr=False,
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        raw_image = self.config.get("image")
        if raw_image is None:
            self._default_image = None
        elif isinstance(raw_image, str) and raw_image.strip():
            self._default_image = raw_image.strip()
        else:
            raise TypeError("docker image must be a non-empty string")
        raw_root = self.config.get("root")
        if raw_root is None:
            self._runtime_root = Path("/root/.toolang")
        elif isinstance(raw_root, str) and raw_root.strip():
            self._runtime_root = Path(raw_root.strip())
        else:
            raise TypeError("docker root must be a non-empty string")
        if not self._runtime_root.is_absolute():
            raise ValueError("docker root must be an absolute guest path")
        raw_pattern = self.config.get(
            "environment_allow_pattern",
            DEFAULT_ENVIRONMENT_ALLOW_PATTERN,
        )
        if not isinstance(raw_pattern, str):
            raise TypeError("docker environment_allow_pattern must be a string")
        try:
            self._environment_allow_pattern = re.compile(raw_pattern)
        except re.error as exc:
            raise ValueError(
                f"invalid docker environment_allow_pattern: {exc}"
            ) from exc

    def runtime_root(self, local_root: Path) -> Path:
        """Return the Toolang root configured for the Docker guest."""

        del local_root
        return self._runtime_root

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        stage_dir = (
            request.local_root
            / ".sandbox"
            / request.agent_name
            / "launches"
            / uuid4().hex[:8]
        )
        startup_events_path = (
            request.local_home / ".runtime" / f"sandbox-startup-{stage_dir.name}.events"
        )
        try:
            return self._prepare(
                spec,
                request,
                stage_dir=stage_dir,
                startup_events_path=startup_events_path,
            )
        except BaseException:
            remove_stage_directory(stage_dir, ignore_errors=True)
            remove_startup_events(startup_events_path, ignore_errors=True)
            raise

    def _prepare(
        self,
        spec: str | None,
        request: SandboxRequest,
        *,
        stage_dir: Path,
        startup_events_path: Path,
    ) -> SandboxPlan:
        image = _image(spec, self._default_image)
        runtime_dir = request.hosted_home / ".runtime" / "sandbox"
        dotenv_envs, process_envs = self._guest_environment_sections(request)
        validate_guest_environment(dotenv_envs)
        validate_guest_environment(process_envs)
        hosted_log_path = prepare_background_log(request)
        prepare_stage_directory(stage_dir)
        sandbox_instance_path = stage_dir / "instance"
        prepare_sandbox_instance(sandbox_instance_path)
        prepare_startup_events(startup_events_path)

        hosted_dev_artifact: Path | None = None
        if request.local_dev_artifact is not None:
            artifact = request.local_dev_artifact
            if not artifact.is_file() or artifact.suffix.casefold() != ".whl":
                raise ValueError("docker development artifact must be a wheel file")
            staged = stage_dir / artifact.name
            if artifact.resolve() != staged.resolve():
                shutil.copy2(artifact, staged)
            hosted_dev_artifact = runtime_dir / staged.name

        write_agent_script(
            stage_dir / "agent.sh",
            command=request.command,
            hosted_dev_artifact=hosted_dev_artifact,
            sandbox_instance_path=runtime_dir / sandbox_instance_path.name,
            startup_events_path=(
                request.hosted_home / ".runtime" / startup_events_path.name
            ),
            validation_error_to_stderr=request.output == "file",
        )
        write_bootstrap(stage_dir / "bootstrap.py")
        write_start_script(
            stage_dir / "start.sh",
            runtime_dir=runtime_dir,
            guest_env_path=request.hosted_home / ".env",
            log_path=hosted_log_path,
        )
        guest_env_path = stage_dir / "guest.env"
        write_guest_env(
            guest_env_path,
            dotenv_envs=dotenv_envs,
            process_envs=process_envs,
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
            SandboxMount(request.local_home, request.hosted_home),
            *_linked_agent_file_mounts(request),
            SandboxMount(
                guest_env_path,
                request.hosted_home / ".env",
                read_only=True,
            ),
            SandboxMount(stage_dir, runtime_dir, read_only=True),
        ]
        container_name = (
            f"toolang-{_container_label(request.agent_name)}-{stage_dir.name}"
        )
        return SandboxPlan(
            sandbox=f"{self.name}:{image}",
            command=("/bin/sh", str(runtime_dir / "start.sh")),
            working_directory=request.hosted_home,
            output=request.output,
            log_path=request.log_path,
            endpoint=request.endpoint,
            envs={
                "TOOLANG_HOST_GATEWAY": DEFAULT_HOST_GATEWAY,
                "TOOLANG_ROOT": str(request.hosted_root),
                "TOOLANG_SANDBOX": f"{self.name}:{image}",
            },
            mounts=tuple(mounts),
            ports=(
                SandboxPort(
                    bind_host=request.bind_host,
                    local_port=request.port,
                    hosted_port=request.port,
                ),
            ),
            meta={
                "container_name": container_name,
                "image": image,
                "stage_dir": str(stage_dir),
                "sandbox_instance_path": str(sandbox_instance_path),
                "startup_events_path": str(startup_events_path),
                "package_source": (
                    request.local_dev_artifact.name
                    if request.local_dev_artifact is not None
                    else "package index"
                ),
            },
        )

    def _guest_environment_sections(
        self,
        request: SandboxRequest,
    ) -> tuple[dict[str, str], dict[str, str]]:
        dotenv_envs = {
            name: value
            for name, value in request.dotenv_envs.items()
            if name not in _CONTROL_ENV_NAMES
        }
        process_envs = {
            name: value
            for name, value in request.envs.items()
            if name not in _CONTROL_ENV_NAMES
            and self._environment_allow_pattern.fullmatch(name) is not None
            and dotenv_envs.get(name) != value
        }
        return dotenv_envs, process_envs

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        container_name = _plan_text(plan, "container_name")
        runtime_id = container_name
        image = _plan_text(plan, "image")
        if len(plan.ports) != 1:
            raise ValueError("docker sandbox requires exactly one published port")
        port = plan.ports[0]
        try:
            runtime_id = await docker_run_detached(
                image=image,
                container_name=container_name,
                workdir=str(plan.working_directory),
                command=list(plan.command),
                mounts=plan.mounts,
                bind_host=port.bind_host,
                published_port=port.local_port,
                hosted_port=port.hosted_port,
                env_values=plan.envs,
                log_path=plan.log_path if plan.output == "file" else None,
            )
            await asyncio.to_thread(
                write_sandbox_instance,
                _plan_text(plan, "sandbox_instance_path"),
                runtime_id,
            )
        except BaseException as exc:
            cleanup_error: BaseException | None = None
            try:
                await asyncio.shield(
                    asyncio.to_thread(docker_remove_container, container_name)
                )
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_exc
            if cleanup_error is None:
                await asyncio.to_thread(
                    remove_stage_directory,
                    _plan_text(plan, "stage_dir"),
                    ignore_errors=True,
                )
                await asyncio.to_thread(
                    remove_startup_events,
                    _plan_text(plan, "startup_events_path"),
                    ignore_errors=True,
                )
            else:
                ref = _docker_ref(
                    plan,
                    runtime_id=runtime_id,
                    container_name=container_name,
                )
                raise SandboxLaunchError(
                    "Could not start docker sandbox and could not remove its "
                    f"workload {container_name}: {cleanup_error}",
                    ref=ref,
                ) from exc
            if isinstance(exc, (OSError, RuntimeError)):
                raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
            raise
        return _docker_ref(
            plan,
            runtime_id=runtime_id,
            container_name=container_name,
        )

    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
    ) -> None:
        """Follow inherited output after the recovery reference is durable."""

        if plan.output == "inherit":
            self._log_followers[ref.runtime_id] = await docker_follow_container_logs(
                ref.runtime_id
            )
        if progress is not None:
            await _observe_startup_events_safely(
                Path(_plan_text(plan, "startup_events_path")),
                progress=progress,
                event_prefix=f"startup:{ref.runtime_id}",
                package_source=_plan_text(plan, "package_source"),
                runtime_id=ref.runtime_id,
            )

    async def running(self, ref: SandboxRef) -> bool:
        return await asyncio.to_thread(docker_container_running, ref.runtime_id)

    async def wait(self, ref: SandboxRef) -> int:
        follower = self._log_followers.get(ref.runtime_id)
        if follower is not None:
            returncode = await follower.wait()
            if returncode != 0:
                raise RuntimeError("docker logs failed")
        return await asyncio.to_thread(docker_wait_container, ref.runtime_id)

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        await asyncio.to_thread(
            docker_stop_container,
            ref.runtime_id,
            force=force,
        )

    async def release(self, ref: SandboxRef) -> None:
        follower = self._log_followers.pop(ref.runtime_id, None)
        if follower is not None:
            await finish_process(follower)
        log_path = ref.meta.get("log_path")
        if isinstance(log_path, str) and log_path:
            with suppress(OSError):
                await asyncio.to_thread(
                    docker_append_container_logs,
                    ref.runtime_id,
                    Path(log_path),
                )
        await asyncio.to_thread(docker_remove_container, ref.runtime_id)
        stage_dir = ref.meta.get("stage_dir")
        if isinstance(stage_dir, str) and stage_dir:
            await asyncio.to_thread(remove_stage_directory, stage_dir)
        startup_events_path = ref.meta.get("startup_events_path")
        if isinstance(startup_events_path, str) and startup_events_path:
            await asyncio.to_thread(remove_startup_events, startup_events_path)


def create_sandbox(config: Mapping[str, Any]) -> Sandbox:
    """Create built-in Docker sandbox."""

    return DockerSandbox(dict(config))


def _docker_ref(
    plan: SandboxPlan,
    *,
    runtime_id: str,
    container_name: str,
) -> SandboxRef:
    return SandboxRef(
        runtime_id=runtime_id,
        endpoint=plan.endpoint,
        runtime_kind="container",
        runtime_name=container_name,
        meta={
            "stage_dir": _plan_text(plan, "stage_dir"),
            "startup_events_path": _plan_text(plan, "startup_events_path"),
            **(
                {"log_path": str(plan.log_path)}
                if plan.output == "file" and plan.log_path is not None
                else {}
            ),
        },
    )


async def _observe_startup_events(
    path: Path,
    *,
    progress: ProgressSink,
    event_prefix: str,
    package_source: str,
    runtime_id: str | None = None,
) -> None:
    processed_lines = 0
    previous: str | None = None
    stopped_observed = False
    while True:
        content = await asyncio.to_thread(_read_startup_events, path)
        lines = content.splitlines()
        complete_count = (
            len(lines) if content.endswith("\n") else max(len(lines) - 1, 0)
        )
        for token in lines[processed_lines:complete_count]:
            if token not in _STARTUP_EVENT_TRANSITIONS.get(previous, frozenset()):
                continue
            previous = token
            phase, label, status = _STARTUP_EVENT_MAP[token]
            detail = _startup_event_detail(
                token,
                package_source=package_source,
            )
            emit_progress(
                progress,
                id=f"{event_prefix}:{phase}",
                phase=f"startup.{phase}",
                label=label,
                status=status,
                detail=detail,
            )
            if token in {
                "install.failed",
                "validate.failed",
                "server.running",
            }:
                return
        processed_lines = complete_count
        if runtime_id is not None and not await asyncio.to_thread(
            docker_container_running,
            runtime_id,
        ):
            if stopped_observed:
                return
            stopped_observed = True
        else:
            stopped_observed = False
        await asyncio.sleep(_STARTUP_EVENT_INTERVAL_SEC)


async def _observe_startup_events_safely(
    path: Path,
    *,
    progress: ProgressSink,
    event_prefix: str,
    package_source: str,
    runtime_id: str,
) -> None:
    with suppress(Exception):
        await _observe_startup_events(
            path,
            progress=progress,
            event_prefix=event_prefix,
            package_source=package_source,
            runtime_id=runtime_id,
        )


def _read_startup_events(path: Path) -> str:
    try:
        expected = os.lstat(path)
    except OSError:
        return ""
    if not stat.S_ISREG(expected.st_mode):
        return ""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            expected.st_dev,
            expected.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            return ""
        content = os.read(descriptor, _STARTUP_EVENT_MAX_BYTES + 1)
    except OSError:
        return ""
    finally:
        with suppress(OSError):
            os.close(descriptor)
    if len(content) > _STARTUP_EVENT_MAX_BYTES:
        return ""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _startup_event_detail(token: str, *, package_source: str) -> str | None:
    if token == "install.running":
        return package_source
    if token == "install.failed":
        return "Toolang installation failed."
    if token == "validate.failed":
        return DOCKER_TOOLANG_COMPATIBILITY_ERROR
    return None


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


def _plan_text(plan: SandboxPlan, key: str) -> str:
    value = plan.meta.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"docker sandbox plan is missing {key}")
    return value
