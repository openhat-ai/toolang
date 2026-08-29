"""Prepare the Toolang guest bootstrap used by Docker sandboxes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import shlex
import shutil

from toolang.base.types.sandbox import SandboxRequest
from toolang.common.files import atomic_write_text


DOCKER_TOOLANG_COMPATIBILITY_ERROR = (
    "The installed Toolang package cannot start the required AgentServer."
)
UV_BOOTSTRAP_VERSION = "0.12.7"
PYTHON_BOOTSTRAP_VERSION = "3.13"
_PYTHON_DOWNLOAD_CODE = """import os
import shutil
import sys
import urllib.request

headers = {"User-Agent": "curl/8"}
token = os.environ.get("AUTH_TOKEN")
if token:
    headers["Authorization"] = "Bearer " + token
request = urllib.request.Request(sys.argv[1], headers=headers)
with urllib.request.urlopen(request, timeout=30) as response:
    with open(sys.argv[2], "wb") as destination:
        shutil.copyfileobj(response, destination)
"""


def write_start_script(
    path: Path,
    *,
    runtime_dir: Path,
    guest_env_path: Path,
    diagnostic_path: Path,
    diagnostic_display_path: Path,
    command: tuple[str, ...],
    hosted_dev_artifact: Path | None,
    sandbox_instance_path: Path,
) -> None:
    """Write the POSIX-shell portion of one Docker guest bootstrap."""

    if not command:
        raise ValueError("docker sandbox requires a command")
    source = str(hosted_dev_artifact) if hosted_dev_artifact is not None else "toolang"
    tool_command = command if command[0] in {"too", "toolang"} else ("too", *command)
    bootstrap = runtime_dir / "bootstrap.py"
    installer_url = f"https://astral.sh/uv/{UV_BOOTSTRAP_VERSION}/install.sh"
    lines = [
        "#!/bin/sh",
        "set -eu",
        "TOOLANG_DIAGNOSTIC=" + shlex.quote(str(diagnostic_path)),
        "TOOLANG_DIAGNOSTIC_DISPLAY=" + shlex.quote(str(diagnostic_display_path)),
        'if ! : >>"$TOOLANG_DIAGNOSTIC"; then',
        "  echo 'Docker guest diagnostic log is not writable.' >&2",
        "  exit 73",
        "fi",
        'chmod 600 "$TOOLANG_DIAGNOSTIC" 2>/dev/null || :',
        "fail() {",
        "  printf '%s\\n' \"$1\" >&2",
        "  printf 'Log: %s\\n' \"$TOOLANG_DIAGNOSTIC_DISPLAY\" >&2",
        '  exit "${2:-1}"',
        "}",
        'TOOLANG_GUEST_RUNTIME="${TOOLANG_GUEST_RUNTIME:-'
        '${TMPDIR:-/tmp}/toolang-runtime}"',
        'case "$TOOLANG_GUEST_RUNTIME" in',
        "  /*) ;;",
        "  *) fail 'Docker guest runtime path must be absolute.' 64 ;;",
        "esac",
        'export UV_INSTALL_DIR="$TOOLANG_GUEST_RUNTIME/bin"',
        'export UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR"',
        'export UV_PYTHON_INSTALL_DIR="$TOOLANG_GUEST_RUNTIME/python"',
        'export UV_PYTHON_BIN_DIR="$TOOLANG_GUEST_RUNTIME/python-bin"',
        'export UV_TOOL_DIR="$TOOLANG_GUEST_RUNTIME/tools"',
        'export UV_TOOL_BIN_DIR="$TOOLANG_GUEST_RUNTIME/tool-bin"',
        'export UV_CACHE_DIR="$TOOLANG_GUEST_RUNTIME/cache"',
        "export UV_NO_MODIFY_PATH=1",
        'export PATH="$UV_TOOL_BIN_DIR:$UV_INSTALL_DIR:$PATH"',
        'if ! mkdir -p "$UV_INSTALL_DIR" "$UV_PYTHON_INSTALL_DIR" '
        '"$UV_PYTHON_BIN_DIR" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" '
        '"$UV_CACHE_DIR" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then',
        "  fail 'Docker guest runtime directory is not writable.' 73",
        "fi",
        'TOOLANG_EXEC_PROBE="$TOOLANG_GUEST_RUNTIME/.exec-test"',
        "if ! printf '#!/bin/sh\nexit 0\n' >\"$TOOLANG_EXEC_PROBE\" ||",
        '   ! chmod 700 "$TOOLANG_EXEC_PROBE" ||',
        '   ! "$TOOLANG_EXEC_PROBE" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then',
        "  fail 'Docker guest runtime directory is not executable.' 126",
        "fi",
        'rm -f "$TOOLANG_EXEC_PROBE"',
        'have() { command -v "$1" >/dev/null 2>&1; }',
        "uv_capable() {",
        '  "$1" python find --help >/dev/null 2>&1 &&',
        '    "$1" python install --help >/dev/null 2>&1 &&',
        '    "$1" tool install --help >/dev/null 2>&1',
        "}",
        'TOOLANG_PYTHON_DOWNLOADER=""',
        "download_file() {",
        "  if have curl; then",
        '    curl -LsSf "$1" -o "$2"',
        "  elif have wget; then",
        '    wget -q "$1" -O "$2"',
        "  elif have python3; then",
        "    TOOLANG_PYTHON_DOWNLOADER=$(command -v python3)",
        "    export TOOLANG_PYTHON_DOWNLOADER",
        '    "$TOOLANG_PYTHON_DOWNLOADER" -c '
        + shlex.quote(_PYTHON_DOWNLOAD_CODE)
        + ' "$1" "$2"',
        "  elif have python; then",
        "    TOOLANG_PYTHON_DOWNLOADER=$(command -v python)",
        "    export TOOLANG_PYTHON_DOWNLOADER",
        '    "$TOOLANG_PYTHON_DOWNLOADER" -c '
        + shlex.quote(_PYTHON_DOWNLOAD_CODE)
        + ' "$1" "$2"',
        "  else",
        "    return 127",
        "  fi",
        "}",
        'TOOLANG_UV_BIN=""',
        'if have uv && uv_capable "$(command -v uv)"; then',
        "  TOOLANG_UV_BIN=$(command -v uv)",
        '  TOOLANG_UV_VERSION=$("$TOOLANG_UV_BIN" --version '
        '2>>"$TOOLANG_DIAGNOSTIC") ||',
        "    fail 'Could not inspect the existing uv installation.'",
        '  printf "Using uv · %s\\n" "${TOOLANG_UV_VERSION#uv }" >&2',
        "else",
        "  echo 'Installing uv...' >&2",
        '  TOOLANG_UV_INSTALLER="$TOOLANG_GUEST_RUNTIME/uv-install.sh"',
        "  if ! have curl && ! have wget && ! have python3 && ! have python; then",
        "    fail 'Could not install uv: curl, wget, or Python is required.' 127",
        "  fi",
        "  if ! download_file "
        + shlex.quote(installer_url)
        + ' "$TOOLANG_UV_INSTALLER" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then',
        "    fail 'Could not download the uv installer.'",
        "  fi",
        '  TOOLANG_PYTHON_CURL="$UV_INSTALL_DIR/curl"',
        '  if [ -n "$TOOLANG_PYTHON_DOWNLOADER" ]; then',
        "    printf '%s\\n' "
        + " ".join(
            shlex.quote(line)
            for line in (
                "#!/bin/sh",
                'TOOLANG_CURL_URL=""',
                'TOOLANG_CURL_OUTPUT=""',
                'while [ "$#" -gt 0 ]; do',
                '  case "$1" in',
                '    -o) TOOLANG_CURL_OUTPUT="$2"; shift 2 ;;',
                "    --header) shift 2 ;;",
                "    -*) shift ;;",
                '    *) TOOLANG_CURL_URL="$1"; shift ;;',
                "  esac",
                "done",
                '[ -n "$TOOLANG_CURL_URL" ] && [ -n "$TOOLANG_CURL_OUTPUT" ] || exit 2',
                'exec "$TOOLANG_PYTHON_DOWNLOADER" -c '
                + shlex.quote(_PYTHON_DOWNLOAD_CODE)
                + ' "$TOOLANG_CURL_URL" "$TOOLANG_CURL_OUTPUT"',
            )
        )
        + ' >"$TOOLANG_PYTHON_CURL"',
        '    chmod 700 "$TOOLANG_PYTHON_CURL"',
        "  fi",
        '  if ! UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR" UV_NO_MODIFY_PATH=1 '
        '/bin/sh "$TOOLANG_UV_INSTALLER" '
        '>>"$TOOLANG_DIAGNOSTIC" 2>&1; then',
        "    fail 'Could not install uv.'",
        "  fi",
        '  rm -f "$TOOLANG_UV_INSTALLER" "$TOOLANG_PYTHON_CURL"',
        '  TOOLANG_UV_BIN="$UV_INSTALL_DIR/uv"',
        '  uv_capable "$TOOLANG_UV_BIN" ||',
        "    fail 'Installed uv lacks required Python or tool commands.' 64",
        '  TOOLANG_UV_VERSION=$("$TOOLANG_UV_BIN" --version '
        '2>>"$TOOLANG_DIAGNOSTIC") ||',
        "    fail 'Could not inspect the installed uv version.'",
        '  printf "Installed uv · %s\\n" "${TOOLANG_UV_VERSION#uv }" >&2',
        "fi",
        'if TOOLANG_PYTHON_BIN=$("$TOOLANG_UV_BIN" python find --system '
        "--no-python-downloads '>=3.11' 2>>\"$TOOLANG_DIAGNOSTIC\"); then",
        '  TOOLANG_PYTHON_VERSION=$("$TOOLANG_PYTHON_BIN" -c '
        "'import platform; print(platform.python_version())' "
        '2>>"$TOOLANG_DIAGNOSTIC") ||',
        "    fail 'Could not inspect the existing Python installation.'",
        '  printf "Using Python · %s\\n" "$TOOLANG_PYTHON_VERSION" >&2',
        "else",
        "  echo 'Installing Python...' >&2",
        '  if ! "$TOOLANG_UV_BIN" python install --no-progress '
        + shlex.quote(PYTHON_BOOTSTRAP_VERSION)
        + ' >>"$TOOLANG_DIAGNOSTIC" 2>&1; then',
        f"    fail 'Could not install Python {PYTHON_BOOTSTRAP_VERSION}.'",
        "  fi",
        '  TOOLANG_PYTHON_BIN=$("$TOOLANG_UV_BIN" python find --managed-python '
        "--no-python-downloads "
        + shlex.quote(PYTHON_BOOTSTRAP_VERSION)
        + ' 2>>"$TOOLANG_DIAGNOSTIC") ||',
        f"    fail 'Could not locate the installed Python {PYTHON_BOOTSTRAP_VERSION}.'",
        '  TOOLANG_PYTHON_VERSION=$("$TOOLANG_PYTHON_BIN" -c '
        "'import platform; print(platform.python_version())' "
        '2>>"$TOOLANG_DIAGNOSTIC") ||',
        "    fail 'Could not inspect the installed Python version.'",
        '  printf "Installed Python · %s\\n" "$TOOLANG_PYTHON_VERSION" >&2',
        "fi",
        'exec "$TOOLANG_PYTHON_BIN" '
        + shlex.join(
            (
                str(bootstrap),
                str(guest_env_path),
                str(diagnostic_path),
                str(diagnostic_display_path),
                str(sandbox_instance_path),
            )
        )
        + ' "$TOOLANG_UV_BIN" "$TOOLANG_PYTHON_BIN" '
        + shlex.join((source, *tool_command)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def write_bootstrap(path: Path) -> None:
    """Write the Python portion that loads dotenv data and starts Toolang."""

    source = """from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


COMPATIBILITY_ERROR = __TOOLANG_COMPATIBILITY_ERROR__


def load_generated_dotenv(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    index = 0
    while index < len(text):
        if text[index] in " \\t\\n":
            index += 1
            continue
        if text[index] == "#":
            newline = text.find("\\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        separator = text.find('="', index)
        if separator < 0:
            raise ValueError("invalid staged guest environment")
        name = text[index:separator]
        index = separator + 2
        value: list[str] = []
        while index < len(text):
            char = text[index]
            index += 1
            if char == "\\\\":
                if index >= len(text):
                    raise ValueError("invalid staged guest environment")
                escaped = text[index]
                index += 1
                value.append("\\r" if escaped == "r" else escaped)
            elif char == '"':
                break
            else:
                value.append(char)
        else:
            raise ValueError("invalid staged guest environment")
        if not name or any(char.isspace() or char in "=#\\x00" for char in name):
            raise ValueError("invalid staged guest environment")
        values[name] = "".join(value)
    return values


def wait_for_instance(path: Path) -> str:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.endswith("\\n"):
            value = text[:-1]
            if value and value == value.strip() and not any(
                char.isspace() for char in value
            ):
                return value
        time.sleep(0.05)
    raise RuntimeError("Docker sandbox instance is unavailable.")


def run_logged(
    command: tuple[str, ...],
    *,
    environ: dict[str, str],
    diagnostic: Path,
) -> int:
    try:
        with diagnostic.open("ab") as stream:
            completed = subprocess.run(
                command,
                check=False,
                env=environ,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        append_diagnostic(diagnostic, str(exc))
        return 127
    return completed.returncode


def capture(
    command: tuple[str, ...],
    *,
    environ: dict[str, str],
    diagnostic: Path,
) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            env=environ,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        append_diagnostic(diagnostic, str(exc))
        return None
    if completed.returncode == 0:
        return completed.stdout.strip()
    append_diagnostic(diagnostic, completed.stdout, completed.stderr)
    return None


def compatibility_fix(source: str) -> str:
    if source == "toolang":
        return "Build a wheel with `uv build --wheel` and pass it with `--dev dist`."
    return "Rebuild or select a compatible Toolang wheel and pass it with `--dev`."


def installation_fix(source: str) -> str:
    if source == "toolang":
        return "Check package index access, or build a wheel and pass it with `--dev dist`."
    return "Rebuild or select a valid Toolang wheel and pass it with `--dev`."


def append_diagnostic(path: Path, *values: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as stream:
            for value in values:
                if value:
                    stream.write(value.rstrip("\\n") + "\\n")
    except OSError:
        pass


def fail(message: str, diagnostic: str, fix: str | None = None) -> None:
    print(message, file=sys.stderr)
    if fix is not None:
        print(f"Fix: {fix}", file=sys.stderr)
    print(f"Log: {diagnostic}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) < 9:
        raise SystemExit("invalid Docker guest bootstrap arguments")
    (
        env_path,
        diagnostic_path,
        diagnostic_display,
        instance_path,
        uv_bin,
        python_bin,
        source,
        *command,
    ) = sys.argv[1:]
    diagnostic = Path(diagnostic_path)
    environ = dict(os.environ)
    try:
        environ.update(load_generated_dotenv(Path(env_path)))
        environ["TOOLANG_SANDBOX_INSTANCE"] = wait_for_instance(
            Path(instance_path)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        append_diagnostic(diagnostic, str(exc))
        fail("Could not load the Docker guest environment.", diagnostic_display)

    source_label = "package index" if source == "toolang" else Path(source).name
    print("Installing Toolang...", file=sys.stderr, flush=True)
    installed = run_logged(
        (
            uv_bin,
            "tool",
            "install",
            "--quiet",
            "--no-progress",
            "--force",
            "--python",
            python_bin,
            source,
        ),
        environ=environ,
        diagnostic=diagnostic,
    )
    if installed != 0:
        fail(
            f"Could not install Toolang from {source_label}.",
            diagnostic_display,
            installation_fix(source),
        )

    executable = Path(environ["UV_TOOL_BIN_DIR"]) / Path(command[0]).name
    version = capture(
        (str(executable), "--version"),
        environ=environ,
        diagnostic=diagnostic,
    )
    compatible = run_logged(
        (str(executable), "serve", "--help"),
        environ=environ,
        diagnostic=diagnostic,
    )
    if version is None or compatible != 0:
        fail(COMPATIBILITY_ERROR, diagnostic_display, compatibility_fix(source))
    label = version.removeprefix("toolang ").strip() or "unknown"
    print(f"Installed Toolang · {label}", file=sys.stderr, flush=True)
    print("Starting agent...", file=sys.stderr, flush=True)
    argv = (str(executable), *command[1:])
    try:
        os.execvpe(str(executable), argv, environ)
    except OSError as exc:
        append_diagnostic(diagnostic, str(exc))
        fail("Could not start the installed Toolang command.", diagnostic_display)


if __name__ == "__main__":
    main()
"""
    path.write_text(
        source.replace(
            "__TOOLANG_COMPATIBILITY_ERROR__",
            repr(DOCKER_TOOLANG_COMPATIBILITY_ERROR),
        ),
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


def prepare_sandbox_instance(path: Path) -> None:
    atomic_write_text(path, "")
    path.chmod(0o600)


def write_sandbox_instance(path: str | Path, instance: str) -> None:
    value = instance.strip()
    if not value or value != instance:
        raise ValueError("docker sandbox instance must be a nonempty token")
    target = Path(path)
    atomic_write_text(target, value + "\n")
    target.chmod(0o600)


def prepare_diagnostic_log(
    request: SandboxRequest,
    launch_id: str,
) -> tuple[Path, Path]:
    relative = Path(".runtime") / f"sandbox-bootstrap-{launch_id}.log"
    local_path = request.local_home / relative
    local_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(local_path, "")
    local_path.chmod(0o600)
    return local_path, request.hosted_home / relative


def remove_diagnostic_log(path: str | Path, *, ignore_errors: bool = False) -> None:
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
