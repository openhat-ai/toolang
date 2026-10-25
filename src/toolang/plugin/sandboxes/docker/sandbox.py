"""Sandbox inside Docker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import os
import json
from pathlib import Path
import re
import shutil
import stat
from typing import Any
from uuid import uuid4

from toolang.base.errors import SandboxLaunchError, ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.progress import ProgressSink, ProgressStage, ProgressStatus
from toolang.base.types.sandbox import (
    JsonValue,
    SandboxLocation,
    SandboxMount,
    SandboxPlan,
    SandboxPort,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.progress import emit_progress
from toolang.common.environment import RUNTIME_ENV_NAMES
from toolang.common.place_codes import root_code
from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.up.records import RuntimeReference
from toolang.common.files import atomic_write_text
from toolang.common.version import base_toolang_version

from ._cli import (
    DEFAULT_HOST_GATEWAY,
    docker_append_container_logs,
    docker_container_running,
    docker_follow_container_logs,
    docker_remove_container,
    docker_identity_labels,
    docker_reference_id,
    docker_reference_running,
    docker_run_detached,
    docker_release_controller,
    docker_stop_container,
    docker_wait_container,
    finish_process,
)
from ._guest import (
    GuestLaunchFiles,
    prepare_background_log,
    prepare_diagnostic,
    prepare_stage_directory,
    remove_stage_directory,
    stage_guest_files,
    validate_guest_environment,
    write_guest_env,
)

DEFAULT_IMAGE = "python:3.13-slim"
# Requests already contain the resolved agent environment. Only an explicit
# legacy pattern adds another restriction.
DEFAULT_ENVIRONMENT_ALLOW_PATTERN = r".*"
_CONTROL_ENV_NAMES = RUNTIME_ENV_NAMES
_STARTUP_EVENT_INTERVAL_SEC = 0.05
_STARTUP_EVENT_MAX_BYTES = 1024
_STARTUP_EVENT_MAP: dict[str, tuple[ProgressStage, str, ProgressStatus]] = {
    "uv.use": ("create", "Using uv", "running"),
    "uv.install.running": ("create", "Installing uv...", "running"),
    "uv.install.ok": ("create", "Installed uv", "running"),
    "uv.install.failed": ("create", "Failed to install uv", "failed"),
    "python.use": ("create", "Using Python", "running"),
    "python.install.running": ("create", "Installing Python...", "running"),
    "python.install.ok": ("create", "Installed Python", "running"),
    "python.install.failed": ("create", "Failed to install Python", "failed"),
    "toolang.install.running": ("create", "Installing Toolang", "running"),
    "toolang.install.ok": ("create", "Installed Toolang", "running"),
    "toolang.install.failed": ("create", "Failed to install Toolang", "failed"),
    "toolang.check.running": ("create", "Checking Toolang...", "running"),
    "toolang.check.ok": ("create", "Checked Toolang", "running"),
    "toolang.check.failed": ("create", "Failed to check Toolang", "failed"),
    "server.running": ("start", "Starting agent...", "running"),
}
_STARTUP_EVENT_TRANSITIONS = {
    None: frozenset({"uv.use", "uv.install.running"}),
    "uv.install.running": frozenset({"uv.install.ok", "uv.install.failed"}),
    "uv.use": frozenset({"python.use", "python.install.running"}),
    "uv.install.ok": frozenset({"python.use", "python.install.running"}),
    "python.install.running": frozenset({"python.install.ok", "python.install.failed"}),
    "python.use": frozenset({"toolang.install.running"}),
    "python.install.ok": frozenset({"toolang.install.running"}),
    "toolang.install.running": frozenset(
        {"toolang.install.ok", "toolang.install.failed"}
    ),
    "toolang.install.ok": frozenset({"toolang.check.running"}),
    "toolang.check.running": frozenset({"toolang.check.ok", "toolang.check.failed"}),
    "toolang.check.ok": frozenset({"server.running"}),
}


def _request_mounts(request: SandboxRequest) -> tuple[SandboxMount, ...]:
    """Place nested home mounts after the home mount they override."""

    nested_home = tuple(
        mount
        for mount in request.mounts
        if mount.hosted_path != request.hosted_home
        and mount.hosted_path.is_relative_to(request.hosted_home)
    )
    outer = tuple(mount for mount in request.mounts if mount not in nested_home)
    return (
        *outer,
        SandboxMount(request.local_home, request.hosted_home),
        *nested_home,
    )


def _hosted_model_catalog_mounts(
    request: SandboxRequest,
    dotenv_envs: dict[str, str],
    process_envs: dict[str, str],
) -> tuple[SandboxMount, ...]:
    """Reuse captured read-only catalogs or bind explicit files at one target."""

    mounts: dict[tuple[Path, Path], SandboxMount] = {}
    if MODEL_CATALOG_ENV in process_envs:
        dotenv_envs.pop(MODEL_CATALOG_ENV, None)
    for section in (dotenv_envs, process_envs):
        raw_path = section.get(MODEL_CATALOG_ENV)
        if raw_path is None or not raw_path.strip():
            continue
        captured = next(
            (mount for mount in request.mounts if str(mount.hosted_path) == raw_path),
            None,
        )
        if captured is None:
            for mount in sorted(
                _request_mounts(request),
                key=lambda item: len(item.hosted_path.parts),
                reverse=True,
            ):
                guest_path = Path(raw_path)
                if mount.local_path.is_dir() and guest_path.is_relative_to(
                    mount.hosted_path
                ):
                    local_path = mount.local_path / guest_path.relative_to(
                        mount.hosted_path
                    )
                    captured = SandboxMount(local_path, guest_path)
                    break
        if captured is not None:
            if not captured.local_path.is_file():
                raise ValueError(
                    f"model catalog is not a regular file: {captured.local_path}"
                )
            if captured.read_only:
                continue
            local_path = captured.local_path.resolve(strict=True)
        else:
            local_path = Path(raw_path).expanduser().resolve(strict=True)
        if not local_path.is_file():
            raise ValueError(f"model catalog is not a regular file: {local_path}")
        hosted_path = request.hosted_place / ".inputs" / "catalog.json"
        mount = SandboxMount(local_path, hosted_path, read_only=True)
        existing = tuple(
            item for item in request.mounts if item.hosted_path == hosted_path
        )
        if any(
            not item.read_only or item.local_path.resolve() != local_path
            for item in existing
        ):
            raise ValueError("model catalog target conflicts with an existing mount")
        section[MODEL_CATALOG_ENV] = str(hosted_path)
        if not existing:
            mounts[(local_path, hosted_path)] = mount
    return tuple(mounts.values())


@dataclass(slots=True)
class DockerSandbox:
    """Stage and run the AgentServer as a Docker container's main workload."""

    config: dict[str, Any]
    name: str = "docker"
    location: SandboxLocation = "guest"
    _default_image: str | None = field(init=False, repr=False, default=None)
    _runtime_place: Path = field(init=False, repr=False)
    _environment_allow_pattern: re.Pattern[str] = field(init=False, repr=False)
    _log_followers: dict[str, asyncio.subprocess.Process] = field(
        init=False,
        repr=False,
        default_factory=dict,
    )
    _foreground_runtime_ids: set[str] = field(
        init=False,
        repr=False,
        default_factory=set,
    )

    def __post_init__(self) -> None:
        unknown = self.config.keys() - {"image", "root", "environment_allow_pattern"}
        if unknown:
            raise ValueError(f"unknown docker settings: {', '.join(sorted(unknown))}")
        raw_image = self.config.get("image")
        if raw_image is None:
            self._default_image = None
        elif isinstance(raw_image, str) and raw_image.strip():
            self._default_image = raw_image.strip()
        else:
            raise TypeError("docker image must be a non-empty string")
        raw_place = self.config.get("root")
        if raw_place is None:
            self._runtime_place = Path("/root/.toolang")
        elif isinstance(raw_place, str) and raw_place.strip():
            self._runtime_place = Path(raw_place.strip())
        else:
            raise TypeError("docker agent root must be a non-empty string")
        if not self._runtime_place.is_absolute():
            raise ValueError("docker agent root must be an absolute guest path")
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

    def runtime_place(self, local_place: Path) -> Path:
        """Return the mapped place configured for the Docker guest."""

        del local_place
        return self._runtime_place

    def intent_reference(self, request: SandboxRequest) -> SandboxRef:
        """Persist a generation-bound locator before staging or container creation."""
        launch_id = request.envs.get("TOOLANG_HOST_LAUNCH", "")
        name, identity = _request_identity(request, launch_id)
        files = GuestLaunchFiles.for_launch(request, launch_id)
        return SandboxRef(
            name,
            request.endpoint,
            runtime_kind="container",
            runtime_name=name,
            meta={
                "docker_identity": identity,
                **files.metadata(),
            },
        )

    def prepared_reference(self, plan: SandboxPlan) -> SandboxRef:
        name = _plan_text(plan, "container_name")
        return _docker_ref(plan, runtime_id=name, container_name=name)

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        launch_id = request.envs.get("TOOLANG_HOST_LAUNCH", uuid4().hex)
        container_name, identity = _request_identity(request, launch_id)
        files = GuestLaunchFiles.for_launch(request, launch_id)
        owned_stage = False
        try:
            prepare_stage_directory(files.stage)
            owned_stage = True
            return self._prepare(
                spec,
                request,
                files=files,
                container_name=container_name,
                identity=identity,
            )
        except BaseException:
            if owned_stage:
                remove_stage_directory(
                    files.stage, launch_id=launch_id, ignore_errors=True
                )
                with suppress(OSError):
                    files.diagnostic.unlink(missing_ok=True)
                    files.startup_events.unlink(missing_ok=True)
            raise

    def _prepare(
        self,
        spec: str | None,
        request: SandboxRequest,
        *,
        files: GuestLaunchFiles,
        container_name: str,
        identity: dict[str, JsonValue],
    ) -> SandboxPlan:
        image = _image(spec, self._default_image)
        runtime_dir = request.hosted_runtime or (
            (request.hosted_root or request.hosted_place)
            / ".runtime"
            / "active"
            / request.agent_name
        )
        active_dir = files.stage / "active"
        active_dir.mkdir()
        guest_info = RuntimeReference(
            request.hosted_home / ".runtime" / "status.json",
            str(identity["launch_id"]),
            location="guest",
            sandbox=f"{self.name}:{image}",
            source=request.hosted_source,
            source_ref=request.source_ref,
            address=request.agent_address,
        )
        atomic_write_text(active_dir / "info.json", json.dumps(guest_info.to_data()))
        dotenv_envs, process_envs = self._guest_environment_sections(request)
        model_catalog_mounts = _hosted_model_catalog_mounts(
            request,
            dotenv_envs,
            process_envs,
        )
        validate_guest_environment(dotenv_envs)
        validate_guest_environment(process_envs)
        hosted_log_path = prepare_background_log(request)
        hosted_diagnostic_path = (
            request.hosted_home / ".runtime" / files.diagnostic.name
        )
        prepare_diagnostic(files.diagnostic)
        prepare_diagnostic(files.startup_events)
        stage_guest_files(active_dir)

        hosted_dev_artifact: Path | None = None
        if request.local_dev_artifact is not None:
            artifact = request.local_dev_artifact
            if not artifact.is_file() or artifact.suffix.casefold() != ".whl":
                raise ValueError("docker development artifact must be a wheel file")
            staged = active_dir / artifact.name
            if artifact.resolve() != staged.resolve():
                shutil.copy2(artifact, staged)
            hosted_dev_artifact = runtime_dir / staged.name
        guest_env_path = active_dir / "guest.env"
        hosted_env_path = (
            request.environment_path
            or self._runtime_place / f".{request.agent_name}.env"
        )
        write_guest_env(
            guest_env_path,
            dotenv_envs=dotenv_envs,
            process_envs=process_envs,
        )
        mounts = [
            *_request_mounts(request),
            *model_catalog_mounts,
            SandboxMount(
                guest_env_path,
                hosted_env_path,
                read_only=True,
            ),
            SandboxMount(active_dir, runtime_dir, read_only=True),
        ]
        package_source = (
            str(hosted_dev_artifact)
            if hosted_dev_artifact is not None
            else f"toolang=={base_toolang_version()}"
        )
        command = list(request.command)
        if "--sandbox" in command:
            command[command.index("--sandbox") + 1] = f"{self.name}:{image}"
        if "_serve" in command:
            command.extend(["--environment-file", str(runtime_dir / "guest.env")])
        return SandboxPlan(
            sandbox=f"{self.name}:{image}",
            command=(
                "/bin/sh",
                str(runtime_dir / "guest.sh"),
                str(hosted_diagnostic_path),
                str(files.diagnostic),
                str(request.hosted_home / ".runtime" / files.startup_events.name),
                package_source,
                str(hosted_log_path) if hosted_log_path is not None else "-",
                "--",
                *command,
            ),
            working_directory=request.hosted_home,
            output=request.output,
            log_path=request.log_path,
            endpoint=request.endpoint,
            envs={
                "TOOLANG_HOST_GATEWAY": DEFAULT_HOST_GATEWAY,
                "TOOLANG_ROOT": str(hosted_env_path.parent),
                "TOOLANG_SANDBOX": f"{self.name}:{image}",
                "TOOLANG_WORKSPACE_LOCATION": "guest",
                **(
                    {
                        "TOOLANG_FOREGROUND_OWNER_FILE": str(
                            request.hosted_home
                            / ".runtime"
                            / f"owner-{identity['launch_id']}"
                        )
                    }
                    if request.foreground_owner
                    else {}
                ),
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
                "remove_on_exit": request.remove_on_exit,
                "foreground_owner": request.foreground_owner,
                "container_name": container_name,
                "docker_identity": identity,
                "image": image,
                **files.metadata(),
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
        labels = docker_identity_labels(plan.meta.get("docker_identity"))
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
                log_path=Path(_plan_text(plan, "stage_dir")) / "controller.log"
                if plan.meta.get("foreground_owner")
                else None,
                labels=labels,
                **({"foreground": True} if plan.meta.get("foreground_owner") else {}),
                **({"remove_on_exit": True} if plan.meta.get("remove_on_exit") else {}),
            )
        except BaseException as exc:
            cleanup_error: BaseException | None = None
            ref = _docker_ref(
                plan, runtime_id=runtime_id, container_name=container_name
            )

            async def remove_owned_container() -> None:
                owned_id = await asyncio.to_thread(docker_reference_id, ref)
                if owned_id is not None:
                    if plan.meta.get("foreground_owner"):
                        await asyncio.to_thread(
                            docker_stop_container, owned_id, force=False
                        )
                    await asyncio.to_thread(docker_remove_container, owned_id)
                await asyncio.to_thread(docker_release_controller, container_name)

            try:
                await asyncio.shield(remove_owned_container())
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_exc
            if cleanup_error is not None:
                raise SandboxLaunchError(
                    "Could not start docker sandbox and could not remove its "
                    f"workload {container_name}: {cleanup_error}",
                    ref=ref,
                ) from exc
            await asyncio.to_thread(
                remove_stage_directory,
                _plan_text(plan, "stage_dir"),
                launch_id=str(plan.meta["docker_identity"]["launch_id"]),
                ignore_errors=True,
            )
            await asyncio.to_thread(
                Path(_plan_text(plan, "startup_events_path")).unlink, missing_ok=True
            )
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
        """Observe guest startup while retaining foreground output ownership."""

        if plan.output == "inherit":
            self._foreground_runtime_ids.add(ref.runtime_id)
        if progress is not None:
            runtime_id = await asyncio.to_thread(docker_reference_id, ref)
            # Automatic removal can precede attachment; drain host-side events too.
            await _observe_startup_events_safely(
                Path(_plan_text(plan, "startup_events_path")),
                progress=progress,
                progress_id=progress_id or f"runtime:{ref.runtime_id}",
                package_source=_plan_text(plan, "package_source"),
                runtime_id=runtime_id,
                runtime_stopped=runtime_id is None,
            )

    async def detach_output(self, ref: SandboxRef) -> None:
        """Stop following output without stopping the container workload."""

        follower = self._log_followers.pop(ref.runtime_id, None)
        if follower is not None:
            await finish_process(follower)

    async def running(self, ref: SandboxRef) -> bool:
        return await asyncio.to_thread(docker_reference_running, ref)

    async def wait(self, ref: SandboxRef) -> int:
        """Follow foreground logs after startup progress releases the terminal."""

        runtime_id = await asyncio.to_thread(docker_reference_id, ref)
        if runtime_id is None:
            raise RuntimeError("docker container is missing")
        if ref.runtime_id in self._foreground_runtime_ids:
            follower = await docker_follow_container_logs(runtime_id)
            self._log_followers[ref.runtime_id] = follower
            try:
                returncode = await follower.wait()
            except asyncio.CancelledError:
                with suppress(ProcessLookupError):
                    follower.terminate()
                await asyncio.shield(finish_process(follower))
                raise
            if returncode != 0:
                raise RuntimeError("docker logs failed")
        return await asyncio.to_thread(docker_wait_container, runtime_id)

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        runtime_id = await asyncio.to_thread(docker_reference_id, ref)
        if runtime_id is None:
            return
        await asyncio.to_thread(
            docker_stop_container,
            runtime_id,
            force=force,
        )

    async def release(self, ref: SandboxRef) -> None:
        runtime_id = await asyncio.to_thread(docker_reference_id, ref)
        self._foreground_runtime_ids.discard(ref.runtime_id)
        await self.detach_output(ref)
        log_path = ref.meta.get("log_path")
        if runtime_id is not None and isinstance(log_path, str) and log_path:
            with suppress(OSError):
                await asyncio.to_thread(
                    docker_append_container_logs,
                    runtime_id,
                    Path(log_path),
                )
        if runtime_id is not None:
            await asyncio.to_thread(docker_remove_container, runtime_id)
        await asyncio.to_thread(docker_release_controller, ref.runtime_id)
        stage_dir = ref.meta.get("stage_dir")
        if isinstance(stage_dir, str) and stage_dir:
            identity = ref.meta.get("docker_identity")
            generation = (
                identity.get("launch_id") if isinstance(identity, dict) else None
            )
            await asyncio.to_thread(
                remove_stage_directory, stage_dir, launch_id=generation
            )
        startup_events_path = ref.meta.get("startup_events_path")
        if isinstance(startup_events_path, str) and startup_events_path:
            await asyncio.to_thread(Path(startup_events_path).unlink, missing_ok=True)


def create_sandbox(config: Mapping[str, Any]) -> Sandbox:
    """Create built-in Docker sandbox."""

    return DockerSandbox(dict(config))


def _docker_ref(
    plan: SandboxPlan,
    *,
    runtime_id: str,
    container_name: str,
) -> SandboxRef:
    docker_identity_labels(plan.meta.get("docker_identity"))
    return SandboxRef(
        runtime_id=runtime_id,
        endpoint=plan.endpoint,
        runtime_kind="container",
        runtime_name=container_name,
        meta={
            "foreground_owner": bool(plan.meta.get("foreground_owner")),
            "docker_identity": plan.meta["docker_identity"],
            "stage_dir": _plan_text(plan, "stage_dir"),
            "diagnostic_path": _plan_text(plan, "diagnostic_path"),
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
    progress_id: str,
    package_source: str,
    runtime_id: str | None = None,
    runtime_stopped: bool = False,
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
            stage, label, status = _STARTUP_EVENT_MAP[token]
            if token.startswith("toolang.install."):
                source = (
                    "the package index"
                    if package_source == "package index"
                    else package_source
                )
                label = (
                    f"{label} from {source}"
                    f"{'...' if token == 'toolang.install.running' else ''}"
                )
            detail = _startup_event_detail(
                token,
                package_source=package_source,
            )
            emit_progress(
                progress,
                id=progress_id,
                kind="runtime",
                stage=stage,
                label=label,
                status=status,
                detail=detail,
            )
            if token in {
                "uv.install.failed",
                "python.install.failed",
                "toolang.install.failed",
                "toolang.check.failed",
                "server.running",
            }:
                return
        processed_lines = complete_count
        if runtime_stopped or (
            runtime_id is not None
            and not await asyncio.to_thread(docker_container_running, runtime_id)
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
    progress_id: str,
    package_source: str,
    runtime_id: str | None,
    runtime_stopped: bool = False,
) -> None:
    with suppress(Exception):
        await _observe_startup_events(
            path,
            progress=progress,
            progress_id=progress_id,
            package_source=package_source,
            runtime_id=runtime_id,
            runtime_stopped=runtime_stopped,
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
    if token == "toolang.install.running":
        return package_source
    if token == "toolang.install.failed":
        return "Toolang installation failed."
    if token == "toolang.check.failed":
        return "The installed Toolang package cannot start the required AgentServer."
    return None


def _image(spec: str | None, configured: str | None) -> str:
    if spec is not None:
        image = spec.strip()
        if not image:
            raise ValueError("docker sandbox spec cannot be empty")
        return image
    return configured or DEFAULT_IMAGE


def _request_identity(
    request: SandboxRequest, launch_id: str
) -> tuple[str, dict[str, JsonValue]]:
    """Validate identity and own the single Docker container naming format."""
    root = request.toolang_root
    address = request.agent_address
    if not isinstance(root, Path) or not isinstance(address, str):
        raise ValueError("docker requires an explicit agent address and Toolang root")
    identity: dict[str, JsonValue] = {
        "version": 1,
        "root": str(root),
        "address": address,
        "launch_id": launch_id,
    }
    docker_identity_labels(identity)
    if address.partition("@")[0] != request.agent_name:
        raise ValueError("docker agent address does not match agent name")
    return f"toolang-{root_code(str(root))}-{address.replace('@', '-')}", identity


def _plan_text(plan: SandboxPlan, key: str) -> str:
    value = plan.meta.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"docker sandbox plan is missing {key}")
    return value
