"""Sandbox inside Docker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any
from uuid import uuid4

from toolang.base.errors import ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.sandbox import (
    SandboxMount,
    SandboxPlan,
    SandboxPort,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.files import atomic_write_text

DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_HOST_GATEWAY = "host.docker.internal"
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
    {"TOOLANG_HOST_GATEWAY", "TOOLANG_ROOT", "TOOLANG_SANDBOX"}
)


@dataclass(slots=True)
class DockerSandbox:
    """Stage and run the AgentServer as a Docker container's main workload."""

    config: dict[str, Any]
    name: str = "docker"
    _default_image: str | None = field(init=False, repr=False, default=None)
    _environment_allow_pattern: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        image = str(self.config.get("image", "")).strip()
        self._default_image = image or None
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

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        stage_dir = request.local_root / ".sandbox" / request.agent_name
        try:
            return self._prepare(spec, request)
        except BaseException:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def _prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        image = _image(spec, self._default_image)
        stage_dir = request.local_root / ".sandbox" / request.agent_name
        runtime_dir = request.hosted_home / ".runtime" / "sandbox"
        dotenv_envs, process_envs = self._guest_environment_sections(request)
        _validate_guest_environment(dotenv_envs)
        _validate_guest_environment(process_envs)
        stage_dir.mkdir(parents=True, exist_ok=True)

        hosted_dev_artifact: Path | None = None
        if request.local_dev_artifact is not None:
            artifact = request.local_dev_artifact
            if not artifact.is_file() or artifact.suffix.casefold() != ".whl":
                raise ValueError("docker development artifact must be a wheel file")
            staged = stage_dir / artifact.name
            if artifact.resolve() != staged.resolve():
                shutil.copy2(artifact, staged)
            hosted_dev_artifact = runtime_dir / staged.name

        agent_script_path = stage_dir / "agent.sh"
        _write_agent_script(
            agent_script_path,
            command=request.command,
            hosted_dev_artifact=hosted_dev_artifact,
        )
        bootstrap_path = stage_dir / "bootstrap.py"
        _write_bootstrap(bootstrap_path)
        script_path = stage_dir / "start.sh"
        _write_start_script(
            script_path,
            runtime_dir=runtime_dir,
            guest_env_path=request.hosted_home / ".env",
        )
        guest_env_path = stage_dir / "guest.env"
        _write_guest_env(
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
            SandboxMount(
                guest_env_path,
                request.hosted_home / ".env",
                read_only=True,
            ),
            SandboxMount(stage_dir, runtime_dir, read_only=True),
        ]
        container_name = (
            f"toolang-{_container_label(request.agent_name)}-{uuid4().hex[:8]}"
        )
        return SandboxPlan(
            sandbox=f"{self.name}:{image}",
            command=("/bin/sh", str(runtime_dir / "start.sh")),
            working_directory=request.hosted_home,
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
        image = _plan_text(plan, "image")
        if len(plan.ports) != 1:
            raise ValueError("docker sandbox requires exactly one published port")
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
        except BaseException as exc:
            await asyncio.to_thread(
                shutil.rmtree,
                _plan_text(plan, "stage_dir"),
                True,
            )
            if isinstance(exc, (OSError, RuntimeError)):
                raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
            raise
        return SandboxRef(
            runtime_id=container_name,
            endpoint=plan.endpoint,
            meta={
                "container_id": container_id,
                "image": image,
                "stage_dir": _plan_text(plan, "stage_dir"),
                "follow_logs": plan.log_path is None,
            },
        )

    async def running(self, ref: SandboxRef) -> bool:
        return await asyncio.to_thread(docker_container_running, ref.runtime_id)

    async def wait(self, ref: SandboxRef) -> int:
        if ref.meta.get("follow_logs") is True:
            await asyncio.to_thread(docker_follow_container_logs, ref.runtime_id)
        return await asyncio.to_thread(docker_wait_container, ref.runtime_id)

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        await asyncio.to_thread(
            docker_stop_container,
            ref.runtime_id,
            force=force,
        )

    async def release(self, ref: SandboxRef) -> None:
        await asyncio.to_thread(docker_remove_container, ref.runtime_id)
        stage_dir = ref.meta.get("stage_dir")
        if isinstance(stage_dir, str) and stage_dir:
            await asyncio.to_thread(shutil.rmtree, stage_dir, True)


def create_sandbox(config: Mapping[str, Any]) -> Sandbox:
    """Create built-in Docker sandbox."""

    return DockerSandbox(dict(config))


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


def _write_agent_script(
    path: Path,
    *,
    command: tuple[str, ...],
    hosted_dev_artifact: Path | None,
) -> None:
    if not command:
        raise ValueError("docker sandbox requires a command")
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


def _write_start_script(
    path: Path,
    *,
    runtime_dir: Path,
    guest_env_path: Path,
) -> None:
    bootstrap = runtime_dir / "bootstrap.py"
    agent_script = runtime_dir / "agent.sh"
    lines = [
        "#!/bin/sh",
        "set -eu",
        'PYTHON_BIN=""',
        'if command -v python >/dev/null 2>&1; then PYTHON_BIN="python"; '
        'elif command -v python3 >/dev/null 2>&1; then PYTHON_BIN="python3"; fi',
        '[ -n "$PYTHON_BIN" ] || { echo "python not available" >&2; exit 127; }',
        'exec "$PYTHON_BIN" '
        + shlex.join(
            (str(bootstrap), str(guest_env_path), "/bin/sh", str(agent_script))
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _write_bootstrap(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations

import os
from pathlib import Path
import sys


def load_generated_dotenv(path: Path) -> dict[str, str]:
    text = path.read_text(encoding=\"utf-8\")
    values: dict[str, str] = {}
    index = 0
    while index < len(text):
        if text[index] in \" \\t\\n\":
            index += 1
            continue
        if text[index] == \"#\":
            newline = text.find(\"\\n\", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        separator = text.find('=\"', index)
        if separator < 0:
            raise ValueError(\"invalid staged guest environment\")
        name = text[index:separator]
        index = separator + 2
        value: list[str] = []
        while index < len(text):
            char = text[index]
            index += 1
            if char == \"\\\\\":
                if index >= len(text):
                    raise ValueError(\"invalid staged guest environment\")
                escaped = text[index]
                index += 1
                value.append(\"\\r\" if escaped == \"r\" else escaped)
            elif char == '\"':
                break
            else:
                value.append(char)
        else:
            raise ValueError(\"invalid staged guest environment\")
        if not name or any(char.isspace() or char in \"=#\\x00\" for char in name):
            raise ValueError(\"invalid staged guest environment\")
        values[name] = \"\".join(value)
    return values


def main() -> None:
    environ = dict(os.environ)
    environ.update(load_generated_dotenv(Path(sys.argv[1])))
    os.execvpe(sys.argv[2], sys.argv[2:], environ)


if __name__ == \"__main__\":
    main()
""",
        encoding="utf-8",
    )


def _write_guest_env(
    path: Path,
    *,
    dotenv_envs: Mapping[str, str],
    process_envs: Mapping[str, str],
) -> None:
    content = _dotenv_section("Root and agent dotenv values", dotenv_envs)
    content += "\n"
    content += _dotenv_section("Filtered host process values", process_envs)
    atomic_write_text(path, content)
    path.chmod(0o600)


def _dotenv_section(title: str, environ: Mapping[str, str]) -> str:
    return f"# {title}\n" + "".join(
        f'{_dotenv_name(name)}="{_dotenv_value(value)}"\n'
        for name, value in sorted(environ.items())
    )


def _validate_guest_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        _dotenv_name(name)
        _dotenv_value(value)


def _dotenv_name(name: str) -> str:
    if not name or any(char.isspace() or char in "=#\x00" for char in name):
        raise ValueError(f"invalid guest environment variable name: {name!r}")
    return name


def _dotenv_value(value: str) -> str:
    if "\x00" in value:
        raise ValueError("guest environment variable values must not contain NUL")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")


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
    mounts: tuple[SandboxMount, ...],
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
        "--add-host",
        f"{DEFAULT_HOST_GATEWAY}:host-gateway",
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
