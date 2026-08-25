"""Prepare the Toolang guest bootstrap used by Docker sandboxes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import shlex
import shutil

from toolang.base.types.sandbox import SandboxRequest
from toolang.common.files import atomic_write_text


DOCKER_TOOLANG_COMPATIBILITY_ERROR = (
    "Installed Toolang does not provide the required `too serve` entrypoint."
)


def write_agent_script(
    path: Path,
    *,
    command: tuple[str, ...],
    hosted_dev_artifact: Path | None,
    startup_events_path: Path,
    validation_error_to_stderr: bool,
) -> None:
    if not command:
        raise ValueError("docker sandbox requires a command")
    source = str(hosted_dev_artifact) if hosted_dev_artifact is not None else "toolang"
    tool_command = command if command[0] in {"too", "toolang"} else ("too", *command)
    lines = [
        "#!/bin/sh",
        "set -eu",
        'export PATH="$HOME/.local/bin:$PATH"',
        'export TOOLANG_SANDBOX_INSTANCE="${HOSTNAME:?docker sandbox hostname is unavailable}"',
        "startup_event() { printf '%s\\n' \"$1\" >>"
        + shlex.quote(str(startup_events_path))
        + "; }",
        'have() { command -v "$1" >/dev/null 2>&1; }',
        'PYTHON_BIN=""',
        'if have python; then PYTHON_BIN="python"; elif have python3; then PYTHON_BIN="python3"; fi',
        "ensure_uv() {",
        "  have uv && return 0",
        '  [ -n "$PYTHON_BIN" ] || { echo "python not available" >&2; return 127; }',
        '  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true',
        '  "$PYTHON_BIN" -m pip install --disable-pip-version-check '
        "--root-user-action=ignore --quiet --user -U uv || true",
        "  have uv && return 0",
        "  if have curl; then curl -LsSf https://astral.sh/uv/install.sh | sh "
        ">/dev/null; fi",
        "  have uv || { echo 'uv not available' >&2; return 127; }",
        "}",
        "startup_event install.running",
        "if ! ensure_uv; then",
        "  startup_event install.failed",
        "  exit 127",
        "fi",
        "if ! uv tool install --quiet --no-progress --force "
        + shlex.quote(source)
        + "; then",
        "  startup_event install.failed",
        "  exit 1",
        "fi",
        "startup_event install.ok",
        "startup_event validate.running",
        "if ! " + shlex.quote(tool_command[0]) + " serve --help >/dev/null 2>&1; then",
        "  startup_event validate.failed",
        *(
            ["  echo " + shlex.quote(DOCKER_TOOLANG_COMPATIBILITY_ERROR) + " >&2"]
            if validation_error_to_stderr
            else []
        ),
        "  exit 64",
        "fi",
        "startup_event validate.ok",
        "startup_event server.running",
        "exec " + shlex.join(tool_command),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def write_start_script(
    path: Path,
    *,
    runtime_dir: Path,
    guest_env_path: Path,
    log_path: Path | None,
) -> None:
    bootstrap = runtime_dir / "bootstrap.py"
    agent_script = runtime_dir / "agent.sh"
    lines = [
        "#!/bin/sh",
        "set -eu",
        *(
            [f"exec >>{shlex.quote(str(log_path))} 2>&1"]
            if log_path is not None
            else []
        ),
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


def write_bootstrap(path: Path) -> None:
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


def write_guest_env(
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


def validate_guest_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        _dotenv_name(name)
        _dotenv_value(value)


def prepare_stage_directory(stage_dir: Path) -> None:
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir()


def prepare_startup_events(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "")
    path.chmod(0o600)


def remove_startup_events(path: str | Path, *, ignore_errors: bool = False) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        if not ignore_errors:
            raise


def remove_stage_directory(
    stage_dir: str | Path,
    *,
    ignore_errors: bool = False,
) -> None:
    path = Path(stage_dir)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=ignore_errors)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise


def prepare_background_log(request: SandboxRequest) -> Path | None:
    if request.output == "inherit":
        if request.log_path is not None:
            raise ValueError("inherited docker output does not accept a log path")
        return None
    if request.output != "file":
        raise ValueError(f"unsupported docker output mode: {request.output}")
    log_path = request.log_path
    if log_path is None:
        raise ValueError("docker file output requires a log path")
    resolved_home = request.local_home.resolve()
    resolved_log = log_path.resolve()
    try:
        relative = resolved_log.relative_to(resolved_home)
    except ValueError as exc:
        raise ValueError("docker background log must be inside the agent home") from exc
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    resolved_log.touch(mode=0o600, exist_ok=True)
    resolved_log.chmod(0o600)
    return request.hosted_home / relative


def _dotenv_section(title: str, environ: Mapping[str, str]) -> str:
    return f"# {title}\n" + "".join(
        f'{_dotenv_name(name)}="{_dotenv_value(value)}"\n'
        for name, value in sorted(environ.items())
    )


def _dotenv_name(name: str) -> str:
    if not name or any(char.isspace() or char in "=#\x00" for char in name):
        raise ValueError(f"invalid guest environment variable name: {name!r}")
    return name


def _dotenv_value(value: str) -> str:
    if "\x00" in value:
        raise ValueError("guest environment variable values must not contain NUL")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")
