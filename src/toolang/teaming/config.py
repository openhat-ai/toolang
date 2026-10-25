"""Teaming configuration formats; no file reads, environment, or driver imports."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from hashlib import sha256
import json
from urllib.parse import urlsplit, parse_qs, unquote

from toolang.common.place_codes import root_code
from toolang.common.urls import replace_guest_loopback
from .errors import TeamingError
from .schemas import HubConnection, identifier


@dataclass(frozen=True)
class BackendConfig:
    url: str
    root: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, str)
            or not self.root
            or not Path(self.root).is_absolute()
        ):
            raise TeamingError("Backend root must be an explicit canonical host path")
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
            raise TeamingError("Invalid hub.backend.url") from exc

    @property
    def root_code(self) -> str:
        return root_code(self.root)

    @property
    def identity(self) -> str:
        material = json.dumps(
            ["hub-backend:v1", self.url, self.root],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(material.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class TeamingRootConfig:
    human: str
    hub: str = "http://127.0.0.1:7001"
    enabled: bool = True

    def connection(self, *, gateway: str | None = None) -> HubConnection:
        return HubConnection(replace_guest_loopback(self.hub, gateway))


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
    _fields(raw, {"human", "enabled", "hub"}, "teaming", source, "root")
    human = raw.get("human", default_human)
    if not isinstance(human, str):
        raise TeamingError(f"{source}: teaming.human must be text")
    try:
        identifier(human)
    except TeamingError as exc:
        raise TeamingError(f"{source}: teaming.human: {exc}") from exc
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TeamingError(f"{source}: teaming.enabled must be a boolean")
    url = raw.get("hub", "http://127.0.0.1:7001")
    try:
        if not isinstance(url, str) or url != url.strip():
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.hostname in {"0.0.0.0", "::"}
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError
    except ValueError as exc:
        raise TeamingError(f"{source}: invalid teaming.hub URL") from exc
    return TeamingRootConfig(f"human:{human}", url.rstrip("/"), enabled)


def home_config(config: Mapping[str, object], *, source: Path) -> TeamingHomeConfig:
    raw = _table(config.get("teaming", {}), "teaming", source)
    _fields(raw, {"enabled"}, "teaming", source, "home")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TeamingError(f"{source}: teaming.enabled must be a boolean")
    return TeamingHomeConfig(enabled)
