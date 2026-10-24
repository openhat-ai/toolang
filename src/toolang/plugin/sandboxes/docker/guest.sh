#!/bin/sh

set -eu
umask 077

fail() {
    printf '%s\nSee %s\n' "$1" "$DIAGNOSTIC_DISPLAY" >&3
    exit "${2:-1}"
}

progress() {
    if [ "$PROGRESS_EVENTS" != "-" ]; then
        { printf '%s\n' "$1" >>"$PROGRESS_EVENTS"; } 2>/dev/null || :
    elif [ "$#" -ge 2 ]; then
        printf '%s\n' "$2" >&3
    fi
}

tool_bin_dir() {
    printf '%s/bin' "${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}"
}

prepare_runtime() {
    TOOL_BIN_DIR=$(tool_bin_dir)
    RUNTIME_ROOT=${TOOL_BIN_DIR%/bin}
    mkdir -p "$TOOL_BIN_DIR" || {
        printf '%s\n' "docker guest runtime is not writable: $RUNTIME_ROOT" >&2
        exit 73
    }
    : >"$DIAGNOSTIC" || {
        printf '%s\n' "docker guest diagnostic is not writable: $DIAGNOSTIC_DISPLAY" >&2
        exit 73
    }
    chmod 600 "$DIAGNOSTIC" 2>/dev/null || :
    RUNTIME_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT" && pwd -P)
    TOOL_BIN_DIR=$RUNTIME_ROOT/bin
    TOO_BIN=$TOOL_BIN_DIR/too

    # Docker's default hostname is a twelve-character hexadecimal container ID.
    case "${HOSTNAME:-}" in
        ????????????) ;;
        *) fail "docker guest hostname is not a short container ID" 64 ;;
    esac
    case "$HOSTNAME" in
        *[!0-9a-f]*) fail "docker guest hostname is not a short container ID" 64 ;;
    esac
    export TOOLANG_SANDBOX_INSTANCE=$HOSTNAME
}

find_pip_python() {
    for CANDIDATE in python3 python; do
        if command -v "$CANDIDATE" >/dev/null 2>&1 &&
            "$CANDIDATE" -c \
                'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
                >>"$DIAGNOSTIC" &&
            "$CANDIDATE" -m pip --version >>"$DIAGNOSTIC"; then
            command -v "$CANDIDATE"
            return 0
        fi
    done
    return 1
}

probe_uv() {
    "$UV_BIN" python find --help >/dev/null &&
        "$UV_BIN" python install --help >/dev/null &&
        "$UV_BIN" tool install --help >/dev/null &&
        "$UV_BIN" --version
}

ensure_uv() {
    export UV_CACHE_DIR=$RUNTIME_ROOT/cache
    export UV_PYTHON_INSTALL_DIR=$RUNTIME_ROOT/python
    export UV_PYTHON_BIN_DIR=$RUNTIME_ROOT/python-bin
    export UV_TOOL_DIR=$RUNTIME_ROOT/tools
    export UV_TOOL_BIN_DIR=$TOOL_BIN_DIR
    export UV_NO_PROGRESS=1

    if UV_BIN=$(command -v uv) && UV_FACT=$(probe_uv); then
        progress uv.use "Using uv · ${UV_FACT#uv }"
        return
    fi

    progress uv.install.running "Installing uv..."
    INSTALLER=$RUNTIME_ROOT/install-uv.sh
    INSTALLER_URL="https://astral.sh/uv/$UV_VERSION/install.sh"
    if command -v curl >/dev/null 2>&1; then
        set -- curl -LsSf "$INSTALLER_URL" -o "$INSTALLER"
    elif command -v wget >/dev/null 2>&1; then
        set -- wget -qO "$INSTALLER" "$INSTALLER_URL"
    else
        fail "Unsupported image: provide Python 3.11+ with pip, uv, curl, or wget" 69
    fi
    "$@" || fail "Could not download uv" 69
    UV_BIN=$RUNTIME_ROOT/uv-bin/uv
    UV_UNMANAGED_INSTALL=$RUNTIME_ROOT/uv-bin sh "$INSTALLER" ||
        fail "Could not install uv" 69
    UV_FACT=$(probe_uv) || fail "Installed uv is unusable" 69
    progress uv.install.ok "Installed uv · ${UV_FACT#uv }"
}

prepare_python() {
    UV_BIN=
    if PYTHON_BIN=$(find_pip_python); then
        PYTHON_EVENT=python.use
        PYTHON_LABEL="Using Python"
    else
        ensure_uv
        progress python.install.running "Installing Python · $PYTHON_VERSION..."
        "$UV_BIN" python install "$PYTHON_VERSION" --no-progress ||
            fail "Could not install Python $PYTHON_VERSION" 69
        PYTHON_BIN=$("$UV_BIN" python find --managed-python "$PYTHON_VERSION" || :)
        if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
            fail "Installed Python $PYTHON_VERSION is unavailable" 69
        fi
        PYTHON_EVENT=python.install.ok
        PYTHON_LABEL="Installed Python"
    fi
    PYTHON_FACT=$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())') ||
        fail "Python is unusable" 69
    progress "$PYTHON_EVENT" "$PYTHON_LABEL · $PYTHON_FACT"
}

install_toolang() {
    if [ -n "$UV_BIN" ]; then
        set -- "$UV_BIN" tool install --quiet --no-progress --force --python "$PYTHON_BIN"
    else
        set -- "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input \
            --quiet --upgrade --force-reinstall --no-user
        export PIP_ROOT_USER_ACTION=ignore
    fi
    progress toolang.install.running "Installing Toolang · ${PACKAGE_SOURCE##*/}..."
    if ! "$@" "$PACKAGE_SOURCE"; then
        progress toolang.install.failed
        fail "Could not install Toolang"
    fi
    if [ -z "$UV_BIN" ]; then
        PIP_BIN_DIR=$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_path("scripts"))') ||
            fail "Could not locate Python scripts directory" 69
        if [ "$PIP_BIN_DIR" != "$TOOL_BIN_DIR" ]; then
            ln -sf "$PIP_BIN_DIR/too" "$TOO_BIN" ||
                fail "Could not expose Toolang executable" 69
        fi
    fi
    progress toolang.install.ok
}

validate_toolang() {
    progress toolang.check.running
    if [ ! -f "$TOO_BIN" ] || [ ! -x "$TOO_BIN" ]; then
        progress toolang.check.failed
        fail "Installed Toolang executable is unavailable" 69
    fi
    if ! "$TOO_BIN" _serve --help; then
        progress toolang.check.failed
        fail "The installed Toolang package cannot start the required AgentServer" 69
    fi
    progress toolang.check.ok
    if [ "$PROGRESS_EVENTS" = "-" ]; then
        TOOLANG_VERSION=$("$TOO_BIN" --version || :)
        progress toolang.version "Installed Toolang${TOOLANG_VERSION:+ · $TOOLANG_VERSION}"
    fi
}

prepare_workload() {
    if [ "$WORKLOAD_LOG" != "-" ]; then
        { : >>"$WORKLOAD_LOG" && chmod 600 "$WORKLOAD_LOG"; } ||
            fail "Could not open workload log" 73
    fi
    rm -f "$DIAGNOSTIC"
    case "$1:${2:-}" in
        too:_serve|toolang:_serve) progress server.running "Starting agent..." ;;
        *) progress server.running "Starting command..." ;;
    esac
}

# Bootstrap state and installer overrides stay out of the workload environment.
bootstrap() (
    exec 3>&2
    UV_VERSION="0.12.6"
    PYTHON_VERSION="3.13"
    if [ "$#" -lt 7 ]; then
        printf '%s\n' "usage: guest.sh DIAGNOSTIC DISPLAY EVENTS PACKAGE LOG -- COMMAND..." >&2
        exit 64
    fi
    DIAGNOSTIC=$1
    DIAGNOSTIC_DISPLAY=$2
    PROGRESS_EVENTS=$3
    PACKAGE_SOURCE=$4
    WORKLOAD_LOG=$5
    if [ "$6" != "--" ]; then
        printf '%s\n' "docker guest command separator is missing" >&2
        exit 64
    fi
    shift 6
    if [ "$(uname -s 2>/dev/null || :)" != "Linux" ]; then
        printf '%s\n' "docker guest requires a Linux image" >&2
        exit 64
    fi
    prepare_runtime
    # Keep messages on fd 3; installer output stays in the private diagnostic.
    exec >>"$DIAGNOSTIC" 2>&1
    prepare_python
    install_toolang
    validate_toolang
    prepare_workload "$@"
)

# Arguments: LOG -- COMMAND...
exec_workload() {
    if [ "$1" != "-" ]; then
        exec >>"$1" 2>&1
    fi
    shift 2
    case "$1" in
        too|toolang)
            shift
            set -- "$(tool_bin_dir)/too" "$@"
            ;;
    esac
    export TOOLANG_SANDBOX_INSTANCE=$HOSTNAME
    export PATH="$(tool_bin_dir)${PATH:+:$PATH}"
    exec "$@"
}

bootstrap "$@"
# Pass only LOG -- COMMAND... to the parent-shell launcher.
shift 4
exec_workload "$@"
