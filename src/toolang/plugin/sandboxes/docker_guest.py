"""Install Toolang and replace the Docker guest bootstrap process."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def load_generated_dotenv(path: Path) -> dict[str, str]:
    """Read the restricted dotenv format without shell evaluation."""

    text = path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    index = 0
    while index < len(text):
        if text[index] in " \t\n":
            index += 1
            continue
        if text[index] == "#":
            newline = text.find("\n", index)
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
            if char == "\\":
                if index >= len(text):
                    raise ValueError("invalid staged guest environment")
                escaped = text[index]
                index += 1
                value.append("\r" if escaped == "r" else escaped)
            elif char == '"':
                break
            else:
                value.append(char)
        else:
            raise ValueError("invalid staged guest environment")
        if not name or any(char.isspace() or char in "=#\x00" for char in name):
            raise ValueError("invalid staged guest environment")
        values[name] = "".join(value)
    return values


def fail(message: str, diagnostic_display: str, status: int) -> None:
    """Report one concise failure while retaining private diagnostics."""

    print(message, file=sys.stderr)
    print(f"See {diagnostic_display}", file=sys.stderr)
    raise SystemExit(status)


def report_progress(path: str, token: str, message: str) -> None:
    """Write a closed host event or the standalone harness display line."""

    if path == "-":
        print(message, file=sys.stderr)
        return
    try:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"{token}\n")
    except OSError:
        pass


def report_event(path: str, token: str) -> None:
    """Write a product event without changing standalone harness output."""

    if path != "-":
        report_progress(path, token, "")


def _uv_command(
    *,
    mode: str,
    executable: str,
    module_dir: str,
    environ: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    command = [executable]
    uv_environ = dict(environ)
    if mode == "module":
        command.extend(("-m", "uv"))
        existing = uv_environ.get("PYTHONPATH")
        uv_environ["PYTHONPATH"] = module_dir + (
            os.pathsep + existing if existing else ""
        )
    elif mode != "bin":
        raise ValueError("docker guest uv mode is invalid")
    return command, uv_environ


def _install_toolang(
    *,
    uv_command: list[str],
    uv_environ: dict[str, str],
    python: str,
    package_source: str,
    diagnostic: Path,
    diagnostic_display: str,
    progress_events: str,
) -> None:
    package_fact = Path(package_source).name
    report_progress(
        progress_events,
        "toolang.install.running",
        f"Installing Toolang · {package_fact}...",
    )
    try:
        with diagnostic.open("ab") as stream:
            installed = subprocess.run(
                (
                    *uv_command,
                    "tool",
                    "install",
                    "--quiet",
                    "--no-progress",
                    "--force",
                    "--python",
                    python,
                    package_source,
                ),
                check=False,
                env=uv_environ,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
    except OSError:
        report_event(progress_events, "toolang.install.failed")
        fail("Could not install Toolang", diagnostic_display, 1)
    if installed.returncode != 0:
        report_event(progress_events, "toolang.install.failed")
        fail("Could not install Toolang", diagnostic_display, 1)
    report_event(progress_events, "toolang.install.ok")


def _validate_toolang(
    *,
    too: Path,
    environ: dict[str, str],
    diagnostic: Path,
    diagnostic_display: str,
    progress_events: str,
) -> str:
    report_event(progress_events, "toolang.check.running")
    if not too.is_file() or not os.access(too, os.X_OK):
        report_event(progress_events, "toolang.check.failed")
        fail("Installed Toolang executable is unavailable", diagnostic_display, 69)
    with diagnostic.open("ab") as stream:
        validated = subprocess.run(
            (str(too), "serve", "--help"),
            check=False,
            env=environ,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        version = subprocess.run(
            (str(too), "--version"),
            check=False,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=stream,
            text=True,
        )
    if validated.returncode != 0:
        report_event(progress_events, "toolang.check.failed")
        fail(
            "The installed Toolang package cannot start the required AgentServer",
            diagnostic_display,
            69,
        )
    report_event(progress_events, "toolang.check.ok")
    return version.stdout.strip() if version.returncode == 0 else ""


def _open_workload_log(path: str, diagnostic_display: str) -> int | None:
    if path == "-":
        return None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
    except OSError:
        fail("Could not open workload log", diagnostic_display, 73)
    return descriptor


def main() -> None:
    """Load guest state, install Toolang, and exec the requested workload."""

    if len(sys.argv) < 14 or sys.argv[12] != "--":
        raise ValueError("invalid docker guest helper arguments")
    guest_env = Path(sys.argv[1])
    diagnostic = Path(sys.argv[2])
    diagnostic_display = sys.argv[3]
    progress_events = sys.argv[4]
    package_source = sys.argv[5]
    workload_log = sys.argv[6]
    python = sys.argv[7]
    tool_bin_dir = Path(sys.argv[8])
    uv_mode = sys.argv[9]
    uv_executable = sys.argv[10]
    uv_module_dir = sys.argv[11]
    command = sys.argv[13:]
    if not command:
        raise ValueError("docker guest command is missing")

    hostname = os.environ["HOSTNAME"]
    environ = dict(os.environ)
    environ.update(load_generated_dotenv(guest_env))
    environ["HOSTNAME"] = hostname
    environ["TOOLANG_SANDBOX_INSTANCE"] = hostname
    for name in (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_NO_PROGRESS",
    ):
        environ[name] = os.environ[name]
    environ["PATH"] = str(tool_bin_dir) + os.pathsep + environ.get("PATH", "")

    uv_command, uv_environ = _uv_command(
        mode=uv_mode,
        executable=uv_executable,
        module_dir=uv_module_dir,
        environ=environ,
    )
    _install_toolang(
        uv_command=uv_command,
        uv_environ=uv_environ,
        python=python,
        package_source=package_source,
        diagnostic=diagnostic,
        diagnostic_display=diagnostic_display,
        progress_events=progress_events,
    )
    too = tool_bin_dir / "too"
    version = _validate_toolang(
        too=too,
        environ=environ,
        diagnostic=diagnostic,
        diagnostic_display=diagnostic_display,
        progress_events=progress_events,
    )
    suffix = f" · {version}" if version else ""
    if progress_events == "-":
        print(f"Installed Toolang{suffix}", file=sys.stderr)

    authored_command = command[0]
    if authored_command in {"too", "toolang"}:
        command[0] = str(too)
    descriptor = _open_workload_log(workload_log, diagnostic_display)
    diagnostic.unlink(missing_ok=True)
    if authored_command in {"too", "toolang"} and command[1:2] == ["serve"]:
        report_progress(progress_events, "server.running", "Starting agent...")
    else:
        report_progress(progress_events, "server.running", "Starting command...")
    if descriptor is not None:
        sys.stderr.flush()
        os.dup2(descriptor, sys.stdout.fileno())
        os.dup2(descriptor, sys.stderr.fileno())
        os.close(descriptor)
    os.execvpe(command[0], command, environ)


if __name__ == "__main__":
    main()
