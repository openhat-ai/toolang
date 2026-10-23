"""Explicit root and agent environment selection and process scopes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import cast

from dotenv.parser import parse_stream

from .config_sources import read_config
from .layout import AgentLayout

DEFAULT_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
)
DEFAULT_PICK = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnvironmentPolicy:
    keep: tuple[str, ...] = DEFAULT_KEEP
    pick: tuple[str, ...] = DEFAULT_PICK

    @classmethod
    def from_config(cls, config: Mapping[str, object], path: Path) -> EnvironmentPolicy:
        table = config.get("env", {})
        if not isinstance(table, dict) or set(table) - {"keep", "pick"}:
            raise ValueError(f"{path}: env accepts only keep and pick name arrays")
        table = cast(Mapping[str, object], table)
        values: dict[str, tuple[str, ...]] = {}
        for key, default in (("keep", DEFAULT_KEEP), ("pick", DEFAULT_PICK)):
            raw = table.get(key, list(default))
            if not isinstance(raw, list) or not all(
                isinstance(name, str) and _NAME.fullmatch(name) for name in raw
            ):
                raise ValueError(f"{path}: env.{key} must contain environment names")
            if len(raw) != len(set(raw)):
                raise ValueError(f"{path}: env.{key} contains duplicate names")
            values[key] = tuple(cast(list[str], raw))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ResolvedEnvironment:
    root: Path
    policy: EnvironmentPolicy
    values: Mapping[str, str] = field(repr=False)
    identity: str | None = None
    source: Path | None = None
    picked: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


_CURRENT: ContextVar[ResolvedEnvironment | None] = ContextVar(
    "toolang_environment", default=None
)


def current_environment() -> ResolvedEnvironment | None:
    return _CURRENT.get()


def environment_identity(layout: AgentLayout) -> str:
    source = (
        str(layout.source_path.resolve())
        if layout.source_path is not None
        else layout.source_ref or layout.address
    )
    return f"{layout.placement}:{layout.name if layout.placement == 'resident' else source}"


def apply_environment_file(path: Path, parent: Mapping[str, str]) -> dict[str, str]:
    """Parse bindings without expansion or diagnostics containing literal values."""
    result: dict[str, str] = {}
    seen: set[str] = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for binding in parse_stream(stream):
                name = binding.key
                if (
                    binding.error
                    or (name is not None and not _NAME.fullmatch(name))
                    or (binding.value is not None and "\x00" in binding.value)
                ):
                    raise ValueError(
                        f"{path}:{binding.original.line}: invalid environment binding"
                    )
                if name is None:
                    continue
                if name in seen:
                    raise ValueError(
                        f"{path}:{binding.original.line}: duplicate environment name"
                    )
                seen.add(name)
                if binding.value is not None:
                    result[name] = binding.value
                elif name in parent:
                    result[name] = parent[name]
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"could not read environment file: {path}") from exc
    return result


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def resolve_root_environment(
    root: Path, environ: Mapping[str, str]
) -> ResolvedEnvironment:
    root = root.expanduser().resolve()
    policy = EnvironmentPolicy.from_config(
        read_config(root / "config.toml"), root / "config.toml"
    )
    source = root / ".env"
    if _exists(source):
        values = {name: environ[name] for name in policy.keep if name in environ}
        values.update(apply_environment_file(source, environ))
    else:
        source = None
        values = dict(environ)
    return ResolvedEnvironment(root, policy, values, source=source)


def resolve_agent_environment(
    root: ResolvedEnvironment,
    layout: AgentLayout,
) -> ResolvedEnvironment:
    candidates: tuple[Path, ...] = ()
    if layout.placement == "resident":
        candidates = (root.root / f".{layout.name}.env", layout.env)
    elif layout.placement == "roaming" and layout.source_path is not None:
        # Nonresident root-file lookup awaits the approved reversible codec.
        candidates = (layout.source_path.resolve().parent / ".toolang.env",)
    source = next((path for path in candidates if _exists(path)), None)
    values = {
        name: root.values[name] for name in root.policy.keep if name in root.values
    }
    picked = None
    if source is not None:
        values.update(apply_environment_file(source, root.values))
    else:
        picked = next(
            (name for name in root.policy.pick if root.values.get(name, "").strip()),
            None,
        )
        if picked is not None:
            values[picked] = root.values[picked]
    return ResolvedEnvironment(
        root.root, root.policy, values, environment_identity(layout), source, picked
    )


@contextmanager
def environment_scope(environment: ResolvedEnvironment) -> Iterator[None]:
    """Install an invocation's environment until all of its work has stopped."""
    previous = dict(os.environ)
    token = _CURRENT.set(environment)
    logger.info(
        "environment.selected identity=%s source=%s pick=%s",
        environment.identity or "root",
        environment.source or "none",
        environment.picked or "none",
    )
    try:
        os.environ.clear()
        os.environ.update(environment.values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)
        _CURRENT.reset(token)
