"""Docker sandbox plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import shlex
import subprocess
from typing import Any

from toolang.base.error import ToolangError
from toolang.base.protocols.sandbox import AgentSandbox
from toolang.base.types.sandbox import (
    SandboxMount,
    SandboxPlan,
    SandboxPortForward,
    SandboxSelector,
    SandboxStartRequest,
    SandboxStartResult,
    SandboxState,
)

@dataclass(slots=True)
class DockerSandbox:
    """Sandbox plugin that stages and runs the agent inside Docker."""

    config: dict[str, Any]
    name: str = "docker"
    _default_image: str | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        image = str(self.config.get("image", "")).strip()
        self._default_image = image or None

    def resolve_selector(
        self,
        raw_selector: str | None,
        *,
        configured_selector: SandboxSelector | None = None,
    ) -> SandboxSelector:
        parsed = SandboxSelector.parse(raw_selector) if raw_selector is not None else None
        if parsed is not None and parsed.driver != self.name:
            raise ValueError(f"sandbox selector does not match plugin {self.name}: {raw_selector}")
        if configured_selector is not None and configured_selector.driver != self.name:
            raise ValueError(
                f"configured sandbox selector does not match plugin {self.name}: "
                f"{configured_selector.render()}"
            )
        target = (
            (parsed.target if parsed is not None else None)
            or (configured_selector.target if configured_selector is not None else None)
            or self._default_image
            or "python:3.13-slim"
        )
        return SandboxSelector(driver=self.name, target=target)

    def prepare(self, request: SandboxStartRequest) -> SandboxPlan:
        image = request.selector.target
        if not image:
            raise ValueError("docker sandbox requires a resolved image target")
        stage_dir = request.local_root / ".sandbox" / request.agent_name
        start_path = stage_dir / "start.json"
        script_path = stage_dir / "start.sh"
        runtime_sandbox_dir = request.sandbox_home / ".runtime" / "sandbox"
        stage_dir.mkdir(parents=True, exist_ok=True)

        sandbox_dev_artifact: Path | None = None
        extra_mounts: list[SandboxMount] = []
        if request.local_dev_artifact is not None:
            if request.local_dev_artifact.is_file():
                staged_artifact_path = stage_dir / request.local_dev_artifact.name
                if request.local_dev_artifact.resolve() != staged_artifact_path.resolve():
                    shutil.copy2(request.local_dev_artifact, staged_artifact_path)
                sandbox_dev_artifact = runtime_sandbox_dir / staged_artifact_path.name
            elif _path_is_within(request.local_dev_artifact, request.local_root):
                sandbox_dev_artifact = _translate_to_sandbox_path(
                    request.local_dev_artifact,
                    local_root=request.local_root,
                    sandbox_root=request.sandbox_root,
                )
            else:
                sandbox_dev_artifact = runtime_sandbox_dir / "dev"
                extra_mounts.append(
                    SandboxMount(
                        local_path=request.local_dev_artifact,
                        sandbox_path=sandbox_dev_artifact,
                        read_only=True,
                    )
                )

        start_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agent_name": request.agent_name,
                    "local_root": str(request.local_root),
                    "local_home": str(request.local_home),
                    "sandbox_root": str(request.sandbox_root),
                    "sandbox_home": str(request.sandbox_home),
                    "bind_host": request.bind_host,
                    "endpoint_host": request.endpoint_host,
                    "port": request.port,
                    "endpoint": request.endpoint,
                    "sandbox": {
                        "driver": request.selector.driver,
                        "target": request.selector.target,
                        "image": image,
                    },
                    "dev_artifact": str(sandbox_dev_artifact) if sandbox_dev_artifact is not None else None,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _write_start_script(
            script_path,
            run_command=request.run_command,
            run_shell_command=request.run_shell_command,
            sandbox_dev_artifact=sandbox_dev_artifact,
            runtime_state_path=request.sandbox_home / ".runtime" / "status.json",
        )

        mounts = list(request.mounts)
        mounts.append(SandboxMount(local_path=request.local_home, sandbox_path=request.sandbox_home))
        mounts.append(SandboxMount(local_path=stage_dir, sandbox_path=runtime_sandbox_dir))
        mounts.extend(extra_mounts)

        container_name = f"toolang-{request.agent_name}"
        target_exec_path = runtime_sandbox_dir / "start.sh"
        return SandboxPlan(
            selector=request.selector,
            start_mode="managed",
            sandbox_root=request.sandbox_root,
            sandbox_home=request.sandbox_home,
            sandbox_working_directory=request.sandbox_home,
            run_command=("/bin/sh", "-lc", str(target_exec_path)),
            mounts=tuple(mounts),
            port_forwards=(
                SandboxPortForward(
                    bind_host=request.bind_host,
                    local_port=request.port,
                    sandbox_port=request.port,
                ),
            ),
            env_vars={**dict(request.env_vars), "TOOLANG_ROOT": str(request.sandbox_root)},
            sandbox_dev_artifact=sandbox_dev_artifact,
            state=SandboxState(
                selector=request.selector,
                runtime_id=container_name,
                meta={
                    "image": image,
                    "stage_dir": str(stage_dir),
                    "start_path": str(start_path),
                    "script_path": str(script_path),
                    "endpoint": request.endpoint,
                },
            ),
        )

    def start(self, plan: SandboxPlan) -> SandboxStartResult:
        if plan.state is None or not plan.state.runtime_id:
            raise ValueError("docker sandbox start requires runtime state")
        if not plan.run_command:
            raise ValueError("docker sandbox start requires run_command")
        if len(plan.port_forwards) != 1:
            raise ValueError("docker sandbox start requires exactly one port forward")
        image = str(plan.state.meta.get("image", "")).strip()
        if not image:
            raise ValueError("docker sandbox start requires resolved image")
        port_forward = plan.port_forwards[0]
        docker_remove_container(plan.state.runtime_id)
        try:
            container_id = docker_run_detached(
                image=image,
                container_name=plan.state.runtime_id,
                workdir=str(plan.sandbox_working_directory),
                command=list(plan.run_command),
                mounts=plan.mounts,
                bind_host=port_forward.bind_host,
                published_port=port_forward.local_port,
                env_values=plan.env_vars,
            )
        except RuntimeError as exc:
            raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
        return SandboxStartResult(
            state=plan.state,
            endpoint=plan.state.meta.get("endpoint") if plan.state is not None else None,
            meta={"container_id": container_id},
        )

    def alive(self, state: SandboxState) -> bool:
        if not state.runtime_id:
            return False
        return docker_container_running(state.runtime_id)

    def stop(self, state: SandboxState, *, force: bool = False) -> None:
        del force
        if state.runtime_id:
            docker_remove_container(state.runtime_id)


def create_sandbox(config: Mapping[str, Any]) -> AgentSandbox:
    """Create the built-in Docker sandbox plugin."""

    return DockerSandbox(dict(config))


def _write_start_script(
    path: Path,
    *,
    run_command: tuple[str, ...],
    run_shell_command: str | None,
    sandbox_dev_artifact: Path | None,
    runtime_state_path: Path,
) -> None:
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
        "  if have pip; then",
        '    pip install --disable-pip-version-check --user -U uv >/dev/null 2>&1 || pip install --disable-pip-version-check --user uv',
        "    have uv && return 0",
        "  fi",
        "  if have curl; then",
        '    curl -LsSf https://astral.sh/uv/install.sh | sh',
        "    have uv && return 0",
        "  fi",
        "  if have wget; then",
        '    wget -qO- https://astral.sh/uv/install.sh | sh',
        "    have uv && return 0",
        "  fi",
        '  echo "uv not available; need uv, python with pip, curl, or wget" >&2',
        "  exit 127",
        "}",
        f"RUNTIME_STATE_PATH={shlex.quote(str(runtime_state_path))}",
        "write_runtime_status() {",
        '  [ -n "$PYTHON_BIN" ] || return 0',
        '  STATUS="${1:-}"',
        '  MESSAGE="${2:-}"',
        '  "$PYTHON_BIN" - "$RUNTIME_STATE_PATH" "$STATUS" "$MESSAGE" <<\'PY\'',
        "from __future__ import annotations",
        "import json",
        "from pathlib import Path",
        "import sys",
        "import time",
        "path = Path(sys.argv[1])",
        'status = sys.argv[2] or None',
        'message = sys.argv[3] or None',
        "data: dict[str, object] = {}",
        "if path.exists():",
        "    try:",
        "        loaded = json.loads(path.read_text(encoding='utf-8'))",
        "    except Exception:",
        "        loaded = {}",
        "    if isinstance(loaded, dict):",
        "        data = {str(key): value for key, value in loaded.items()}",
        "if status is not None:",
        "    data['status'] = status",
        "data['message'] = message",
        "data['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())",
        "path.parent.mkdir(parents=True, exist_ok=True)",
        "path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\\n', encoding='utf-8')",
        "PY",
        "}",
        "trap 'write_runtime_status failed \"sandbox start failed\"' EXIT",
        "write_runtime_status starting bootstrapping",
    ]
    if run_command:
        source = str(sandbox_dev_artifact) if sandbox_dev_artifact is not None else "toolang"
        tool_command = (
            run_command
            if run_command[0] in {"too", "toolang"}
            else ("too", *run_command)
        )
        lines.append("ensure_uv")
        lines.append("write_runtime_status starting launching")
        lines.append(
            "exec uv tool run --from "
            + shlex.quote(source)
            + " "
            + shlex.join(tool_command)
        )
    elif run_shell_command is not None:
        lines.append(run_shell_command)
    else:
        raise ValueError("docker sandbox start script requires run_command or run_shell_command")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _translate_to_sandbox_path(path: Path, *, local_root: Path, sandbox_root: Path) -> Path:
    relative = path.resolve().relative_to(local_root.resolve())
    return sandbox_root / relative


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


def docker_container_identity(container_name: str) -> tuple[str, int] | None:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}} {{.State.Pid}}",
                container_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    container_id, pid_text = parts
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if not container_id or pid <= 0:
        return None
    return container_id, pid


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
    workdir: str,
    command: list[str],
    mounts: tuple[SandboxMount, ...],
    bind_host: str,
    published_port: int,
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
        f"{bind_host}:{published_port}:{published_port}",
    ]
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        args.extend(["--volume", f"{mount.local_path}:{mount.sandbox_path}{suffix}"])
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
