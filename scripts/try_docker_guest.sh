#!/bin/sh
set -eu

usage() {
  echo "Usage: try_docker_guest.sh IMAGE [--dev WHEEL_OR_DIST] [--keep] [-- COMMAND...]" >&2
  exit 64
}

[ "$#" -ge 1 ] || usage
TOOLANG_IMAGE=$1
shift
TOOLANG_DEV=
TOOLANG_KEEP=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dev)
      [ "$#" -ge 2 ] || usage
      TOOLANG_DEV=$2
      shift 2
      ;;
    --keep)
      TOOLANG_KEEP=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *) usage ;;
  esac
done

[ "$#" -gt 0 ] || set -- too --version

TOOLANG_SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
TOOLANG_REPOSITORY=$(CDPATH='' cd "$TOOLANG_SCRIPT_DIR/.." && pwd)
TOOLANG_GUEST_CORE="$TOOLANG_REPOSITORY/src/toolang/plugin/sandboxes/docker_guest.sh"
[ -f "$TOOLANG_GUEST_CORE" ] || {
  echo "Docker guest core not found: $TOOLANG_GUEST_CORE" >&2
  exit 66
}

resolve_dev_wheel() (
  candidate=$1
  if [ -f "$candidate" ]; then
    case "$candidate" in
      *.whl) printf '%s\n' "$candidate" ;;
      *) return 1 ;;
    esac
    return
  fi
  [ -d "$candidate" ] || return 1
  set -- "$candidate"/toolang-*.whl
  [ "$#" -eq 1 ] && [ -f "$1" ] || return 1
  printf '%s\n' "$1"
)

TOOLANG_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/toolang-docker-guest.XXXXXXXX")
TOOLANG_CONTAINER=

# Invoked by the traps below.
# shellcheck disable=SC2329
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$TOOLANG_KEEP" = 1 ]; then
    if [ -n "$TOOLANG_CONTAINER" ]; then
      printf 'Container retained · %s\n' "$TOOLANG_CONTAINER" >&2
    fi
    printf 'Staging retained · %s\n' "$TOOLANG_STAGE" >&2
  else
    if [ -n "$TOOLANG_CONTAINER" ]; then
      echo "Removing container..." >&2
      docker rm --force "$TOOLANG_CONTAINER" >/dev/null 2>&1 || :
      echo "Removed container" >&2
    fi
    rm -rf "$TOOLANG_STAGE"
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cp "$TOOLANG_GUEST_CORE" "$TOOLANG_STAGE/docker_guest.sh"
chmod 700 "$TOOLANG_STAGE/docker_guest.sh"
printf '# Root and agent dotenv values\n\n# Filtered host process values\n' \
  >"$TOOLANG_STAGE/guest.env"
: >"$TOOLANG_STAGE/diagnostic.log"
: >"$TOOLANG_STAGE/instance"
chmod 600 \
  "$TOOLANG_STAGE/guest.env" \
  "$TOOLANG_STAGE/diagnostic.log" \
  "$TOOLANG_STAGE/instance"

TOOLANG_SOURCE=toolang
if [ -n "$TOOLANG_DEV" ]; then
  TOOLANG_WHEEL=$(resolve_dev_wheel "$TOOLANG_DEV") || {
    echo "--dev must select exactly one Toolang wheel" >&2
    exit 64
  }
  TOOLANG_WHEEL_NAME=${TOOLANG_WHEEL##*/}
  cp "$TOOLANG_WHEEL" "$TOOLANG_STAGE/$TOOLANG_WHEEL_NAME"
  TOOLANG_SOURCE="/toolang-bootstrap/$TOOLANG_WHEEL_NAME"
fi

command -v docker >/dev/null 2>&1 || {
  echo "Docker command not found" >&2
  exit 127
}

echo "Creating container..." >&2
TOOLANG_CONTAINER=$(docker create \
  --add-host host.docker.internal:host-gateway \
  --env TOOLANG_GUEST_RUNTIME=/tmp/toolang-runtime \
  --env TOOLANG_HOST_GATEWAY=host.docker.internal \
  --env "TOOLANG_SANDBOX=docker:$TOOLANG_IMAGE" \
  --volume "$TOOLANG_STAGE:/toolang-bootstrap:ro" \
  --volume "$TOOLANG_STAGE/diagnostic.log:/toolang-bootstrap/diagnostic.log" \
  "$TOOLANG_IMAGE" \
  /bin/sh \
  /toolang-bootstrap/docker_guest.sh \
  /toolang-bootstrap/guest.env \
  /toolang-bootstrap/diagnostic.log \
  "$TOOLANG_STAGE/diagnostic.log" \
  /toolang-bootstrap/instance \
  "$TOOLANG_SOURCE" \
  "$@")
if [ "${#TOOLANG_CONTAINER}" -ne 64 ]; then
  echo "Docker create returned an invalid container ID" >&2
  exit 1
fi
case "$TOOLANG_CONTAINER" in
  *[!0123456789abcdefABCDEF]*)
    echo "Docker create returned an invalid container ID" >&2
    exit 1
    ;;
esac
printf '%s\n' "$TOOLANG_CONTAINER" >"$TOOLANG_STAGE/instance"
printf 'Created container · %.12s\n' "$TOOLANG_CONTAINER" >&2

echo "Starting container..." >&2
set +e
docker start --attach "$TOOLANG_CONTAINER"
TOOLANG_STATUS=$?
set -e
printf 'Container exited · status %s\n' "$TOOLANG_STATUS" >&2
exit "$TOOLANG_STATUS"
