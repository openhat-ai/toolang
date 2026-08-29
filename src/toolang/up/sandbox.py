"""AgentServer sandbox selection and lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import threading
import time
from typing import Protocol, runtime_checkable
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4
from weakref import WeakKeyDictionary

from toolang.base.errors import SandboxLaunchError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.progress import (
    ProgressEvent,
    ProgressSink,
    ProgressStage,
    ProgressStatus,
)
from toolang.base.types.sandbox import SandboxOutput, SandboxRef, SandboxRequest
from toolang.common.files import file_write_lock
from toolang.common.progress import emit_progress
from toolang.common.layout import AgentLayout
from toolang.plugin.config import (
    merge_plugin_configs,
    resolve_sandbox_binding,
)
from toolang.plugin.sandboxes.loading import create_sandbox
from toolang.setup.config import (
    load_agent_config,
    load_setup_config,
    load_setup_dotenvs,
)
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher
from toolang.up.mounts import prepare_root_mounts
from toolang.up.records import SandboxState
from toolang.up.server import ServeSpec, build_serve_argv, resolve_serve

SANDBOX_READY_TIMEOUT_SEC = 30.0
SANDBOX_ATTACH_GRACE_SEC = 0.2
_TASK_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = (
    WeakKeyDictionary()
)
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
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    binding_overrides: Mapping[str, str | None] | None = None,
    limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    file_inboxes: Sequence[Path] | None = None,
    dev: Path | None = None,
    log_spec: str | None = None,
    output: SandboxOutput = "inherit",
    log_path: Path | None = None,
    temporary_port: bool = False,
) -> LaunchSpec:
    """Resolve source state, server inputs, and one sandbox selection."""

    watcher = StateWatcher(layout)
    state = await watcher.refresh()
    selected, config = _select_sandbox(
        state,
        explicit=sandbox,
    )
    artifact = _resolve_dev_artifact(dev, sandbox=selected)
    serve = resolve_serve(
        layout=layout,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        ceiling_overrides=ceiling_overrides,
        binding_overrides=binding_overrides,
        limit_overrides=limit_overrides,
        file_inboxes=file_inboxes,
        log_spec=log_spec,
        temporary_port=temporary_port,
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
        (load_setup_config(layout), load_agent_config(layout)),
        explicit=explicit,
    )
    return selected


async def launch(
    spec: LaunchSpec,
    *,
    progress: ProgressSink | None = None,
) -> SandboxHandle:
    """Launch an AgentServer and return after it becomes ready."""

    lock_path = spec.serve.layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            return await _launch_locked(spec, progress=progress)


async def _launch_locked(
    spec: LaunchSpec,
    *,
    progress: ProgressSink | None,
) -> SandboxHandle:
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

        name, raw_spec = _split_sandbox(spec.sandbox)
        implementation = create_sandbox(name, config=spec.config)
        hosted_root = implementation.runtime_root(spec.serve.layout.root)
        if implementation.location not in {"host", "guest"}:
            raise ValueError(
                f"sandbox {name} returned invalid location: {implementation.location}"
            )
        on_host = implementation.location == "host"
        hosted_home = hosted_root / "agents" / spec.serve.layout.name
        request = SandboxRequest(
            local_root=spec.serve.layout.root,
            local_home=spec.serve.layout.home,
            hosted_root=hosted_root,
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
                    root=hosted_root,
                    host=spec.serve.host if on_host else "0.0.0.0",
                ),
            ),
            working_directory=spec.serve.layout.home if on_host else hosted_home,
            output=spec.output,
            log_path=spec.log_path,
            envs={
                **spec.environ,
                "TOOLANG_ROOT": str(hosted_root),
                "TOOLANG_SANDBOX": spec.sandbox,
            },
            dotenv_envs=spec.dotenv_envs,
            mounts=(
                ()
                if on_host
                else prepare_root_mounts(spec.serve.layout.root, hosted_root)
            ),
            local_dev_artifact=spec.dev_artifact,
        )
        plan = implementation.prepare(raw_spec, request)
    except BaseException as exc:
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
            state = SandboxState(sandbox=plan.sandbox, ref=ref)
            state.save(spec.serve.layout.sandbox_state)
            raise
        state = SandboxState(sandbox=plan.sandbox, ref=ref)
        state.save(spec.serve.layout.sandbox_state)
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
) -> bool:
    """Stop and release the currently hosted AgentServer."""

    lock_path = layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            _reject_legacy_state(layout)
            state = SandboxState.load(layout.sandbox_state)
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

    lock_path = layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            _reject_legacy_state(layout)
            current = SandboxState.load(layout.sandbox_state)
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
    state = SandboxState.load(layout.sandbox_state)
    if state is None:
        return False
    return await load_state_sandbox(layout, state).running(state.ref)


def _select_sandbox(
    state: AgentState,
    *,
    explicit: str | None,
) -> tuple[str, dict[str, object]]:
    return _select_sandbox_configs(
        (state.root_config, state.home_config),
        explicit=explicit,
    )


def _select_sandbox_configs(
    sources: Sequence[Mapping[str, object]],
    *,
    explicit: str | None,
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
        return "host", dict(configs.get("host", {}))
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


def _resolve_dev_artifact(raw: Path | None, *, sandbox: str) -> Path | None:
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
        (load_setup_config(layout), load_agent_config(layout)),
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
    timeout_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    url = f"{ref.endpoint.rstrip('/')}/healthz"
    while time.monotonic() < deadline:
        if not await implementation.running(ref):
            raise RuntimeError("agent server exited before becoming ready")
        if await asyncio.to_thread(_health_ready, url):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"agent server did not become ready: {ref.endpoint}")


def _health_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _clear_state(layout: AgentLayout, *, expected: SandboxState) -> None:
    path = layout.sandbox_state
    with file_write_lock(path.with_suffix(".lock")):
        current = SandboxState.load(path)
        if current == expected:
            path.unlink(missing_ok=True)


async def release_for_removal(layout: AgentLayout) -> None:
    """Release stopped sandbox resources before removing an agent home."""

    await release_stopped(layout)


async def release_stopped(layout: AgentLayout) -> None:
    """Release any stopped sandbox resources before embedded execution."""

    lock_path = layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            await _release_stopped_locked(layout)


async def _release_stopped_locked(layout: AgentLayout) -> None:
    _reject_legacy_state(layout)
    state = SandboxState.load(layout.sandbox_state)
    if state is None:
        _reject_unreferenced_staging(layout)
        return
    implementation = load_state_sandbox(layout, state)
    if await implementation.running(state.ref):
        raise ValueError(f"agent is already running: {layout.name}")
    await implementation.release(state.ref)
    _reject_unreferenced_staging(layout)
    _clear_state(layout, expected=state)


def _reject_legacy_state(layout: AgentLayout) -> None:
    path = layout.legacy_sandbox_state
    if not path.is_file():
        return
    raise ValueError(
        "legacy guest-writable sandbox state requires manual cleanup before "
        f"continuing: {path}; stop it with the previous Toolang version or "
        "remove the workload manually, then delete this file"
    )


def _reject_unreferenced_staging(layout: AgentLayout) -> None:
    path = layout.sandbox_stage
    if not path.is_dir() or not any(path.iterdir()):
        return
    raise ValueError(
        "unreferenced sandbox staging requires manual cleanup before continuing: "
        f"{path}; remove any associated workload before deleting these files"
    )


def _task_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = path.resolve(strict=False)
    with _TASK_LOCKS_MUTEX:
        locks = _TASK_LOCKS.setdefault(loop, {})
        return locks.setdefault(key, asyncio.Lock())
