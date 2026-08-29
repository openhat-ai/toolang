#!/bin/sh

set -eu

UV_VERSION="0.12.6"
PYTHON_VERSION="3.13"

fail() {
    printf '%s\n' "$1" >&2
    printf 'See %s\n' "$DIAGNOSTIC_DISPLAY" >&2
    exit "${2:-1}"
}

if [ "$#" -lt 6 ]; then
    printf '%s\n' \
        "usage: docker_guest.sh GUEST_ENV DIAGNOSTIC DISPLAY_PATH PACKAGE -- COMMAND..." \
        >&2
    exit 64
fi

GUEST_ENV=$1
DIAGNOSTIC=$2
DIAGNOSTIC_DISPLAY=$3
PACKAGE_SOURCE=$4
shift 4
if [ "$1" != "--" ]; then
    printf '%s\n' "docker guest command separator is missing" >&2
    exit 64
fi
shift
if [ "$#" -eq 0 ]; then
    printf '%s\n' "docker guest command is missing" >&2
    exit 64
fi

if [ "$(uname -s 2>/dev/null || :)" != "Linux" ]; then
    printf '%s\n' "docker guest requires a Linux image" >&2
    exit 64
fi

RUNTIME_ROOT=${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}
UV_BIN_DIR=$RUNTIME_ROOT/uv-bin
UV_MODULE_DIR=$RUNTIME_ROOT/uv-module
PYTHON_DIR=$RUNTIME_ROOT/python
PYTHON_BIN_DIR=$RUNTIME_ROOT/python-bin
TOOL_DIR=$RUNTIME_ROOT/tools
TOOL_BIN_DIR=$RUNTIME_ROOT/bin
CACHE_DIR=$RUNTIME_ROOT/cache
HELPER=$RUNTIME_ROOT/exec.py
INSTALLER=$RUNTIME_ROOT/install-uv.sh

umask 077
mkdir -p \
    "$RUNTIME_ROOT" \
    "$UV_BIN_DIR" \
    "$UV_MODULE_DIR" \
    "$PYTHON_DIR" \
    "$PYTHON_BIN_DIR" \
    "$TOOL_DIR" \
    "$TOOL_BIN_DIR" \
    "$CACHE_DIR" || {
    printf '%s\n' "docker guest runtime is not writable: $RUNTIME_ROOT" >&2
    exit 73
}
: >"$DIAGNOSTIC" || {
    printf '%s\n' "docker guest diagnostic is not writable: $DIAGNOSTIC_DISPLAY" >&2
    exit 73
}
chmod 600 "$DIAGNOSTIC" 2>/dev/null || :

case "${HOSTNAME:-}" in
    ????????????) ;;
    *) fail "docker guest hostname is not a short container ID" 64 ;;
esac
case "$HOSTNAME" in
    *[!0-9a-f]*) fail "docker guest hostname is not a short container ID" 64 ;;
esac

export UV_CACHE_DIR=$CACHE_DIR
export UV_PYTHON_INSTALL_DIR=$PYTHON_DIR
export UV_PYTHON_BIN_DIR=$PYTHON_BIN_DIR
export UV_TOOL_DIR=$TOOL_DIR
export UV_TOOL_BIN_DIR=$TOOL_BIN_DIR
export UV_NO_PROGRESS=1

UV_MODE=
UV_BIN=
UV_PYTHON=

uv_run() {
    if [ "$UV_MODE" = "module" ]; then
        PYTHONPATH=$UV_MODULE_DIR "$UV_PYTHON" -m uv "$@"
    else
        "$UV_BIN" "$@"
    fi
}

uv_capable() {
    uv_run --version >/dev/null 2>>"$DIAGNOSTIC" &&
        uv_run python find --help >/dev/null 2>>"$DIAGNOSTIC" &&
        uv_run python install --help >/dev/null 2>>"$DIAGNOSTIC" &&
        uv_run tool install --help >/dev/null 2>>"$DIAGNOSTIC"
}

uv_fact() {
    UV_FACT=$(uv_run --version 2>>"$DIAGNOSTIC") || return 1
    UV_FACT=${UV_FACT#uv }
    printf '%s' "$UV_FACT"
}

find_pip_python() {
    for CANDIDATE in python3 python; do
        if command -v "$CANDIDATE" >/dev/null 2>&1 &&
            "$CANDIDATE" -c \
                'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
                >>"$DIAGNOSTIC" 2>&1 &&
            "$CANDIDATE" -m pip --version >>"$DIAGNOSTIC" 2>&1; then
            command -v "$CANDIDATE"
            return 0
        fi
    done
    return 1
}

if command -v uv >/dev/null 2>&1; then
    UV_MODE=bin
    UV_BIN=$(command -v uv)
    if ! uv_capable; then
        UV_MODE=
        UV_BIN=
    fi
fi

if [ -n "$UV_MODE" ]; then
    UV_FACT=$(uv_fact) || fail "Existing uv is unusable" 69
    printf 'Using uv · %s\n' "$UV_FACT" >&2
else
    printf 'Installing uv...\n' >&2
    BOOTSTRAP_PYTHON=$(find_pip_python || :)
    if [ -n "$BOOTSTRAP_PYTHON" ]; then
        if "$BOOTSTRAP_PYTHON" -m pip install \
            --disable-pip-version-check \
            --no-input \
            --quiet \
            --target "$UV_MODULE_DIR" \
            "uv==$UV_VERSION" >>"$DIAGNOSTIC" 2>&1; then
            UV_MODE=module
            UV_PYTHON=$BOOTSTRAP_PYTHON
            if ! uv_capable; then
                UV_MODE=
                UV_PYTHON=
            fi
        fi
    fi

    if [ -z "$UV_MODE" ]; then
        INSTALLER_URL="https://astral.sh/uv/$UV_VERSION/install.sh"
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf "$INSTALLER_URL" -o "$INSTALLER" \
                >>"$DIAGNOSTIC" 2>&1 ||
                fail "Could not download uv" 69
        elif command -v wget >/dev/null 2>&1; then
            wget -qO "$INSTALLER" "$INSTALLER_URL" \
                >>"$DIAGNOSTIC" 2>&1 ||
                fail "Could not download uv" 69
        else
            fail \
                "Unsupported image: provide uv, Python 3.8+ with pip, curl, or wget" \
                69
        fi
        UV_UNMANAGED_INSTALL=$UV_BIN_DIR sh "$INSTALLER" \
            >>"$DIAGNOSTIC" 2>&1 || fail "Could not install uv" 69
        UV_MODE=bin
        UV_BIN=$UV_BIN_DIR/uv
        uv_capable || fail "Installed uv is unusable" 69
    fi
    UV_FACT=$(uv_fact) || fail "Installed uv is unusable" 69
    printf 'Installed uv · %s\n' "$UV_FACT" >&2
fi

PYTHON_BIN=$(uv_run python find '>=3.11,<4' 2>>"$DIAGNOSTIC" || :)
if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then
    PYTHON_FACT=$(
        "$PYTHON_BIN" -c 'import platform; print(platform.python_version())' \
            2>>"$DIAGNOSTIC"
    ) || fail "Existing Python is unusable" 69
    printf 'Using Python · %s\n' "$PYTHON_FACT" >&2
else
    printf 'Installing Python · %s...\n' "$PYTHON_VERSION" >&2
    uv_run python install "$PYTHON_VERSION" --no-progress \
        >>"$DIAGNOSTIC" 2>&1 || fail "Could not install Python $PYTHON_VERSION" 69
    PYTHON_BIN=$(uv_run python find "$PYTHON_VERSION" 2>>"$DIAGNOSTIC" || :)
    if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
        fail "Installed Python $PYTHON_VERSION is unavailable" 69
    fi
    PYTHON_FACT=$(
        "$PYTHON_BIN" -c 'import platform; print(platform.python_version())' \
            2>>"$DIAGNOSTIC"
    ) || fail "Installed Python is unusable" 69
    printf 'Installed Python · %s\n' "$PYTHON_FACT" >&2
fi

cat >"$HELPER" <<'PYTHON'
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def load_generated_dotenv(path: Path) -> dict[str, str]:
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
    print(message, file=sys.stderr)
    print(f"See {diagnostic_display}", file=sys.stderr)
    raise SystemExit(status)


def main() -> None:
    guest_env = Path(sys.argv[1])
    diagnostic = Path(sys.argv[2])
    diagnostic_display = sys.argv[3]
    package_source = sys.argv[4]
    python = sys.argv[5]
    tool_bin_dir = Path(sys.argv[6])
    uv_mode = sys.argv[7]
    uv_executable = sys.argv[8]
    uv_module_dir = sys.argv[9]
    if sys.argv[10] != "--":
        raise ValueError("docker guest command separator is missing")
    command = sys.argv[11:]
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

    uv_command = [uv_executable]
    uv_environ = dict(environ)
    if uv_mode == "module":
        uv_command.extend(("-m", "uv"))
        existing_pythonpath = uv_environ.get("PYTHONPATH")
        uv_environ["PYTHONPATH"] = uv_module_dir + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
    elif uv_mode != "bin":
        raise ValueError("docker guest uv mode is invalid")

    package_fact = Path(package_source).name
    print(f"Installing Toolang · {package_fact}...", file=sys.stderr)
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
        fail("Could not install Toolang", diagnostic_display, 1)
    if installed.returncode != 0:
        fail("Could not install Toolang", diagnostic_display, 1)

    too = tool_bin_dir / "too"
    if not too.is_file() or not os.access(too, os.X_OK):
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
        fail(
            "The installed Toolang package cannot start the required AgentServer",
            diagnostic_display,
            69,
        )
    toolang_fact = version.stdout.strip() if version.returncode == 0 else ""
    suffix = f" · {toolang_fact}" if toolang_fact else ""
    print(f"Installed Toolang{suffix}", file=sys.stderr)

    diagnostic.unlink(missing_ok=True)
    authored_command = command[0]
    if command[0] in {"too", "toolang"}:
        command[0] = str(too)
    if authored_command in {"too", "toolang"} and command[1:2] == ["serve"]:
        print("Starting agent...", file=sys.stderr)
    else:
        print("Starting command...", file=sys.stderr)
    os.execvpe(command[0], command, environ)


if __name__ == "__main__":
    main()
PYTHON
chmod 700 "$HELPER"

if [ "$UV_MODE" = "module" ]; then
    UV_EXECUTABLE=$UV_PYTHON
else
    UV_EXECUTABLE=$UV_BIN
fi
exec "$PYTHON_BIN" "$HELPER" \
    "$GUEST_ENV" \
    "$DIAGNOSTIC" \
    "$DIAGNOSTIC_DISPLAY" \
    "$PACKAGE_SOURCE" \
    "$PYTHON_BIN" \
    "$TOOL_BIN_DIR" \
    "$UV_MODE" \
    "$UV_EXECUTABLE" \
    "$UV_MODULE_DIR" \
    -- \
    "$@"
