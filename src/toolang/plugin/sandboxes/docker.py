"""Sandbox inside Docker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from toolang.base.errors import SandboxLaunchError, ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.progress import ProgressSink
from toolang.base.types.sandbox import (
    SandboxLocation,
    SandboxMount,
    SandboxPlan,
    SandboxPort,
    SandboxRef,
    SandboxRequest,
)

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
    prepare_background_log,
    prepare_diagnostic,
    prepare_stage_directory,
    remove_stage_directory,
    stage_guest_files,
    validate_guest_environment,
    write_guest_env,
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
        "HOSTNAME",
    }
)


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
        diagnostic_path = (
            request.local_home / ".runtime" / f"docker-guest-{stage_dir.name}.log"
        )
        try:
            return self._prepare(
                spec,
                request,
                stage_dir=stage_dir,
                diagnostic_path=diagnostic_path,
            )
        except BaseException:
            remove_stage_directory(stage_dir, ignore_errors=True)
            with suppress(OSError):
                diagnostic_path.unlink(missing_ok=True)
            raise

    def _prepare(
        self,
        spec: str | None,
        request: SandboxRequest,
        *,
        stage_dir: Path,
        diagnostic_path: Path,
    ) -> SandboxPlan:
        image = _image(spec, self._default_image)
        runtime_dir = request.hosted_home / ".runtime" / "sandbox"
        dotenv_envs, process_envs = self._guest_environment_sections(request)
        validate_guest_environment(dotenv_envs)
        validate_guest_environment(process_envs)
        hosted_log_path = prepare_background_log(request)
        prepare_stage_directory(stage_dir)
        hosted_diagnostic_path = request.hosted_home / ".runtime" / diagnostic_path.name
        prepare_diagnostic(diagnostic_path)
        stage_guest_files(stage_dir)

        hosted_dev_artifact: Path | None = None
        if request.local_dev_artifact is not None:
            artifact = request.local_dev_artifact
            if not artifact.is_file() or artifact.suffix.casefold() != ".whl":
                raise ValueError("docker development artifact must be a wheel file")
            staged = stage_dir / artifact.name
            if artifact.resolve() != staged.resolve():
                shutil.copy2(artifact, staged)
            hosted_dev_artifact = runtime_dir / staged.name
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
        package_source = (
            str(hosted_dev_artifact) if hosted_dev_artifact is not None else "toolang"
        )
        return SandboxPlan(
            sandbox=f"{self.name}:{image}",
            command=(
                "/bin/sh",
                str(runtime_dir / "docker_guest.sh"),
                str(runtime_dir / "docker_guest.py"),
                str(request.hosted_home / ".env"),
                str(hosted_diagnostic_path),
                str(diagnostic_path),
                package_source,
                str(hosted_log_path) if hosted_log_path is not None else "-",
                "--",
                *request.command,
            ),
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
                "diagnostic_path": str(diagnostic_path),
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
                log_path=None,
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
        progress_id: str | None = None,
    ) -> None:
        """Follow the container stream from bootstrap through the workload."""

        del progress_id
        if plan.output != "inherit" and progress is None:
            return
        self._log_followers[ref.runtime_id] = await docker_follow_container_logs(
            ref.runtime_id
        )

    async def detach_output(self, ref: SandboxRef) -> None:
        """Stop following output without stopping the container workload."""

        follower = self._log_followers.pop(ref.runtime_id, None)
        if follower is not None:
            await finish_process(follower)

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
        await self.detach_output(ref)
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
            "diagnostic_path": _plan_text(plan, "diagnostic_path"),
            **(
                {"log_path": str(plan.log_path)}
                if plan.output == "file" and plan.log_path is not None
                else {}
            ),
        },
    )


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
