#!/bin/sh
set -eu

UV_BOOTSTRAP_VERSION=0.12.7
PYTHON_BOOTSTRAP_VERSION=3.13

if [ "$#" -lt 6 ]; then
  echo "Usage: docker_guest.sh ENV DIAGNOSTIC DISPLAY_LOG INSTANCE SOURCE COMMAND..." >&2
  exit 64
fi

TOOLANG_ENV_PATH=$1
TOOLANG_DIAGNOSTIC=$2
TOOLANG_DIAGNOSTIC_DISPLAY=$3
TOOLANG_INSTANCE_PATH=$4
TOOLANG_SOURCE=$5
shift 5

case "$1" in
  too | toolang) ;;
  *) set -- too "$@" ;;
esac

if ! : >>"$TOOLANG_DIAGNOSTIC"; then
  echo "Docker guest diagnostic log is not writable." >&2
  exit 73
fi
chmod 600 "$TOOLANG_DIAGNOSTIC" 2>/dev/null || :

fail() {
  printf '%s\n' "$1" >&2
  printf 'Log: %s\n' "$TOOLANG_DIAGNOSTIC_DISPLAY" >&2
  exit "${2:-1}"
}

TOOLANG_SYSTEM=$(uname -s 2>>"$TOOLANG_DIAGNOSTIC" || :)
[ "$TOOLANG_SYSTEM" = Linux ] ||
  fail "Docker guest bootstrap supports Linux containers only." 64

TOOLANG_GUEST_RUNTIME="${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}"
case "$TOOLANG_GUEST_RUNTIME" in
  /*) ;;
  *) fail "Docker guest runtime path must be absolute." 64 ;;
esac

export UV_INSTALL_DIR="$TOOLANG_GUEST_RUNTIME/bin"
export UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR"
export UV_PYTHON_INSTALL_DIR="$TOOLANG_GUEST_RUNTIME/python"
export UV_PYTHON_BIN_DIR="$TOOLANG_GUEST_RUNTIME/python-bin"
export UV_TOOL_DIR="$TOOLANG_GUEST_RUNTIME/tools"
export UV_TOOL_BIN_DIR="$TOOLANG_GUEST_RUNTIME/tool-bin"
export UV_CACHE_DIR="$TOOLANG_GUEST_RUNTIME/cache"
export UV_NO_MODIFY_PATH=1
export PATH="$UV_TOOL_BIN_DIR:$UV_INSTALL_DIR:$PATH"

if ! mkdir -p \
  "$UV_INSTALL_DIR" \
  "$UV_PYTHON_INSTALL_DIR" \
  "$UV_PYTHON_BIN_DIR" \
  "$UV_TOOL_DIR" \
  "$UV_TOOL_BIN_DIR" \
  "$UV_CACHE_DIR" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
  fail "Docker guest runtime directory is not writable." 73
fi

TOOLANG_EXEC_PROBE="$TOOLANG_GUEST_RUNTIME/.exec-test"
if ! printf '#!/bin/sh\nexit 0\n' >"$TOOLANG_EXEC_PROBE" ||
  ! chmod 700 "$TOOLANG_EXEC_PROBE" ||
  ! "$TOOLANG_EXEC_PROBE" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
  fail "Docker guest runtime directory is not executable." 126
fi
rm -f "$TOOLANG_EXEC_PROBE"

have() {
  command -v "$1" >/dev/null 2>&1
}

TOOLANG_UV_KIND=
TOOLANG_UV_VALUE=
TOOLANG_UV_MODULE_PATH=

uv_run() {
  if [ "$TOOLANG_UV_KIND" = module ]; then
    PYTHONPATH="$TOOLANG_UV_MODULE_PATH${PYTHONPATH:+:$PYTHONPATH}" \
      "$TOOLANG_UV_VALUE" -m uv "$@"
  else
    "$TOOLANG_UV_VALUE" "$@"
  fi
}

uv_capable() {
  uv_run python find --help >/dev/null 2>&1 &&
    uv_run python install --help >/dev/null 2>&1 &&
    uv_run tool install --help >/dev/null 2>&1
}

TOOLANG_UV_INSTALLING=0
begin_uv_install() {
  if [ "$TOOLANG_UV_INSTALLING" = 0 ]; then
    echo "Installing uv..." >&2
    TOOLANG_UV_INSTALLING=1
  fi
}

if have uv; then
  TOOLANG_UV_KIND=binary
  TOOLANG_UV_VALUE=$(command -v uv)
  if ! uv_capable; then
    TOOLANG_UV_KIND=
    TOOLANG_UV_VALUE=
  fi
fi

TOOLANG_BOOTSTRAP_PYTHON=
if [ -z "$TOOLANG_UV_KIND" ]; then
  for TOOLANG_PYTHON_CANDIDATE in python3 python; do
    if have "$TOOLANG_PYTHON_CANDIDATE" &&
      "$TOOLANG_PYTHON_CANDIDATE" -c \
        'import sys; raise SystemExit(sys.version_info < (3, 8))' \
        >>"$TOOLANG_DIAGNOSTIC" 2>&1 &&
      "$TOOLANG_PYTHON_CANDIDATE" -m pip --version \
        >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
      TOOLANG_BOOTSTRAP_PYTHON=$(command -v "$TOOLANG_PYTHON_CANDIDATE")
      break
    fi
  done
fi

if [ -z "$TOOLANG_UV_KIND" ] && [ -n "$TOOLANG_BOOTSTRAP_PYTHON" ]; then
  begin_uv_install
  TOOLANG_UV_MODULE_PATH="$TOOLANG_GUEST_RUNTIME/uv-package"
  if "$TOOLANG_BOOTSTRAP_PYTHON" -m pip install \
    --disable-pip-version-check \
    --no-input \
    --quiet \
    --target "$TOOLANG_UV_MODULE_PATH" \
    "uv==$UV_BOOTSTRAP_VERSION" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
    TOOLANG_UV_KIND=module
    TOOLANG_UV_VALUE=$TOOLANG_BOOTSTRAP_PYTHON
    if ! uv_capable; then
      TOOLANG_UV_KIND=
      TOOLANG_UV_VALUE=
    fi
  fi
fi

download_file() {
  if have curl; then
    curl -LsSf "$1" -o "$2"
  elif have wget; then
    wget -q "$1" -O "$2"
  else
    return 127
  fi
}

if [ -z "$TOOLANG_UV_KIND" ] && { have curl || have wget; }; then
  begin_uv_install
  TOOLANG_UV_INSTALLER="$TOOLANG_GUEST_RUNTIME/uv-install.sh"
  if ! download_file \
    "https://astral.sh/uv/$UV_BOOTSTRAP_VERSION/install.sh" \
    "$TOOLANG_UV_INSTALLER" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
    fail "Could not download the uv installer."
  fi
  if ! UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR" UV_NO_MODIFY_PATH=1 \
    /bin/sh "$TOOLANG_UV_INSTALLER" >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
    fail "Could not install uv."
  fi
  rm -f "$TOOLANG_UV_INSTALLER"
  TOOLANG_UV_KIND=binary
  TOOLANG_UV_VALUE="$UV_INSTALL_DIR/uv"
  uv_capable || fail "Installed uv lacks required Python or tool commands." 64
fi

[ -n "$TOOLANG_UV_KIND" ] ||
  fail "Docker guest image must provide uv, Python 3.8+ with pip, or curl/wget with the uv installer prerequisites." 127

TOOLANG_UV_VERSION=$(uv_run --version 2>>"$TOOLANG_DIAGNOSTIC") ||
  fail "Could not inspect the uv installation."
if [ "$TOOLANG_UV_INSTALLING" = 1 ]; then
  printf 'Installed uv · %s\n' "${TOOLANG_UV_VERSION#uv }" >&2
else
  printf 'Using uv · %s\n' "${TOOLANG_UV_VERSION#uv }" >&2
fi

if TOOLANG_PYTHON_BIN=$(uv_run python find --system --no-python-downloads \
  '>=3.11' 2>>"$TOOLANG_DIAGNOSTIC"); then
  TOOLANG_PYTHON_VERSION=$("$TOOLANG_PYTHON_BIN" -c \
    'import platform; print(platform.python_version())' \
    2>>"$TOOLANG_DIAGNOSTIC") ||
    fail "Could not inspect the existing Python installation."
  printf 'Using Python · %s\n' "$TOOLANG_PYTHON_VERSION" >&2
else
  echo "Installing Python..." >&2
  if ! uv_run python install --no-progress "$PYTHON_BOOTSTRAP_VERSION" \
    >>"$TOOLANG_DIAGNOSTIC" 2>&1; then
    fail "Could not install Python $PYTHON_BOOTSTRAP_VERSION."
  fi
  TOOLANG_PYTHON_BIN=$(uv_run python find --managed-python \
    --no-python-downloads "$PYTHON_BOOTSTRAP_VERSION" \
    2>>"$TOOLANG_DIAGNOSTIC") ||
    fail "Could not locate the installed Python $PYTHON_BOOTSTRAP_VERSION."
  TOOLANG_PYTHON_VERSION=$("$TOOLANG_PYTHON_BIN" -c \
    'import platform; print(platform.python_version())' \
    2>>"$TOOLANG_DIAGNOSTIC") ||
    fail "Could not inspect the installed Python version."
  printf 'Installed Python · %s\n' "$TOOLANG_PYTHON_VERSION" >&2
fi

TOOLANG_PYTHON_BOOTSTRAP="$TOOLANG_GUEST_RUNTIME/bootstrap.py"
if ! cat >"$TOOLANG_PYTHON_BOOTSTRAP" <<'PYTHON_BOOTSTRAP'
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


COMPATIBILITY_ERROR = (
    "The installed Toolang package cannot start the required AgentServer."
)


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


def wait_for_instance(path: Path) -> str:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.endswith("\n"):
            value = text[:-1]
            if value and value == value.strip() and not any(
                char.isspace() for char in value
            ):
                return value
        time.sleep(0.05)
    raise RuntimeError("Docker sandbox instance is unavailable.")


def append_diagnostic(path: Path, *values: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as stream:
            for value in values:
                if value:
                    stream.write(value.rstrip("\n") + "\n")
    except OSError:
        pass


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
        return (
            "Check package index access, or build a wheel and pass it with "
            "`--dev dist`."
        )
    return "Rebuild or select a valid Toolang wheel and pass it with `--dev`."


def runs_agent_server(command: list[str]) -> bool:
    arguments = command[1:]
    if arguments[:1] in (["--root"], ["-r"]):
        arguments = arguments[2:]
    elif arguments and arguments[0].startswith("--root="):
        arguments = arguments[1:]
    return arguments[:1] == ["serve"]


def fail(message: str, diagnostic: str, fix: str | None = None) -> None:
    print(message, file=sys.stderr)
    if fix is not None:
        print(f"Fix: {fix}", file=sys.stderr)
    print(f"Log: {diagnostic}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) < 11:
        raise SystemExit("invalid Docker guest bootstrap arguments")
    (
        env_path,
        diagnostic_path,
        diagnostic_display,
        instance_path,
        uv_kind,
        uv_value,
        uv_module_path,
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

    if uv_kind == "binary":
        uv_command = (uv_value,)
    elif uv_kind == "module":
        uv_command = (uv_value, "-m", "uv")
        environ["PYTHONPATH"] = uv_module_path + (
            os.pathsep + environ["PYTHONPATH"]
            if environ.get("PYTHONPATH")
            else ""
        )
    else:
        fail("Docker guest received an invalid uv runtime.", diagnostic_display)

    source_label = "package index" if source == "toolang" else Path(source).name
    print("Installing Toolang...", file=sys.stderr, flush=True)
    installed = run_logged(
        (
            *uv_command,
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
    if runs_agent_server(command):
        print("Starting agent...", file=sys.stderr, flush=True)
    argv = (str(executable), *command[1:])
    try:
        os.execvpe(str(executable), argv, environ)
    except OSError as exc:
        append_diagnostic(diagnostic, str(exc))
        fail("Could not start the installed Toolang command.", diagnostic_display)


if __name__ == "__main__":
    main()
PYTHON_BOOTSTRAP
then
  fail "Could not write the Docker guest Python bootstrap." 73
fi
chmod 700 "$TOOLANG_PYTHON_BOOTSTRAP" 2>>"$TOOLANG_DIAGNOSTIC" ||
  fail "Could not make the Docker guest Python bootstrap executable." 73

exec "$TOOLANG_PYTHON_BIN" \
  "$TOOLANG_PYTHON_BOOTSTRAP" \
  "$TOOLANG_ENV_PATH" \
  "$TOOLANG_DIAGNOSTIC" \
  "$TOOLANG_DIAGNOSTIC_DISPLAY" \
  "$TOOLANG_INSTANCE_PATH" \
  "$TOOLANG_UV_KIND" \
  "$TOOLANG_UV_VALUE" \
  "$TOOLANG_UV_MODULE_PATH" \
  "$TOOLANG_PYTHON_BIN" \
  "$TOOLANG_SOURCE" \
  "$@"
