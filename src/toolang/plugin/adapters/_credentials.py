"""Select credentials from explicitly supplied route environment values."""

from collections.abc import Mapping
from toolang.base.types.model import ResolvedEnv

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_PAT", "_TOKEN")


def _env_value(environ: Mapping[str, str], name: str) -> bool:
    return bool(environ.get(name, "").strip())


def credential_value(
    env: ResolvedEnv | None,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Select one opaque credential value from a satisfied env rule."""

    names: tuple[str, ...] = ()
    for alternative in env or ():
        candidate = (alternative,) if isinstance(alternative, str) else alternative
        if all(_env_value(environ, name) for name in candidate):
            names = candidate
            break
    credential = next(
        (name for name in names if name.endswith(_CREDENTIAL_SUFFIXES)),
        names[-1] if names else None,
    )
    return environ.get(credential) if credential is not None else None
