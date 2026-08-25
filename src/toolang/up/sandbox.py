"""AgentServer sandbox selection and lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
from weakref import WeakKeyDictionary

from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.sandbox import SandboxRef, SandboxRequest
from toolang.common.files import atomic_write_text, file_write_lock
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
from toolang.up.server import ServeSpec, build_serve_argv, resolve_serve

SANDBOX_READY_TIMEOUT_SEC = 30.0
_TASK_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = (
    WeakKeyDictionary()
)
_TASK_LOCKS_MUTEX = threading.Lock()


@dataclass(frozen=True, slots=True)
class SandboxState:
    """Persisted control-side reference to one sandboxed AgentServer workload."""

    sandbox: str
    ref: SandboxRef

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox:
            raise ValueError("sandbox state requires sandbox")
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
    def load(cls, path: Path) -> SandboxState | None:
        with file_write_lock(path.with_suffix(".lock")):
            if not path.is_file():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid sandbox state: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid sandbox state: {path}")
        sandbox = payload.get("sandbox")
        if not isinstance(sandbox, str) or not sandbox.strip():
            raise ValueError(f"sandbox state is missing sandbox: {path}")
        return cls(
            sandbox=sandbox.strip(),
            ref=SandboxRef.from_data(payload.get("ref")),
        )


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Resolved inputs for running one AgentServer in a sandbox."""

    serve: ServeSpec
    sandbox: str
    config: dict[str, object]
    environ: dict[str, str]
    log_path: Path | None = None
    dev_artifact: Path | None = None
    dotenv_envs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SandboxHandle:
    """Process-local handle for one launched sandbox workload."""

    implementation: Sandbox
    state: SandboxState


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
        dotenv_envs=load_setup_dotenvs(layout),
        log_path=log_path,
        dev_artifact=artifact,
    )


async def launch(spec: LaunchSpec) -> SandboxHandle:
    """Launch an AgentServer and return after it becomes ready."""

    lock_path = spec.serve.layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            return await _launch_locked(spec)


async def _launch_locked(spec: LaunchSpec) -> SandboxHandle:
    current = SandboxState.load(spec.serve.layout.sandbox_state)
    if current is not None:
        implementation = load_state_sandbox(
            spec.serve.layout,
            current,
        )
        if await implementation.running(current.ref):
            raise ValueError(f"agent is already running: {spec.serve.layout.name}")
        await implementation.release(current.ref)
        _clear_state(spec.serve.layout, expected=current)

    name, raw_spec = _split_sandbox(spec.sandbox)
    implementation = create_sandbox(name, config=spec.config)
    hosted_root = _hosted_root(
        name,
        local_root=spec.serve.layout.root,
        config=spec.config,
    )
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
                host="0.0.0.0" if name != "host" else spec.serve.host,
            ),
        ),
        working_directory=(spec.serve.layout.home if name == "host" else hosted_home),
        log_path=spec.log_path,
        envs={
            **spec.environ,
            "TOOLANG_ROOT": str(hosted_root),
            "TOOLANG_SANDBOX": spec.sandbox,
        },
        dotenv_envs=spec.dotenv_envs,
        mounts=(
            ()
            if name == "host"
            else prepare_root_mounts(spec.serve.layout.root, hosted_root)
        ),
        local_dev_artifact=spec.dev_artifact,
    )
    plan = implementation.prepare(raw_spec, request)
    ref: SandboxRef | None = None
    state: SandboxState | None = None
    try:
        ref = await implementation.launch(plan)
        state = SandboxState(sandbox=plan.sandbox, ref=ref)
        state.save(spec.serve.layout.sandbox_state)
        await _wait_ready(
            implementation,
            ref,
            timeout_sec=SANDBOX_READY_TIMEOUT_SEC,
        )
        return SandboxHandle(implementation, state)
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
    on_ready: Callable[[SandboxState], None] | None = None,
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

    lock_path = layout.sandbox_state.with_suffix(".lock")
    async with _task_lock(lock_path):
        with file_write_lock(lock_path):
            state = SandboxState.load(layout.sandbox_state)
            if state is None:
                return False
            implementation = load_state_sandbox(layout, state)
            await implementation.stop(state.ref, force=force)
            await implementation.release(state.ref)
            _clear_state(layout, expected=state)
            return True


async def running(layout: AgentLayout) -> bool:
    """Return whether the currently referenced hosted workload is running."""

    state = SandboxState.load(layout.sandbox_state)
    if state is None:
        return False
    return await load_state_sandbox(layout, state).running(state.ref)


def _select_sandbox(
    state: AgentState,
    *,
    explicit: str | None,
) -> tuple[str, dict[str, object]]:
    configs = merge_plugin_configs(
        (state.root_config, state.home_config),
        family="sandbox",
    )
    binding = resolve_sandbox_binding((state.root_config, state.home_config))
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
            "--dev does not apply to the host sandbox; "
            "it already uses the current Toolang environment"
        )

    candidate = raw.expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"development path not found: {candidate}")
    if candidate.is_file():
        if candidate.suffix.casefold() != ".whl":
            raise ValueError(f"development path is not a wheel file: {candidate}")
        if not _is_toolang_wheel(candidate):
            raise ValueError(f"development wheel is not a Toolang wheel: {candidate}")
        return candidate
    if not candidate.is_dir():
        raise ValueError(f"development path is not a file or directory: {candidate}")

    wheels = [
        path
        for path in candidate.rglob("*.whl")
        if path.is_file() and _is_toolang_wheel(path)
    ]
    if not wheels:
        raise FileNotFoundError(f"no Toolang wheel files found in: {candidate}")
    return min(wheels, key=lambda path: (-path.stat().st_mtime_ns, str(path)))


def _is_toolang_wheel(path: Path) -> bool:
    return path.name.casefold().startswith("toolang-")


def _hosted_root(
    name: str,
    *,
    local_root: Path,
    config: Mapping[str, object],
) -> Path:
    if name == "host":
        return local_root
    configured = config.get("root")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip())
    return Path("/root/.toolang")


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


def _task_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = path.resolve(strict=False)
    with _TASK_LOCKS_MUTEX:
        locks = _TASK_LOCKS.setdefault(loop, {})
        return locks.setdefault(key, asyncio.Lock())
