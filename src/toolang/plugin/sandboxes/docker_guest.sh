#!/bin/sh

set -eu

UV_VERSION="0.12.6"
PYTHON_VERSION="3.13"

fail() {
    printf '%s\n' "$1" >&2
    printf 'See %s\n' "$DIAGNOSTIC_DISPLAY" >&2
    exit "${2:-1}"
}

if [ "$#" -lt 8 ]; then
    printf '%s\n' \
        "usage: docker_guest.sh HELPER GUEST_ENV DIAGNOSTIC DISPLAY PACKAGE LOG -- COMMAND..." \
        >&2
    exit 64
fi

HELPER=$1
GUEST_ENV=$2
DIAGNOSTIC=$3
DIAGNOSTIC_DISPLAY=$4
PACKAGE_SOURCE=$5
WORKLOAD_LOG=$6
shift 6
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
                >>"$DIAGNOSTIC" 2>&1 || fail "Could not download uv" 69
        elif command -v wget >/dev/null 2>&1; then
            wget -qO "$INSTALLER" "$INSTALLER_URL" \
                >>"$DIAGNOSTIC" 2>&1 || fail "Could not download uv" 69
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
    "$WORKLOAD_LOG" \
    "$PYTHON_BIN" \
    "$TOOL_BIN_DIR" \
    "$UV_MODE" \
    "$UV_EXECUTABLE" \
    "$UV_MODULE_DIR" \
    -- \
    "$@"
