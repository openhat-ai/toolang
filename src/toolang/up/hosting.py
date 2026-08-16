"""AgentServer hosting selection and lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
from weakref import WeakKeyDictionary

from toolang.base.protocols.hosting import Hosting
from toolang.base.types.hosting import HostingRef, HostingRequest
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from toolang.plugin.config import (
    merge_sandbox_config,
    parse_sandbox_binding,
)
from toolang.plugin.sandboxes.loading import load_hosting
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher
from toolang.up.mounts import prepare_root_mounts
from toolang.up.server import ServeSpec, build_serve_argv, resolve_serve

HOSTING_READY_TIMEOUT_SEC = 30.0
_TASK_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = (
    WeakKeyDictionary()
)
_TASK_LOCKS_MUTEX = threading.Lock()


@dataclass(frozen=True, slots=True)
class HostingState:
    """Persisted control-side reference to one hosted AgentServer workload."""

    sandbox: str
    ref: HostingRef

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox:
            raise ValueError("hosting state requires sandbox")
        object.__setattr__(self, "sandbox", sandbox)

    def save(self, path: Path) -> None:
        payload = {
            "sandbox": self.sandbox,
            "ref": self.ref.to_data(),
        }
        with file_write_lock(path.with_suffix(".lock")):
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )

    @classmethod
    def load(cls, path: Path) -> HostingState | None:
        with file_write_lock(path.with_suffix(".lock")):
            if not path.is_file():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid hosting state: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid hosting state: {path}")
        sandbox = payload.get("sandbox")
        if not isinstance(sandbox, str) or not sandbox.strip():
            raise ValueError(f"hosting state is missing sandbox: {path}")
        return cls(
            sandbox=sandbox.strip(),
            ref=HostingRef.from_data(payload.get("ref")),
        )


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Resolved inputs for hosting one AgentServer."""

    serve: ServeSpec
    sandbox: str
    config: dict[str, object]
    environ: dict[str, str]
    log_path: Path | None = None
    dev_artifact: Path | None = None


@dataclass(slots=True)
class HostingHandle:
    """Process-local handle for one launched hosting workload."""

    implementation: Hosting
    state: HostingState


async def resolve_launch(
    *,
    layout: AgentLayout,
    environ: Mapping[str, str],
    sandbox: str | None = None,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    resource_filter_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    binding_overrides: Mapping[str, str | None] | None = None,
    limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    file_inboxes: Sequence[Path] | None = None,
    dev: Path | None = None,
    log_spec: str | None = None,
    log_path: Path | None = None,
    temporary_port: bool = False,
) -> LaunchSpec:
    """Resolve source state, server inputs, and one sandbox selection."""

    watcher = StateWatcher(layout)
    state = await watcher.refresh()
    selected, config = _select_sandbox(
        state,
        explicit=sandbox,
        environ=environ,
    )
    serve = resolve_serve(
        layout=layout,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        resource_filter_overrides=resource_filter_overrides,
        binding_overrides=binding_overrides,
        limit_overrides=limit_overrides,
        file_inboxes=file_inboxes,
        log_spec=log_spec,
        temporary_port=temporary_port,
    )
    artifact = dev.expanduser().resolve() if dev is not None else None
    if artifact is not None and not artifact.exists():
        raise FileNotFoundError(f"development artifact not found: {artifact}")
    return LaunchSpec(
        serve=serve,
        sandbox=selected,
        config=config,
        environ=dict(environ),
        log_path=log_path,
        dev_artifact=artifact,
    )


async def launch(spec: LaunchSpec) -> HostingHandle:
    """Launch an AgentServer and return after it becomes ready."""

    lock_path = spec.serve.layout.hosting_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            return await _launch_locked(spec)


async def _launch_locked(spec: LaunchSpec) -> HostingHandle:
    current = HostingState.load(spec.serve.layout.hosting_state)
    if current is not None:
        implementation = _load_state_hosting(current)
        if await implementation.running(current.ref):
            raise ValueError(f"agent is already running: {spec.serve.layout.name}")
        await implementation.release(current.ref)
        _clear_state(spec.serve.layout, expected=current)

    name, raw_spec = _split_sandbox(spec.sandbox)
    implementation = load_hosting(name, config=spec.config)
    hosted_root = _hosted_root(
        name,
        local_root=spec.serve.layout.root,
        config=spec.config,
    )
    hosted_home = hosted_root / "agents" / spec.serve.layout.name
    request = HostingRequest(
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
                host="0.0.0.0" if name != "none" else spec.serve.host,
            ),
        ),
        working_directory=(spec.serve.layout.home if name == "none" else hosted_home),
        log_path=spec.log_path,
        envs={
            **spec.environ,
            "TOOLANG_ROOT": str(hosted_root),
            "TOOLANG_SANDBOX": spec.sandbox,
        },
        mounts=(
            ()
            if name == "none"
            else prepare_root_mounts(spec.serve.layout.root, hosted_root)
        ),
        local_dev_artifact=spec.dev_artifact,
    )
    plan = implementation.prepare(raw_spec, request)
    ref: HostingRef | None = None
    state: HostingState | None = None
    try:
        ref = await implementation.launch(plan)
        state = HostingState(sandbox=plan.sandbox, ref=ref)
        state.save(spec.serve.layout.hosting_state)
        await _wait_ready(
            implementation,
            ref,
            timeout_sec=HOSTING_READY_TIMEOUT_SEC,
        )
        return HostingHandle(implementation, state)
    except BaseException:
        if ref is not None:
            with suppress(BaseException):
                await asyncio.shield(implementation.stop(ref, force=True))
            with suppress(BaseException):
                await asyncio.shield(implementation.release(ref))
        if state is not None:
            _clear_state(spec.serve.layout, expected=state)
        raise


async def run(
    spec: LaunchSpec,
    *,
    on_ready: Callable[[HostingState], None] | None = None,
) -> int:
    """Launch, follow, and release one foreground AgentServer."""

    handle = await launch(spec)
    if on_ready is not None:
        on_ready(handle.state)
    try:
        exit_code = await handle.implementation.wait(handle.state.ref)
    except asyncio.CancelledError:
        await handle.implementation.stop(handle.state.ref)
        await handle.implementation.release(handle.state.ref)
        _clear_state(spec.serve.layout, expected=handle.state)
        raise
    await handle.implementation.release(handle.state.ref)
    _clear_state(spec.serve.layout, expected=handle.state)
    return exit_code


async def stop(layout: AgentLayout, *, force: bool = False) -> bool:
    """Stop and release the currently hosted AgentServer."""

    lock_path = layout.hosting_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            state = HostingState.load(layout.hosting_state)
            if state is None:
                return False
            implementation = _load_state_hosting(state)
            await implementation.stop(state.ref, force=force)
            await implementation.release(state.ref)
            _clear_state(layout, expected=state)
            return True


async def running(layout: AgentLayout) -> bool:
    """Return whether the currently referenced hosted workload is running."""

    state = HostingState.load(layout.hosting_state)
    if state is None:
        return False
    return await _load_state_hosting(state).running(state.ref)


def _select_sandbox(
    state: AgentState,
    *,
    explicit: str | None,
    environ: Mapping[str, str],
) -> tuple[str, dict[str, object]]:
    binding = parse_sandbox_binding(
        merge_sandbox_config(
            (state.root_config, state.home_config),
            environ=environ,
        )
    )
    if explicit is not None:
        selected = explicit.strip()
        if not selected:
            raise ValueError("sandbox selector cannot be empty")
        name, _ = _split_sandbox(selected)
        config = (
            dict(binding.config) if binding is not None and binding.name == name else {}
        )
        return selected, config
    if binding is None:
        return "none", {}
    selected = binding.name
    if binding.spec is not None:
        selected = f"{selected}:{binding.spec}"
    return selected, dict(binding.config)


def _split_sandbox(selector: str) -> tuple[str, str | None]:
    name, separator, spec = selector.partition(":")
    name = name.strip()
    if not name:
        raise ValueError("sandbox selector is missing name")
    return name, spec if separator else None


def _hosted_root(
    name: str,
    *,
    local_root: Path,
    config: Mapping[str, object],
) -> Path:
    if name == "none":
        return local_root
    configured = config.get("root")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip())
    return Path("/root/.toolang")


def _load_state_hosting(state: HostingState) -> Hosting:
    name, _ = _split_sandbox(state.sandbox)
    return load_hosting(name, config={})


async def _wait_ready(
    implementation: Hosting,
    ref: HostingRef,
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


def _clear_state(layout: AgentLayout, *, expected: HostingState) -> None:
    path = layout.hosting_state
    with file_write_lock(path.with_suffix(".lock")):
        current = HostingState.load(path)
        if current == expected:
            path.unlink(missing_ok=True)


def _task_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = path.resolve(strict=False)
    with _TASK_LOCKS_MUTEX:
        locks = _TASK_LOCKS.setdefault(loop, {})
        return locks.setdefault(key, asyncio.Lock())
