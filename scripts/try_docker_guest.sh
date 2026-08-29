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
        --keep) KEEP=1; shift ;;
        --) shift; COMMAND_SET=1; break ;;
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
GUEST_SOURCE=$REPOSITORY_DIR/src/toolang/plugin/sandboxes
for FILE in docker_guest.sh docker_guest.py; do
    [ -f "$GUEST_SOURCE/$FILE" ] || {
        printf 'Guest file not found · %s\n' "$GUEST_SOURCE/$FILE" >&2
        exit 66
    }
done
command -v docker >/dev/null 2>&1 || {
    printf '%s\n' "Docker is not available" >&2
    exit 69
}

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/toolang-docker-guest.XXXXXX") || exit 73
GUEST_DIR=$STAGE/guest
STATE_DIR=$STAGE/state
DIAGNOSTIC=$STATE_DIR/diagnostic.log
DIAGNOSTIC_DISPLAY=${TMPDIR:-/tmp}/toolang-docker-guest-${STAGE##*.}.log
CONTAINER_ID=
CONTAINER_NAME=toolang-guest-$$-${STAGE##*.}

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

resolve_wheel() {
    VALUE=$1
    if [ -f "$VALUE" ]; then
        case "${VALUE##*/}" in
            toolang-*.whl)
                CDPATH= cd -- "$(dirname -- "$VALUE")" &&
                    printf '%s/%s\n' "$(pwd)" "${VALUE##*/}"
                ;;
            *) return 1 ;;
        esac
        return
    fi
    [ -d "$VALUE" ] || return 1
    find "$VALUE" -type f -name 'toolang-*.whl' -print >"$STAGE/wheels"
    SELECTED=
    while IFS= read -r CANDIDATE; do
        if [ -z "$SELECTED" ] || [ "$CANDIDATE" -nt "$SELECTED" ]; then
            SELECTED=$CANDIDATE
        fi
    done <"$STAGE/wheels"
    [ -n "$SELECTED" ] || return 1
    CDPATH= cd -- "$(dirname -- "$SELECTED")" &&
        printf '%s/%s\n' "$(pwd)" "${SELECTED##*/}"
}

trap cleanup 0
trap interrupt INT TERM HUP
mkdir "$GUEST_DIR" "$STATE_DIR" || exit 73
cp "$GUEST_SOURCE/docker_guest.sh" "$GUEST_SOURCE/docker_guest.py" "$GUEST_DIR" || exit 73
chmod 755 "$GUEST_DIR/docker_guest.sh"
printf '%s\n' '# Generated guest environment' >"$GUEST_DIR/guest.env"
: >"$DIAGNOSTIC"
chmod 600 "$GUEST_DIR/guest.env" "$DIAGNOSTIC"

PACKAGE_SOURCE=toolang
if [ -n "$DEV" ]; then
    WHEEL=$(resolve_wheel "$DEV") || {
        printf 'No Toolang wheel found · %s\n' "$DEV" >&2
        exit 66
    }
    cp "$WHEEL" "$GUEST_DIR/${WHEEL##*/}" || exit 73
    PACKAGE_SOURCE=/tmp/toolang-guest/${WHEEL##*/}
fi

printf 'Creating container · %s...\n' "$IMAGE" >&2
CONTAINER_ID=$(docker create \
    --name "$CONTAINER_NAME" \
    --volume "$GUEST_DIR:/tmp/toolang-guest:ro" \
    --volume "$STATE_DIR:/tmp/toolang-guest-state" \
    "$IMAGE" \
    /bin/sh /tmp/toolang-guest/docker_guest.sh \
    /tmp/toolang-guest/docker_guest.py \
    /tmp/toolang-guest/guest.env \
    /tmp/toolang-guest-state/diagnostic.log \
    "$DIAGNOSTIC_DISPLAY" "$PACKAGE_SOURCE" - -- "$@") || exit $?

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
