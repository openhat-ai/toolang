"""AgentServer sandbox selection and lifecycle orchestration."""

from __future__ import annotations

import asyncio
import errno
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
import os
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4
from weakref import WeakKeyDictionary

from toolang.api.version import AGENT_API_VERSION
from toolang.base.errors import SandboxLaunchError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.model import ModelOverride
from toolang.base.types.progress import (
    ProgressEvent,
    ProgressSink,
    ProgressStage,
    ProgressStatus,
)
from toolang.base.types.sandbox import SandboxOutput, SandboxRef, SandboxRequest
from toolang.common.files import file_lock_path
from toolang.common.progress import emit_progress
from toolang.common.layout import AgentLayout
from toolang.common.config_sources import config_sources
from toolang.plugin.catalogs.models_dev.path import (
    resolve_model_catalog_path,
    MODEL_CATALOG_ENV,
)
from toolang.base.types.sandbox import SandboxMount
from toolang.catalog.agent import LocalAgents
from toolang.plugin.sandboxes.host import process_ref
from toolang.plugin.config import (
    merge_plugin_configs,
    resolve_sandbox_binding,
)
from toolang.plugin.loading import create_sandbox
from toolang.setup.config import (
    load_setup_dotenvs,
)
from toolang.state.config import CAP_ALLOW_FIELDS
from toolang.up.mounts import (
    prepare_source_mounts,
    prepare_workspace_mounts,
)
from toolang.up.instance import (
    SandboxState,
    read_instance,
    read_document,
    update_instance,
    patch_workload,
    process_alive,
    runtime_lock,
)
from toolang.up.observation import RuntimeRegistration
from toolang.up.config import placement_policy
from toolang.up.types import ExecutionDrivers, ServerLifetime

if TYPE_CHECKING:
    from toolang.state.state import AgentState
    from toolang.up.server import ServeSpec

SANDBOX_READY_TIMEOUT_SEC = 30.0
SANDBOX_ATTACH_GRACE_SEC = 0.2
_TASK_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[Path, str | None], asyncio.Lock]
] = WeakKeyDictionary()
_TASK_LOCKS_MUTEX = threading.Lock()


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Resolved inputs for running one AgentServer in a sandbox."""

    serve: ServeSpec
    sandbox: str
    config: dict[str, object]
    environ: dict[str, str]
    progress_id: str = field(
        default_factory=lambda: f"runtime:{uuid4().hex}",
        compare=False,
        repr=False,
    )
    output: SandboxOutput = "inherit"
    log_path: Path | None = None
    dev_artifact: Path | None = None
    dotenv_envs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SandboxHandle:
    """Process-local handle for one launched sandbox workload."""

    implementation: Sandbox
    state: SandboxState
    progress_id: str | None = None


@runtime_checkable
class _OutputDetach(Protocol):
    """Optional process-local output attachment owned by a sandbox."""

    async def detach_output(self, ref: SandboxRef) -> None:
        """Detach output observers without stopping the workload."""


async def resolve_launch(
    *,
    layout: AgentLayout,
    environ: Mapping[str, str],
    sandbox: str | None = None,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    drivers: ExecutionDrivers | None = None,
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    default_overrides: Mapping[str, ModelOverride | str | None] | None = None,
    limit_overrides: Mapping[str, int | float | None] | None = None,
    compact_override: ModelOverride | None = None,
    dev: Path | None = None,
    log_spec: str | None = None,
    output: SandboxOutput = "inherit",
    log_path: Path | None = None,
    temporary_port: bool = False,
    workspace_additions: Mapping[str, str] | None = None,
    workdir: str | None = None,
    toolang_root: Path | None = None,
    lifetime: ServerLifetime = "persistent",
) -> LaunchSpec:
    """Resolve source state, server inputs, and one sandbox selection."""
    from toolang.up.server import resolve_serve

    selected, config = _select_sandbox_configs(
        tuple(source.config for source in config_sources(layout)),
        explicit=sandbox,
        remembered=_remembered_sandbox(layout),
    )
    artifact = resolve_dev_artifact(dev, sandbox=selected)
    serve = resolve_serve(
        layout=layout,
        toolang_root=toolang_root,
        lifetime=lifetime,
        drivers=drivers,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        ceiling_overrides=ceiling_overrides,
        default_overrides=default_overrides,
        limit_overrides=limit_overrides,
        compact_override=compact_override,
        log_spec=log_spec,
        temporary_port=temporary_port,
        workspace_additions=workspace_additions,
        workdir=workdir,
    )
    return LaunchSpec(
        serve=serve,
        sandbox=selected,
        config=config,
        environ=dict(environ),
        output=output,
        dotenv_envs=load_setup_dotenvs(layout),
        log_path=log_path,
        dev_artifact=artifact,
    )


def resolve_selection(
    layout: AgentLayout,
    *,
    explicit: str | None = None,
) -> str:
    """Resolve one explicit or configured sandbox selector without preparing state."""

    selected, _config = _select_sandbox_configs(
        tuple(source.config for source in config_sources(layout)),
        explicit=explicit,
        remembered=_remembered_sandbox(layout),
    )
    return selected


async def launch(
    spec: LaunchSpec,
    *,
    progress: ProgressSink | None = None,
) -> SandboxHandle:
    """Launch an AgentServer and return after it becomes ready."""

    lock_path = file_lock_path(spec.serve.layout.runtime_status)
    async with _task_lock(lock_path, instance=spec.serve.layout.instance):
        with management_lock(spec.serve.layout, progress=progress):
            for attempt in range(3):
                try:
                    return await _launch_locked(spec, progress=progress)
                except (OSError, RuntimeError, ValueError) as exc:
                    bind_failure = "address already in use" in str(
                        exc
                    ).lower() or getattr(exc, "errno", None) in {48, 98}
                    if (
                        not spec.serve.automatic_port
                        or attempt == 2
                        or not bind_failure
                    ):
                        raise
                    from .server import resolve_runtime_port

                    spec = replace(
                        spec,
                        serve=replace(
                            spec.serve,
                            port=resolve_runtime_port(
                                host=spec.serve.host,
                                explicit_port=None,
                                layout=spec.serve.layout,
                                temporary=spec.serve.layout.placement != "resident",
                            ),
                        ),
                    )
            raise RuntimeError("automatic bind attempts exhausted")


@contextmanager
def management_lock(
    layout: AgentLayout,
    *,
    stage: Literal["start", "stop"] = "start",
    progress: ProgressSink | None = None,
) -> Iterator[None]:
    """Show contention while retaining the shared, reentrant lifecycle lock."""
    waiting = False
    item_id = f"runtime:{layout.name}:lock"

    def on_wait() -> None:
        nonlocal waiting
        waiting = True
        emit_progress(
            progress,
            id=item_id,
            kind="runtime",
            stage=stage,
            label="Waiting for agent management...",
            status="running",
            detail=layout.name,
        )

    from toolang.up.instance import management_operation

    with management_operation(layout, stage=stage, on_wait=on_wait):
        if waiting:
            emit_progress(
                progress,
                id=item_id,
                kind="runtime",
                stage=stage,
                label="Agent management available",
                status="ok",
            )
        yield


def prepare_launch_state(
    spec: LaunchSpec, *, progress: ProgressSink | None = None
) -> AgentState:
    """Prepare host-owned State before creating or entering a workload."""
    from toolang.state.prepare import prepare_agent_state

    try:
        state = prepare_agent_state(
            spec.serve.layout,
            workspace_additions=spec.serve.workspace_additions,
            allow_overrides={
                name: value
                for name, value in spec.serve.ceiling_overrides.items()
                if name in CAP_ALLOW_FIELDS
            },
            progress=progress,
        )
    except BaseException as exc:
        emit_progress(
            progress,
            id=f"state:{spec.serve.layout.name}",
            kind="prepare",
            stage="materialize",
            label="Failed to prepare agent state",
            status="failed",
            detail=str(exc),
            subject="state",
            complete=True,
        )
        raise
    emit_progress(
        progress,
        id=f"state:{spec.serve.layout.name}",
        kind="prepare",
        stage="materialize",
        label="Prepared agent state",
        status="ok",
        subject="state",
        complete=True,
    )
    return state


async def _launch_locked(
    spec: LaunchSpec,
    *,
    progress: ProgressSink | None,
) -> SandboxHandle:
    from toolang.up.server import build_serve_argv

    owns_launch = False
    sandbox_name = spec.sandbox.partition(":")[0].replace("_", " ").title()
    prepare_label = f"Preparing {sandbox_name} sandbox..."
    _startup_progress(
        progress,
        spec,
        stage="create",
        label=prepare_label,
        status="running",
        detail=spec.sandbox,
    )
    try:
        await _release_stopped_locked(spec.serve.layout)
        launch_id = uuid4().hex
        spec = replace(spec, serve=replace(spec.serve, launch_id=launch_id))
        owns_launch = True
        _begin_launch(spec)
        if not spec.serve.layout.home.is_dir():
            raise FileNotFoundError(f"agent not found: {spec.serve.layout.name}")

        name, raw_spec = _split_sandbox(spec.sandbox)
        implementation = create_sandbox(name, config=spec.config)
        hosted_place = implementation.runtime_place(spec.serve.layout.place)
        if implementation.location not in {"host", "guest"}:
            raise ValueError(
                f"sandbox {name} returned invalid location: {implementation.location}"
            )
        on_host = implementation.location == "host"
        hosted_home = hosted_place / "agents" / spec.serve.layout.name
        workspace_mounts: tuple[SandboxMount, ...] = ()
        workspace_mapping: dict[str, tuple[str, str]] = {}
        state = await asyncio.to_thread(prepare_launch_state, spec, progress=progress)
        if not on_host:
            workspace_mounts, workspace_mapping = prepare_workspace_mounts(
                spec.serve.layout.home, hosted_home, workspaces=state.workspaces
            )
        catalog_mounts: tuple[SandboxMount, ...] = ()
        catalog_env: dict[str, str] = {}
        if not on_host:
            catalog_path = resolve_model_catalog_path(
                spec.serve.layout, environ=spec.environ
            )
            guest_catalog = hosted_home / ".runtime" / "model-catalog.json"
            catalog_mounts = (
                SandboxMount(
                    local_path=catalog_path, hosted_path=guest_catalog, read_only=True
                ),
            )
            catalog_env[MODEL_CATALOG_ENV] = str(guest_catalog)
        edit_lock_mounts: tuple[SandboxMount, ...] = ()
        guest_edit_lock = None
        if not on_host and spec.serve.layout.source_path is not None:
            local_guard = spec.serve.layout.place_edit_lock
            local_guard.parent.mkdir(parents=True, exist_ok=True)
            local_guard.touch(exist_ok=True)
            guest_edit_lock = hosted_place / ".state" / "root" / "edit.lock"
            edit_lock_mounts = (SandboxMount(local_guard, guest_edit_lock),)
        request = SandboxRequest(
            local_place=spec.serve.layout.place,
            local_home=spec.serve.layout.home,
            hosted_place=hosted_place,
            hosted_home=hosted_home,
            agent_name=spec.serve.layout.name,
            bind_host=spec.serve.host,
            endpoint_host=spec.serve.endpoint_host,
            port=spec.serve.port,
            endpoint=spec.serve.endpoint,
            command=(
                "too",
                *build_serve_argv(
                    spec.serve,
                    place=hosted_place,
                    sandbox=spec.sandbox,
                    location=implementation.location,
                    host=spec.serve.host if on_host else "0.0.0.0",
                    source_edit_lock=guest_edit_lock,
                ),
            ),
            working_directory=spec.serve.layout.home if on_host else hosted_home,
            output=spec.output,
            log_path=spec.log_path,
            envs={
                **spec.environ,
                **catalog_env,
                "TOOLANG_HOST_LAUNCH": launch_id,
                "TOOLANG_ROOT": str(
                    spec.serve.toolang_root
                    or spec.serve.layout.root
                    or spec.serve.layout.place
                ),
                "TOOLANG_SANDBOX": spec.sandbox,
                "TOOLANG_WORKSPACE_MOUNTS": json.dumps(workspace_mapping),
                "TOOLANG_WORKSPACE_LOCATION": "host" if on_host else "guest",
            },
            dotenv_envs=spec.dotenv_envs,
            mounts=(
                ()
                if on_host
                else (
                    *workspace_mounts,
                    *catalog_mounts,
                    *prepare_source_mounts(
                        spec.serve.layout.place,
                        spec.serve.layout.name,
                        hosted_place,
                    ),
                    *edit_lock_mounts,
                )
            ),
            local_dev_artifact=spec.dev_artifact,
            remove_on_exit=spec.serve.lifetime == "automatic",
        )
        intent_reference = getattr(implementation, "intent_reference", None)
        if intent_reference is not None:
            SandboxState(spec.sandbox, intent_reference(request), launch_id).save(
                spec.serve.layout.runtime_status, instance=spec.serve.layout.instance
            )
        plan = implementation.prepare(raw_spec, request)
        prepared_reference = getattr(implementation, "prepared_reference", None)
        if prepared_reference is not None:
            SandboxState(plan.sandbox, prepared_reference(plan), launch_id).save(
                spec.serve.layout.runtime_status, instance=spec.serve.layout.instance
            )
    except BaseException as exc:
        if owns_launch and spec.serve.launch_id is not None:
            patch_workload(
                spec.serve.layout.runtime_status,
                spec.serve.launch_id,
                {
                    "status": "failed",
                    "pid": None,
                    "message": str(exc),
                    "updated_at": _now(),
                },
                instance=spec.serve.layout.instance,
            )
            entry = read_instance(
                spec.serve.layout.runtime_status, spec.serve.layout.instance
            )
            if (
                entry is not None
                and entry["launch_id"] == spec.serve.launch_id
                and not entry["control"].get("ref")
            ):
                from .observation import remove_registration

                remove_registration(spec.serve.layout, spec.serve.launch_id)
        _startup_progress(
            progress,
            spec,
            stage="create",
            label=f"Failed to prepare {sandbox_name} sandbox",
            status="failed",
            detail=str(exc),
        )
        raise
    _startup_progress(
        progress,
        spec,
        stage="create",
        label=f"Prepared {sandbox_name} sandbox",
        status="running",
        detail=plan.sandbox,
    )
    _startup_progress(
        progress,
        spec,
        stage="create",
        label="Creating runtime...",
        status="running",
        detail=plan.sandbox,
    )
    ref: SandboxRef | None = None
    state: SandboxState | None = None
    active_stage: ProgressStage = "create"
    active_label = "Failed to create runtime"
    attach_failure_observed = False
    try:
        try:
            ref = await implementation.launch(plan)
        except SandboxLaunchError as exc:
            ref = exc.ref
            state = SandboxState(sandbox=plan.sandbox, ref=ref, launch_id=launch_id)
            state.save(
                spec.serve.layout.runtime_status, instance=spec.serve.layout.instance
            )
            raise
        state = SandboxState(sandbox=plan.sandbox, ref=ref, launch_id=launch_id)
        state.save(
            spec.serve.layout.runtime_status, instance=spec.serve.layout.instance
        )
        deferred_start_events: list[ProgressEvent] = []

        def forward_attach_progress(event: ProgressEvent) -> None:
            nonlocal attach_failure_observed
            if event.status == "failed":
                attach_failure_observed = True
            if event.kind == "runtime" and event.stage == "start":
                deferred_start_events.append(event)
                return
            if progress is not None:
                with suppress(Exception):
                    progress(event)

        attach_task = asyncio.create_task(
            implementation.attach(
                plan,
                ref,
                progress=(forward_attach_progress if progress is not None else None),
                progress_id=spec.progress_id,
            )
        )
        ready_task = asyncio.create_task(
            _wait_ready(
                implementation,
                ref,
                launch_id=launch_id,
                timeout_sec=SANDBOX_READY_TIMEOUT_SEC,
            )
        )
        try:
            done, _pending = await asyncio.wait(
                (attach_task, ready_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if attach_task in done:
                await attach_task
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(attach_task),
                        timeout=SANDBOX_ATTACH_GRACE_SEC,
                    )
                except TimeoutError:
                    attach_task.cancel()
                    await asyncio.gather(attach_task, return_exceptions=True)
            if (
                ready_task.done()
                and not ready_task.cancelled()
                and ready_task.exception() is not None
            ):
                await ready_task
        except BaseException:
            for task in (attach_task, ready_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(attach_task, ready_task, return_exceptions=True)
            raise
        _startup_progress(
            progress,
            spec,
            stage="create",
            label="Created runtime",
            status="ok",
            detail=ref.runtime_id,
        )
        active_stage = "start"
        active_label = "Failed to connect to the agent API"
        for event in deferred_start_events:
            if progress is not None:
                with suppress(Exception):
                    progress(event)
        _startup_progress(
            progress,
            spec,
            stage="start",
            label=f"Waiting for the agent API at {ref.endpoint}...",
            status="running",
            detail=ref.endpoint,
        )
        await ready_task
        _remember_success(spec, state, selector=spec.sandbox)
        _startup_progress(
            progress,
            spec,
            stage="start",
            label=f"Connected to the agent API at {ref.endpoint}",
            status="ok",
            detail=ref.endpoint,
        )
        if plan.output == "file" and isinstance(implementation, _OutputDetach):
            await implementation.detach_output(ref)
        return SandboxHandle(implementation, state, progress_id=spec.progress_id)
    except BaseException as exc:
        entry = read_instance(
            spec.serve.layout.runtime_status, spec.serve.layout.instance
        )
        bind_error = None
        if entry is not None and entry["launch_id"] == launch_id:
            message = entry["workload"].get("message")
            if isinstance(message, str) and "address already in use" in message.lower():
                bind_error = OSError(errno.EADDRINUSE, message)
        if not attach_failure_observed:
            _startup_progress(
                progress,
                spec,
                stage=active_stage,
                label=active_label,
                status="failed",
                detail=str(exc),
            )
        await asyncio.shield(
            _recover_failed_launch(
                spec.serve.layout,
                implementation,
                ref=ref,
                state=state,
            )
        )
        if bind_error is not None:
            raise bind_error from exc
        raise


async def run(
    spec: LaunchSpec,
    *,
    on_ready: Callable[[SandboxState], None] | None = None,
    progress: ProgressSink | None = None,
    cleanup_progress: ProgressSink | None = None,
) -> int:
    """Launch, follow, and release one foreground AgentServer."""

    handle = await launch(spec, progress=progress)
    try:
        if on_ready is not None:
            on_ready(handle.state)
        exit_code = await handle.implementation.wait(handle.state.ref)
    except asyncio.CancelledError:
        await asyncio.shield(
            _stop_and_release(
                spec.serve.layout,
                handle,
                force=False,
                progress=cleanup_progress,
            )
        )
        raise
    except BaseException:
        await asyncio.shield(
            _stop_and_release(
                spec.serve.layout,
                handle,
                force=True,
                progress=cleanup_progress,
            )
        )
        raise
    await _release(spec.serve.layout, handle, progress=cleanup_progress)
    return exit_code


async def _stop_and_release(
    layout: AgentLayout,
    handle: SandboxHandle,
    *,
    force: bool,
    progress: ProgressSink | None = None,
) -> None:
    progress_id = handle.progress_id or f"runtime:{handle.state.ref.runtime_id}"
    _shutdown_progress(
        progress,
        handle.state,
        progress_id=progress_id,
        stage="stop",
        label="Stopping agent...",
        status="running",
    )
    try:
        await handle.implementation.stop(handle.state.ref, force=force)
    except BaseException as exc:
        _shutdown_progress(
            progress,
            handle.state,
            progress_id=progress_id,
            stage="stop",
            label="Failed to stop agent",
            status="failed",
            detail=str(exc),
        )
        raise
    _shutdown_progress(
        progress,
        handle.state,
        progress_id=progress_id,
        stage="stop",
        label="Stopped agent",
        status="ok",
    )
    await _release(layout, handle, progress=progress)


async def _recover_failed_launch(
    layout: AgentLayout,
    implementation: Sandbox,
    *,
    ref: SandboxRef | None,
    state: SandboxState | None,
) -> None:
    if ref is None:
        return
    with suppress(BaseException):
        await implementation.stop(ref, force=True)
    try:
        if await implementation.running(ref):
            return
        await implementation.release(ref)
    except BaseException:
        return
    if state is not None:
        with suppress(BaseException):
            _clear_state(layout, expected=state)


async def _release(
    layout: AgentLayout,
    handle: SandboxHandle,
    *,
    progress: ProgressSink | None = None,
) -> None:
    progress_id = handle.progress_id or f"runtime:{handle.state.ref.runtime_id}"
    _shutdown_progress(
        progress,
        handle.state,
        progress_id=progress_id,
        stage="destroy",
        label="Removing runtime...",
        status="running",
    )
    try:
        await handle.implementation.release(handle.state.ref)
        _clear_state(layout, expected=handle.state)
    except BaseException as exc:
        _shutdown_progress(
            progress,
            handle.state,
            progress_id=progress_id,
            stage="destroy",
            label="Failed to remove runtime",
            status="failed",
            detail=str(exc),
        )
        raise
    _shutdown_progress(
        progress,
        handle.state,
        progress_id=progress_id,
        stage="destroy",
        label="Removed runtime",
        status="ok",
    )


async def stop(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
    expected_launch_id: str | None = None,
) -> bool:
    """Stop and release the currently hosted AgentServer."""

    lock_path = file_lock_path(layout.runtime_status)
    async with _task_lock(lock_path, instance=layout.instance):
        with management_lock(layout, stage="stop", progress=progress):
            if expected_launch_id is not None:
                entry = read_instance(layout.runtime_status, layout.instance)
                if entry is None or entry["launch_id"] != expected_launch_id:
                    raise ValueError("selected execution owner was replaced")
            _reject_legacy_state(layout)
            await _release_abandoned_temporary(layout)
            state = SandboxState.load(layout.runtime_status, instance=layout.instance)
            if state is None:
                state = await _recover_unreferenced(layout)
                if state is None:
                    return False
            implementation = load_state_sandbox(layout, state)
            handle = SandboxHandle(
                implementation,
                state,
                progress_id=f"runtime:{state.ref.runtime_id}",
            )
            await _stop_and_release(
                layout,
                handle,
                force=force,
                progress=progress,
            )
            return True


async def stop_handle(
    layout: AgentLayout,
    handle: SandboxHandle,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> bool:
    """Stop and release one exact process-owned sandbox workload."""

    lock_path = file_lock_path(layout.runtime_status)
    async with _task_lock(lock_path, instance=layout.instance):
        with management_lock(layout, stage="stop", progress=progress):
            _reject_legacy_state(layout)
            current = SandboxState.load(layout.runtime_status, instance=layout.instance)
            if current is None:
                return False
            if current != handle.state:
                raise ValueError(
                    f"sandbox ownership changed while agent was running: {layout.name}"
                )
            await _stop_and_release(
                layout,
                handle,
                force=force,
                progress=progress,
            )
            return True


async def running(layout: AgentLayout) -> bool:
    """Return whether the currently referenced hosted workload is running."""

    _reject_legacy_state(layout)
    state = SandboxState.load(layout.runtime_status, instance=layout.instance)
    if state is None:
        return False
    return await load_state_sandbox(layout, state).running(state.ref)


def _select_sandbox_configs(
    sources: Sequence[Mapping[str, object]],
    *,
    explicit: str | None,
    remembered: str | None = None,
) -> tuple[str, dict[str, object]]:
    configs = merge_plugin_configs(
        sources,
        family="sandbox",
    )
    binding = resolve_sandbox_binding(sources)
    if explicit is not None:
        selected = explicit.strip()
        if not selected:
            raise ValueError("sandbox selector cannot be empty")
        name, _ = _split_sandbox(selected)
        return selected, dict(configs.get(name, {}))
    if binding is None:
        selected = remembered or "host"
        name, _ = _split_sandbox(selected)
        return selected, dict(configs.get(name, {}))
    selected = binding.name
    if binding.spec is not None:
        selected = f"{selected}:{binding.spec}"
    return selected, dict(configs.get(binding.name, {}))


def _split_sandbox(selector: str) -> tuple[str, str | None]:
    name, separator, spec = selector.partition(":")
    name = name.strip()
    if not name:
        raise ValueError("sandbox selector is missing name")
    return name, spec if separator else None


def resolve_dev_artifact(raw: Path | None, *, sandbox: str) -> Path | None:
    if raw is None:
        return None
    sandbox_name, _ = _split_sandbox(sandbox)
    if sandbox_name == "host":
        raise ValueError(
            "--dev only applies to guest sandboxes; host uses the current Toolang "
            "installation."
        )

    candidate = raw.expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"--dev path not found: {candidate}")
    if candidate.is_file():
        if candidate.suffix.casefold() != ".whl":
            raise ValueError(f"--dev file is not a Toolang wheel: {candidate}")
        if not _is_toolang_wheel(candidate):
            raise ValueError(f"--dev file is not a Toolang wheel: {candidate}")
        return candidate
    if not candidate.is_dir():
        raise ValueError(f"--dev path is not a file or directory: {candidate}")

    wheels = [
        path
        for path in candidate.rglob("*.whl")
        if path.is_file() and _is_toolang_wheel(path)
    ]
    if not wheels:
        raise FileNotFoundError(
            f"No Toolang wheels found under --dev directory: {candidate}"
        )
    return min(wheels, key=lambda path: (-path.stat().st_mtime_ns, str(path)))


def _is_toolang_wheel(path: Path) -> bool:
    return path.name.casefold().startswith("toolang-")


def load_state_sandbox(
    layout: AgentLayout,
    state: SandboxState,
) -> Sandbox:
    """Recreate a state sandbox with its current plugin-owned configuration."""

    name, _ = _split_sandbox(state.sandbox)
    configs = merge_plugin_configs(
        tuple(source.config for source in config_sources(layout)),
        family="sandbox",
    )
    return create_sandbox(name, config=configs.get(name, {}))


def _startup_progress(
    progress: ProgressSink | None,
    spec: LaunchSpec,
    *,
    stage: ProgressStage,
    label: str,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    with suppress(Exception):
        emit_progress(
            progress,
            id=spec.progress_id,
            kind="runtime",
            stage=stage,
            label=label,
            status=status,
            detail=detail,
        )


def _shutdown_progress(
    progress: ProgressSink | None,
    state: SandboxState,
    *,
    progress_id: str,
    stage: ProgressStage,
    label: str,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    with suppress(Exception):
        emit_progress(
            progress,
            id=progress_id,
            kind="runtime",
            stage=stage,
            label=label,
            status=status,
            detail=detail if detail is not None else state.sandbox,
        )


async def _wait_ready(
    implementation: Sandbox,
    ref: SandboxRef,
    *,
    launch_id: str,
    timeout_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    url = f"{ref.endpoint.rstrip('/')}/api/v{AGENT_API_VERSION}/profile"
    while time.monotonic() < deadline:
        if not await implementation.running(ref):
            raise RuntimeError("agent server exited before becoming ready")
        if await asyncio.to_thread(_launch_ready, url, launch_id):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"agent server did not become ready: {ref.endpoint}")


def _launch_ready(url: str, launch_id: str) -> bool:
    """Confirm that the listening API belongs to the workload being launched."""
    try:
        with urlopen(url, timeout=0.5) as response:
            payload = json.load(response)
            runtime = payload.get("runtime") if isinstance(payload, dict) else None
            return (
                response.status == 200
                and isinstance(runtime, dict)
                and runtime.get("launch_id") == launch_id
            )
    except (OSError, ValueError):
        return False


def _health_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _clear_state(layout: AgentLayout, *, expected: SandboxState) -> None:
    cleared = False

    def clear(entry):
        nonlocal cleared
        control = entry["control"]
        if control.get("ref") != expected.ref.to_data():
            return
        control.pop("ref", None)
        cleared = True
        workload = entry["workload"]
        if workload.get("status") in {"running", "preparing", "starting"}:
            workload.update(status="stopped", pid=None, updated_at=_now())

    update_instance(
        layout.runtime_status,
        clear,
        instance=layout.instance,
        launch_id=expected.launch_id,
    )
    from toolang.up.observation import remove_registration

    if cleared:
        remove_registration(layout, expected.launch_id)


async def register_host_server(
    layout: AgentLayout,
    *,
    endpoint: str,
    launch_id: str | None,
    spec: ServeSpec | None = None,
    environ: Mapping[str, str] | None = None,
) -> SandboxRef:
    """Confirm a managed launch, or register a direct server before preparation."""
    from toolang.up.server import resolve_serve

    own = process_ref(
        os.getpid(),
        endpoint,
        group=os.getpgid(os.getpid()) == os.getpid(),
        launch_id=launch_id,
    )
    if launch_id is None:
        with management_lock(layout):
            await _release_stopped_locked(layout)
            launch_id = uuid4().hex
            own = process_ref(
                os.getpid(),
                endpoint,
                group=os.getpgid(os.getpid()) == os.getpid(),
                launch_id=launch_id,
            )
            launch_spec = LaunchSpec(
                resolve_serve(layout=layout, port=int(endpoint.rsplit(":", 1)[1])),
                "host",
                _select_sandbox_configs(
                    tuple(source.config for source in config_sources(layout)),
                    explicit="host",
                )[1],
                dict(environ or {}),
                dotenv_envs=load_setup_dotenvs(layout),
            )
            _begin_launch(
                replace(
                    launch_spec,
                    serve=replace(spec or launch_spec.serve, launch_id=launch_id),
                )
            )
            return await register_host_server(
                layout,
                endpoint=endpoint,
                launch_id=launch_id,
                spec=spec,
                environ=environ,
            )
    # Publish identity before execution; recovery must not depend on parent acknowledgement.
    entry = read_instance(layout.runtime_status, layout.instance)
    if entry is None or entry["launch_id"] != launch_id:
        raise ValueError("host launch identity does not match this server")
    state = SandboxState("host", own, launch_id)
    state.save(layout.runtime_status, instance=layout.instance)
    patch_workload(
        layout.runtime_status,
        launch_id,
        {
            "pid": os.getpid(),
            "process_created": own.meta["created"],
            "status": "starting",
        },
        instance=layout.instance,
    )
    return own


async def remove_agent(layout: AgentLayout) -> None:
    """Keep the final stopped check and home deletion in one management operation."""

    lock_path = file_lock_path(layout.runtime_status)
    async with _task_lock(lock_path, instance=layout.instance):
        with management_lock(layout):
            await _release_stopped_locked(layout)
            with runtime_lock(layout.runtime_status, instance=layout.instance):
                document = read_document(layout.runtime_status) or {}
                if any(
                    key != (layout.instance or "default")
                    and (
                        entry["control"].get("ref") is not None
                        or entry["control"].get("operation") is not None
                        or entry["workload"].get("status")
                        in {"preparing", "starting", "running"}
                    )
                    for key, entry in document.get("instances", {}).items()
                ):
                    raise ValueError(
                        "agent has an active or unreleased execution instance"
                    )
                LocalAgents(layout.place / "agents").remove(layout.name)


async def release_stopped(layout: AgentLayout) -> None:
    """Release any stopped sandbox resources before embedded execution."""

    lock_path = file_lock_path(layout.runtime_status)
    async with _task_lock(lock_path, instance=layout.instance):
        with management_lock(layout):
            await _release_stopped_locked(layout)


async def _release_stopped_locked(layout: AgentLayout) -> None:
    allocated = read_instance(layout.runtime_status)
    if (
        not placement_policy(layout.placement).reuse_executor
        and layout.place_code is not None
        and allocated is not None
        and "startup_contract" not in allocated["control"]
        and allocated["control"].get("ref") is None
    ):
        return
    _reject_legacy_state(layout)
    await _release_abandoned_temporary(layout)
    state = SandboxState.load(layout.runtime_status, instance=layout.instance)
    if state is None:
        _reject_unreferenced_staging(layout)
        state = await _recover_unreferenced(layout)
        if state is None:
            return
    implementation = load_state_sandbox(layout, state)
    if await implementation.running(state.ref):
        raise ValueError(f"agent is already running: {layout.name}")
    await implementation.release(state.ref)
    _clear_state(layout, expected=state)


async def _release_abandoned_temporary(layout: AgentLayout) -> None:
    """Recover caller-owned temporary resources; never collect detached servers."""
    document = read_document(layout.runtime_status) or {}
    for instance, entry in document.get("instances", {}).items():
        if instance in {"default", layout.instance}:
            continue
        owner = entry["control"].get("owner")
        if (
            not isinstance(owner, dict)
            or process_alive(owner.get("pid"), owner.get("created")) is not False
        ):
            continue
        sibling = replace(
            layout,
            instance=instance,
            placement=entry["control"]["placement"],
            project_inputs=entry["control"].get("project_inputs"),
            managed_membership=entry["control"].get("managed_membership"),
        )
        async with _task_lock(
            file_lock_path(sibling.runtime_status), instance=instance
        ):
            with management_lock(sibling, stage="stop"):
                current = read_instance(sibling.runtime_status, instance)
                if (
                    current is None
                    or current["launch_id"] != entry["launch_id"]
                    or current["control"].get("owner") != owner
                ):
                    continue
                state = SandboxState.from_instance(current)
                if state is not None:
                    await _stop_and_release(
                        sibling,
                        SandboxHandle(load_state_sandbox(sibling, state), state),
                        force=True,
                    )
                else:
                    await _recover_unreferenced(sibling)
                    from toolang.up.observation import remove_registration

                    remove_registration(sibling, current["launch_id"])


async def _recover_unreferenced(layout: AgentLayout) -> SandboxState | None:
    """Recover a host created before binding, or retire an uncreated intent."""
    from toolang.up.instance import process_alive

    entry = read_instance(layout.runtime_status, layout.instance)
    if entry is None or entry["workload"].get("status") not in {
        "running",
        "preparing",
        "starting",
    }:
        return None
    control, report = entry["control"], entry["workload"]
    if control.get("sandbox") == "host":
        implementation = create_sandbox("host", config={})
        recover = getattr(implementation, "recover", None)
        recovered = (
            await recover(entry["launch_id"], control["endpoint"])
            if recover is not None
            else None
        )
        if recovered is not None:
            state = SandboxState("host", recovered, entry["launch_id"])
            state.save(layout.runtime_status, instance=layout.instance)
            return state
        dead = (
            report.get("pid") is None
            or process_alive(report.get("pid"), report.get("process_created")) is False
        )
    else:
        # Docker persists its container locator before preparation/creation. An
        # unbound intent without bootstrap artifacts has created no resource.
        dead = (
            str(control.get("sandbox", "")).partition(":")[0] == "docker"
            and report.get("pid") is None
            and not (layout.sandbox_stage / entry["launch_id"]).exists()
        )
    if not dead:
        raise ValueError("active runtime report has no verifiable sandbox reference")
    patch_workload(
        layout.runtime_status,
        entry["launch_id"],
        {"status": "stopped", "pid": None},
        instance=layout.instance,
    )
    from toolang.up.observation import remove_registration

    remove_registration(layout, entry["launch_id"])
    return None


def _reject_legacy_state(layout: AgentLayout) -> None:
    for path in (layout.previous_sandbox_state, layout.legacy_sandbox_state):
        if path.is_file():
            raise ValueError(
                "legacy sandbox state requires migration: stop it with the previous Toolang version before continuing: "
                + str(path)
            )


def _reject_unreferenced_staging(layout: AgentLayout) -> None:
    path = layout.sandbox_stage
    if not path.is_dir() or not any(path.iterdir()):
        return
    document = read_document(layout.runtime_status) or {}
    owned = {
        Path(stage).resolve()
        for entry in document.get("instances", {}).values()
        if isinstance(
            stage := (entry["control"].get("ref") or {})
            .get("meta", {})
            .get("stage_dir"),
            str,
        )
    }
    if all(item.resolve() in owned for item in path.iterdir()):
        return
    raise ValueError(
        "unreferenced sandbox staging requires manual cleanup before continuing: "
        f"{path}; remove any associated workload before deleting these files"
    )


def _task_lock(path: Path, *, instance: str | None = None) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (path.resolve(strict=False), instance)
    with _TASK_LOCKS_MUTEX:
        locks = _TASK_LOCKS.setdefault(loop, {})
        return locks.setdefault(key, asyncio.Lock())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _remembered_sandbox(layout: AgentLayout) -> str | None:
    entry = read_instance(layout.runtime_status, layout.instance)
    value = (entry or {}).get("control", {}).get("last_successful", {}).get("sandbox")
    return value if isinstance(value, str) and value else None


def _begin_launch(spec: LaunchSpec) -> None:
    layout = spec.serve.layout
    assert spec.serve.launch_id is not None

    from .contract import startup_contract
    from .workload import begin_workload

    begin_workload(
        layout,
        launch_id=spec.serve.launch_id,
        toolang_root=spec.serve.toolang_root or layout.root or layout.place,
        sandbox=spec.sandbox,
        endpoint=spec.serve.endpoint,
        drivers=spec.serve.drivers,
        workload={"status": "preparing", "pid": None, "updated_at": _now()},
        startup_contract=startup_contract(spec),
    )
    RuntimeRegistration(
        layout,
        toolang_root=spec.serve.toolang_root or layout.root or layout.place,
        launch_id=spec.serve.launch_id,
    ).__enter__()


def _remember_success(spec: LaunchSpec, state: SandboxState, *, selector: str) -> None:
    layout = spec.serve.layout
    effective_port = urlsplit(state.ref.endpoint).port
    contract = None
    if spec.serve.automatic_port and effective_port is not None:
        from .contract import startup_contract

        contract = startup_contract(
            replace(spec, serve=replace(spec.serve, port=effective_port))
        )

    def remember(entry):
        entry["control"].update(
            last_successful={"endpoint": state.ref.endpoint, "sandbox": selector}
        )
        if contract is not None:
            entry["control"]["startup_contract"] = contract

    update_instance(
        layout.runtime_status,
        remember,
        instance=layout.instance,
        launch_id=state.launch_id,
    )
