"""Explicit root and agent environment selection and process scopes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import errno
import logging
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import cast

from dotenv.parser import parse_stream

from .config_sources import read_config
from .flatname import encode_flatname
from .layout import AgentLayout

DEFAULT_KEEP = (
    "COLORTERM",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "SHELL",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
)
DEFAULT_PICK = (
    "ALIBABA_CODING_PLAN_API_KEY",
    "ALIBABA_TOKEN_PLAN_API_KEY",
    "ANTHROPIC_API_KEY",
    "ARK_API_KEY",
    "ARK_CODING_PLAN_API_KEY",
    "CEREBRAS_API_KEY",
    "COHERE_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GROQ_API_KEY",
    "KIMI_API_KEY",
    "LLAMA_API_KEY",
    "META_MODEL_API_KEY",
    "MINIMAX_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "NVIDIA_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "PERPLEXITY_API_KEY",
    "SILICONFLOW_API_KEY",
    "SILICONFLOW_CN_API_KEY",
    "STEPFUN_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
    "ZHIPU_API_KEY",
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HTTP_ENV_PREFIXES = tuple(
    f".{encode_flatname(scheme)}" for scheme in ("http://", "https://")
)
logger = logging.getLogger(__name__)

# Concrete process facts belong to the launcher, not authored dotenv bindings.
RUNTIME_ENV_NAMES = frozenset(
    {
        "HOSTNAME",
        "TOOLANG_HOST_GATEWAY",
        "TOOLANG_HOST_LAUNCH",
        "TOOLANG_ROOT",
        "TOOLANG_SANDBOX",
        "TOOLANG_SANDBOX_DESCRIPTION",
        "TOOLANG_SANDBOX_INSTANCE",
        "TOOLANG_WORKSPACE_MOUNTS",
        "TOOLANG_WORKSPACE_LOCATION",
    }
)


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
    identity: str | None = field(default=None, repr=False)
    source: Path | None = field(default=None, repr=False)
    picked: str | None = None
    guest_values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(
            self, "guest_values", MappingProxyType(dict(self.guest_values))
        )


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
    display_path = _display_path(path)
    try:
        if not path.is_file():
            raise ValueError(f"could not read environment file: {display_path}")
        with path.open(encoding="utf-8") as stream:
            for binding in parse_stream(stream):
                name = binding.key
                if (
                    binding.error
                    or (name is not None and not _NAME.fullmatch(name))
                    or (binding.value is not None and "\x00" in binding.value)
                ):
                    raise ValueError(
                        f"{display_path}:{binding.original.line}: invalid environment binding"
                    )
                if name is None:
                    continue
                if name in seen:
                    raise ValueError(
                        f"{display_path}:{binding.original.line}: duplicate environment name"
                    )
                seen.add(name)
                if binding.value is not None:
                    result[name] = binding.value
                elif name in parent:
                    result[name] = parent[name]
    except (OSError, UnicodeError):
        raise ValueError(f"could not read environment file: {display_path}") from None
    return result


def _display_path(path: Path) -> str:
    # Encoded URLs can contain userinfo, signed queries, and fragments. Keep
    # matching exact, but never print their reversible filename spelling.
    if path.name.lower().startswith(_HTTP_ENV_PREFIXES):
        return str(path.parent / ".[HTTP source redacted].env")
    return str(path)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        # Only a component beyond the filesystem's limit proves absence;
        # ENAMETOOLONG can also mean an uninspectable total path length.
        if exc.errno == errno.ENAMETOOLONG:
            try:
                maximum = os.pathconf(path.parent, "PC_NAME_MAX")
            except (FileNotFoundError, NotADirectoryError):
                return False
            except OSError:
                maximum = -1
            if 0 <= maximum < len(os.fsencode(path.name)):
                return False
        raise ValueError(
            f"could not inspect environment file: {_display_path(path)}"
        ) from None
    return True


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


def agent_environment_paths(
    root: Path,
    layout: AgentLayout,
) -> tuple[Path, ...]:
    """Return policy files in precedence order for one source identity."""

    if layout.placement == "resident":
        return (root / f".{layout.name}.env", layout.env)
    if layout.placement == "roaming" and layout.source_path is not None:
        source_path = layout.source_path.resolve()
        return (
            root / f".{encode_flatname(str(source_path))}.env",
            source_path.parent / ".toolang.env",
        )
    if layout.placement == "visiting" and layout.source_ref is not None:
        return (root / f".{encode_flatname(layout.source_ref)}.env",)
    return ()


def resolve_agent_environment(
    root: ResolvedEnvironment,
    layout: AgentLayout,
) -> ResolvedEnvironment:
    candidates = agent_environment_paths(root.root, layout)
    source = next((path for path in candidates if _exists(path)), None)
    values = {
        name: root.values[name] for name in root.policy.keep if name in root.values
    }
    picked = None
    grants: dict[str, str] = {}
    if source is not None:
        grants = apply_environment_file(source, root.values)
    else:
        picked = next(
            (name for name in root.policy.pick if root.values.get(name, "").strip()),
            None,
        )
        if picked is not None:
            grants[picked] = root.values[picked]
    values.update(grants)
    return ResolvedEnvironment(
        root.root,
        root.policy,
        values,
        environment_identity(layout),
        source,
        picked,
        guest_values=grants,
    )


@contextmanager
def environment_scope(environment: ResolvedEnvironment) -> Iterator[None]:
    """Install an invocation's environment until all of its work has stopped."""
    previous = dict(os.environ)
    token = _CURRENT.set(environment)
    try:
        os.environ.clear()
        os.environ.update(environment.values)
        logger.info(
            "environment.selected identity=%s source=%s pick=%s",
            "visiting"
            if environment.identity and environment.identity.startswith("visiting:")
            else environment.identity or "root",
            _display_path(environment.source)
            if environment.source is not None
            else "none",
            environment.picked or "none",
        )
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)
        _CURRENT.reset(token)
