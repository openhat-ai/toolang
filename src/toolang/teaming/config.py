"""Teaming configuration formats; no file reads, environment, or driver imports."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from hashlib import sha256
from urllib.parse import urlsplit, parse_qs, unquote

from .errors import TeamingError
from .schemas import identifier


@dataclass(frozen=True)
class BackendConfig:
    url: str

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"redis", "rediss", "valkey", "valkeys", "unix"}:
                raise ValueError
            if parsed.scheme == "unix":
                if not parsed.path or parsed.netloc:
                    raise ValueError
            else:
                if not parsed.hostname or (
                    parsed.port is not None and not 1 <= parsed.port <= 65535
                ):
                    raise ValueError
                database = unquote(parsed.path).removeprefix("/")
                if database and (not database.isascii() or not database.isdigit()):
                    raise ValueError
            if parsed.fragment:
                raise ValueError
            for database in parse_qs(parsed.query).get("db", []):
                if not database.isascii() or not database.isdigit():
                    raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise TeamingError("Invalid teaming.backend.url") from exc

    @property
    def identity(self) -> str:
        return sha256(self.url.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class TeamingRootConfig:
    human: str
    backend: BackendConfig
    hub_port: int


@dataclass(frozen=True)
class TeamingHomeConfig:
    enabled: bool


DEFAULT_BACKEND_URL = "redis://localhost:6379/0"


def _table(value: object, path: str, source: Path) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TeamingError(f"{source}: {path} must be a table")
    return cast(Mapping[str, object], value)


def _fields(
    raw: Mapping[str, object], allowed: set[str], path: str, source: Path, scope: str
) -> None:
    for key in raw:
        if key not in allowed:
            raise TeamingError(
                f"{source}: {path}.{key} is not allowed in {scope} scope"
            )


def root_config(
    config: Mapping[str, object], *, source: Path, default_human: str
) -> TeamingRootConfig:
    raw = _table(config.get("teaming", {}), "teaming", source)
    _fields(raw, {"human", "backend", "hub"}, "teaming", source, "root")
    human = raw.get("human", default_human)
    if not isinstance(human, str):
        raise TeamingError(f"{source}: teaming.human must be text")
    try:
        identifier(human)
    except TeamingError as exc:
        raise TeamingError(f"{source}: teaming.human: {exc}") from exc
    backend = _table(raw.get("backend", {}), "teaming.backend", source)
    _fields(backend, {"url"}, "teaming.backend", source, "root")
    url = backend.get("url", DEFAULT_BACKEND_URL)
    if not isinstance(url, str):
        raise TeamingError(f"{source}: teaming.backend.url must be text")
    hub = _table(raw.get("hub", {}), "teaming.hub", source)
    _fields(hub, {"port"}, "teaming.hub", source, "root")
    port = hub.get("port", 7000)
    if type(port) is not int or not 1 <= port <= 65535:
        raise TeamingError(f"{source}: teaming.hub.port must be an integer in 1..65535")
    try:
        concrete = BackendConfig(url)
    except TeamingError as exc:
        raise TeamingError(f"{source}: {exc}") from exc
    return TeamingRootConfig(f"human:{human}", concrete, port)


def home_config(config: Mapping[str, object], *, source: Path) -> TeamingHomeConfig:
    raw = _table(config.get("teaming", {}), "teaming", source)
    _fields(raw, {"enabled"}, "teaming", source, "home")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TeamingError(f"{source}: teaming.enabled must be a boolean")
    return TeamingHomeConfig(enabled)
