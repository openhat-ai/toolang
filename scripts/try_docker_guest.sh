#!/bin/sh

set -u

usage() {
    printf '%s\n' \
        "usage: scripts/try_docker_guest.sh IMAGE [--dev WHEEL_OR_DIST] [--keep] [-- COMMAND...]" \
        >&2
    exit 64
}

[ "$#" -ge 1 ] || usage
IMAGE=$1
shift
DEV=
KEEP=0
COMMAND_SET=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dev)
            [ "$#" -ge 2 ] || usage
            DEV=$2
            shift 2
            ;;
        --keep)
            KEEP=1
            shift
            ;;
        --)
            shift
            COMMAND_SET=1
            break
            ;;
        *) usage ;;
    esac
done

if [ "$COMMAND_SET" -eq 0 ]; then
    set -- too --version
elif [ "$#" -eq 0 ]; then
    usage
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
GUEST_SCRIPT=$REPOSITORY_DIR/src/toolang/plugin/sandboxes/docker_guest.sh
[ -f "$GUEST_SCRIPT" ] || {
    printf 'Guest script not found · %s\n' "$GUEST_SCRIPT" >&2
    exit 66
}
command -v docker >/dev/null 2>&1 || {
    printf '%s\n' "Docker is not available" >&2
    exit 69
}

resolve_wheel() {
    VALUE=$1
    if [ -f "$VALUE" ]; then
        case "${VALUE##*/}" in
            toolang-*.whl) CDPATH= cd -- "$(dirname -- "$VALUE")" && printf '%s/%s\n' "$(pwd)" "${VALUE##*/}" ;;
            *) return 1 ;;
        esac
        return
    fi
    [ -d "$VALUE" ] || return 1
    CANDIDATES=$STAGE/wheels
    find "$VALUE" -type f -name 'toolang-*.whl' -print >"$CANDIDATES"
    SELECTED=
    while IFS= read -r CANDIDATE; do
        if [ -z "$SELECTED" ] || [ "$CANDIDATE" -nt "$SELECTED" ]; then
            SELECTED=$CANDIDATE
        fi
    done <"$CANDIDATES"
    [ -n "$SELECTED" ] || return 1
    CDPATH= cd -- "$(dirname -- "$SELECTED")" && printf '%s/%s\n' "$(pwd)" "${SELECTED##*/}"
}

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/toolang-docker-guest.XXXXXX") || exit 73
CONTAINER_ID=
CONTAINER_NAME=toolang-guest-$$-${STAGE##*.}
STAGED_GUEST=$STAGE/docker_guest.sh
GUEST_ENV=$STAGE/guest.env
DIAGNOSTIC_DIR=$STAGE/state
DIAGNOSTIC=$DIAGNOSTIC_DIR/diagnostic.log
DIAGNOSTIC_DISPLAY=${TMPDIR:-/tmp}/toolang-docker-guest-${STAGE##*.}.log

cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        [ -z "$CONTAINER_ID" ] || printf 'Container retained · %s\n' "$CONTAINER_ID" >&2
        printf 'Staging retained · %s\n' "$STAGE" >&2
        return
    fi
    if [ -n "$CONTAINER_ID" ]; then
        docker rm --force "$CONTAINER_ID" >/dev/null 2>&1 || :
    fi
    case "$STAGE" in
        "${TMPDIR:-/tmp}"/toolang-docker-guest.*) rm -rf -- "$STAGE" ;;
    esac
}

preserve_diagnostic() {
    if [ -s "$DIAGNOSTIC" ]; then
        cp "$DIAGNOSTIC" "$DIAGNOSTIC_DISPLAY" || return
        chmod 600 "$DIAGNOSTIC_DISPLAY" 2>/dev/null || :
        printf 'Diagnostic retained · %s\n' "$DIAGNOSTIC_DISPLAY" >&2
    fi
}

interrupt() {
    if [ -n "$CONTAINER_ID" ]; then
        docker stop "$CONTAINER_ID" >/dev/null 2>&1 || :
    fi
    preserve_diagnostic
    exit 130
}

trap cleanup 0
trap interrupt INT TERM HUP

mkdir "$DIAGNOSTIC_DIR" || exit 73
cp "$GUEST_SCRIPT" "$STAGED_GUEST" || exit 73
chmod 755 "$STAGED_GUEST"
printf '%s\n' '# Generated guest environment' >"$GUEST_ENV"
: >"$DIAGNOSTIC"
chmod 600 "$GUEST_ENV" "$DIAGNOSTIC"

PACKAGE_SOURCE=toolang
STAGED_WHEEL=
if [ -n "$DEV" ]; then
    WHEEL=$(resolve_wheel "$DEV") || {
        printf 'No Toolang wheel found · %s\n' "$DEV" >&2
        exit 66
    }
    STAGED_WHEEL=$STAGE/${WHEEL##*/}
    cp "$WHEEL" "$STAGED_WHEEL" || exit 73
    PACKAGE_SOURCE=/tmp/toolang-guest/${WHEEL##*/}
fi

printf 'Creating container · %s...\n' "$IMAGE" >&2
if [ -n "$STAGED_WHEEL" ]; then
    CONTAINER_ID=$(docker create \
        --name "$CONTAINER_NAME" \
        --volume "$STAGED_GUEST:/tmp/toolang-guest/docker_guest.sh:ro" \
        --volume "$GUEST_ENV:/tmp/toolang-guest/guest.env:ro" \
        --volume "$DIAGNOSTIC_DIR:/tmp/toolang-guest-state" \
        --volume "$STAGED_WHEEL:$PACKAGE_SOURCE:ro" \
        "$IMAGE" \
        /bin/sh \
        /tmp/toolang-guest/docker_guest.sh \
        /tmp/toolang-guest/guest.env \
        /tmp/toolang-guest-state/diagnostic.log \
        "$DIAGNOSTIC_DISPLAY" \
        "$PACKAGE_SOURCE" \
        -- \
        "$@") || exit $?
else
    CONTAINER_ID=$(docker create \
        --name "$CONTAINER_NAME" \
        --volume "$STAGED_GUEST:/tmp/toolang-guest/docker_guest.sh:ro" \
        --volume "$GUEST_ENV:/tmp/toolang-guest/guest.env:ro" \
        --volume "$DIAGNOSTIC_DIR:/tmp/toolang-guest-state" \
        "$IMAGE" \
        /bin/sh \
        /tmp/toolang-guest/docker_guest.sh \
        /tmp/toolang-guest/guest.env \
        /tmp/toolang-guest-state/diagnostic.log \
        "$DIAGNOSTIC_DISPLAY" \
        "$PACKAGE_SOURCE" \
        -- \
        "$@") || exit $?
fi

SHORT_ID=$(printf '%.12s' "$CONTAINER_ID")
printf 'Created container · %s\n' "$SHORT_ID" >&2
printf 'Starting container...\n' >&2
docker start --attach "$CONTAINER_ID"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
    preserve_diagnostic
fi
printf 'Container stopped · %s\n' "$SHORT_ID" >&2
exit "$STATUS"
