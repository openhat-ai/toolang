#!/bin/sh

set -eu
umask 077

fail() {
    printf '%s\nSee %s\n' "$1" "$2" >&2
    exit "${3:-1}"
}

progress() {
    if [ "$1" = "-" ]; then
        if [ "$#" -ge 3 ]; then
            printf '%s\n' "$3" >&2
        fi
    else
        { printf '%s\n' "$2" >>"$1"; } 2>/dev/null || :
    fi
}

prepare_runtime() {
    mkdir -p "$RUNTIME_ROOT/bin" || {
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
        *) fail "docker guest hostname is not a short container ID" "$DIAGNOSTIC_DISPLAY" 64 ;;
    esac
    case "$HOSTNAME" in
        *[!0-9a-f]*) fail "docker guest hostname is not a short container ID" "$DIAGNOSTIC_DISPLAY" 64 ;;
    esac
    export TOOLANG_SANDBOX_INSTANCE=$HOSTNAME
}

find_pip_python() {
    for CANDIDATE in python3 python; do
        if command -v "$CANDIDATE" >/dev/null 2>&1 &&
            "$CANDIDATE" -c \
                'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
                >>"$DIAGNOSTIC" 2>&1 &&
            "$CANDIDATE" -m pip --version >>"$DIAGNOSTIC" 2>&1; then
            command -v "$CANDIDATE"
            return 0
        fi
    done
    return 1
}

uv_capable() {
    "$UV_BIN" --version >/dev/null 2>>"$DIAGNOSTIC" &&
        "$UV_BIN" python find --help >/dev/null 2>>"$DIAGNOSTIC" &&
        "$UV_BIN" python install --help >/dev/null 2>>"$DIAGNOSTIC" &&
        "$UV_BIN" tool install --help >/dev/null 2>>"$DIAGNOSTIC"
}

ensure_uv() {
    export UV_CACHE_DIR=$RUNTIME_ROOT/cache
    export UV_PYTHON_INSTALL_DIR=$RUNTIME_ROOT/python
    export UV_PYTHON_BIN_DIR=$RUNTIME_ROOT/python-bin
    export UV_TOOL_DIR=$RUNTIME_ROOT/tools
    export UV_TOOL_BIN_DIR=$TOOL_BIN_DIR
    export UV_NO_PROGRESS=1

    if UV_BIN=$(command -v uv) && uv_capable; then
        UV_FACT=$("$UV_BIN" --version 2>>"$DIAGNOSTIC") ||
            fail "Existing uv is unusable" "$DIAGNOSTIC_DISPLAY" 69
        progress "$PROGRESS_EVENTS" uv.use "Using uv · ${UV_FACT#uv }"
        return
    fi

    progress "$PROGRESS_EVENTS" uv.install.running "Installing uv..."
    INSTALLER=$RUNTIME_ROOT/install-uv.sh
    INSTALLER_URL="https://astral.sh/uv/$UV_VERSION/install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$INSTALLER_URL" -o "$INSTALLER" >>"$DIAGNOSTIC" 2>&1 ||
            fail "Could not download uv" "$DIAGNOSTIC_DISPLAY" 69
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$INSTALLER" "$INSTALLER_URL" >>"$DIAGNOSTIC" 2>&1 ||
            fail "Could not download uv" "$DIAGNOSTIC_DISPLAY" 69
    else
        fail "Unsupported image: provide Python 3.11+ with pip, uv, curl, or wget" \
            "$DIAGNOSTIC_DISPLAY" 69
    fi
    UV_BIN=$RUNTIME_ROOT/uv-bin/uv
    UV_UNMANAGED_INSTALL=$RUNTIME_ROOT/uv-bin sh "$INSTALLER" >>"$DIAGNOSTIC" 2>&1 ||
        fail "Could not install uv" "$DIAGNOSTIC_DISPLAY" 69
    uv_capable || fail "Installed uv is unusable" "$DIAGNOSTIC_DISPLAY" 69
    UV_FACT=$("$UV_BIN" --version 2>>"$DIAGNOSTIC") ||
        fail "Installed uv is unusable" "$DIAGNOSTIC_DISPLAY" 69
    progress "$PROGRESS_EVENTS" uv.install.ok "Installed uv · ${UV_FACT#uv }"
}

prepare_python() {
    UV_BIN=
    if PYTHON_BIN=$(find_pip_python); then
        PYTHON_EVENT=python.use
        PYTHON_LABEL="Using Python"
    else
        ensure_uv
        progress "$PROGRESS_EVENTS" python.install.running "Installing Python · $PYTHON_VERSION..."
        "$UV_BIN" python install "$PYTHON_VERSION" --no-progress >>"$DIAGNOSTIC" 2>&1 ||
            fail "Could not install Python $PYTHON_VERSION" "$DIAGNOSTIC_DISPLAY" 69
        PYTHON_BIN=$(
            "$UV_BIN" python find --managed-python "$PYTHON_VERSION" 2>>"$DIAGNOSTIC" || :
        )
        if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
            fail "Installed Python $PYTHON_VERSION is unavailable" "$DIAGNOSTIC_DISPLAY" 69
        fi
        PYTHON_EVENT=python.install.ok
        PYTHON_LABEL="Installed Python"
    fi
    PYTHON_FACT=$(
        "$PYTHON_BIN" -c 'import platform; print(platform.python_version())' 2>>"$DIAGNOSTIC"
    ) || fail "Python is unusable" "$DIAGNOSTIC_DISPLAY" 69
    progress "$PROGRESS_EVENTS" "$PYTHON_EVENT" "$PYTHON_LABEL · $PYTHON_FACT"
}

install_toolang() {
    if [ -n "$UV_BIN" ]; then
        set -- "$UV_BIN" tool install --quiet --no-progress --force --python "$PYTHON_BIN"
    else
        set -- "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input \
            --quiet --upgrade --force-reinstall --no-user
        export PIP_ROOT_USER_ACTION=ignore
    fi
    progress "$PROGRESS_EVENTS" toolang.install.running "Installing Toolang · ${PACKAGE_SOURCE##*/}..."
    if ! "$@" "$PACKAGE_SOURCE" >>"$DIAGNOSTIC" 2>&1; then
        progress "$PROGRESS_EVENTS" toolang.install.failed
        fail "Could not install Toolang" "$DIAGNOSTIC_DISPLAY" 1
    fi
    if [ -z "$UV_BIN" ]; then
        PIP_BIN_DIR=$(
            "$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_path("scripts"))' 2>>"$DIAGNOSTIC"
        ) || fail "Could not locate Python scripts directory" "$DIAGNOSTIC_DISPLAY" 69
        if [ "$PIP_BIN_DIR" != "$TOOL_BIN_DIR" ]; then
            ln -sf "$PIP_BIN_DIR/too" "$TOO_BIN" >>"$DIAGNOSTIC" 2>&1 ||
                fail "Could not expose Toolang executable" "$DIAGNOSTIC_DISPLAY" 69
        fi
    fi
    progress "$PROGRESS_EVENTS" toolang.install.ok
}

validate_toolang() {
    progress "$PROGRESS_EVENTS" toolang.check.running
    if [ ! -f "$TOO_BIN" ] || [ ! -x "$TOO_BIN" ]; then
        progress "$PROGRESS_EVENTS" toolang.check.failed
        fail "Installed Toolang executable is unavailable" "$DIAGNOSTIC_DISPLAY" 69
    fi
    if ! "$TOO_BIN" _serve --help >>"$DIAGNOSTIC" 2>&1; then
        progress "$PROGRESS_EVENTS" toolang.check.failed
        fail "The installed Toolang package cannot start the required AgentServer" "$DIAGNOSTIC_DISPLAY" 69
    fi
    progress "$PROGRESS_EVENTS" toolang.check.ok
    if [ "$PROGRESS_EVENTS" = "-" ]; then
        TOOLANG_VERSION=$("$TOO_BIN" --version 2>>"$DIAGNOSTIC" || :)
        progress - toolang.version "Installed Toolang${TOOLANG_VERSION:+ · $TOOLANG_VERSION}"
    fi
}

# Only installation runs in a child: its variables and UV overrides must not
# replace arbitrary image ENV values in the final workload.
bootstrap() (
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
    shift 5
    if [ "$1" != "--" ]; then
        printf '%s\n' "docker guest command separator is missing" >&2
        exit 64
    fi
    shift
    if [ "$(uname -s 2>/dev/null || :)" != "Linux" ]; then
        printf '%s\n' "docker guest requires a Linux image" >&2
        exit 64
    fi
    RUNTIME_ROOT=${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}
    prepare_runtime
    prepare_python
    install_toolang
    validate_toolang
)

exec_workload() {
    # Keep launch state in positional parameters so image ENV names cannot be
    # overwritten by shell implementation variables.
    if [ "$5" != "-" ]; then
        ( : >>"$5" && chmod 600 "$5" ) 2>>"$1" ||
            fail "Could not open workload log" "$2" 73
    fi
    rm -f "$1"
    case "$7:${8:-}" in
        too:_serve|toolang:_serve) progress "$3" server.running "Starting agent..." ;;
        *) progress "$3" server.running "Starting command..." ;;
    esac
    if [ "$5" != "-" ]; then
        exec >>"$5" 2>&1
    fi
    shift 6
    case "$1" in
        too|toolang)
            shift
            set -- "${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}/bin/too" "$@"
            ;;
    esac
    export TOOLANG_SANDBOX_INSTANCE=$HOSTNAME
    export PATH="${TOOLANG_GUEST_RUNTIME:-${TMPDIR:-/tmp}/toolang-runtime}/bin${PATH:+:$PATH}"
    exec "$@"
}

bootstrap "$@"
exec_workload "$@"
