"""Canonical filesystem layout for one materialized agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

from .source import AcquiredSource
from .place_codes import (
    root_code,
    decode_place_code,
    roaming_place_code,
    roaming_timestamp_ms,
    visiting_place_code,
)

AgentPlacement = Literal["resident", "visiting", "roaming"]

IMPLICIT_WORKSPACE_NAME = "lab"
IMPLICIT_WORKSPACE_DIRECTORY = ".lab"
VISITING_BASE = Path("/tmp").resolve() / "toolang"
_AGENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def is_agent_name(name: str) -> bool:
    return _AGENT_NAME.fullmatch(name) is not None


def validate_agent_name(name: str) -> str:
    """Validate an explicit Agent Name without silently changing it."""
    if not is_agent_name(name):
        raise ValueError(
            f"invalid agent name {name!r}: use 1–64 lowercase ASCII letters, "
            "digits, or underscores, starting with a letter"
        )
    return name


def derive_agent_name(stem: str) -> str:
    """Normalize a source filename stem without truncation or hash suffixes."""
    if is_agent_name(stem):
        return stem
    name = re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")
    if name and name[0].isdigit():
        name = f"script_{name}"
    return validate_agent_name(name)


@dataclass(frozen=True, slots=True)
class AgentLayout:
    """Immutable paths for one agent under its resolved place."""

    place: Path
    name: str
    placement: AgentPlacement
    instance: str | None = None
    project_inputs: bool | None = None
    managed_membership: bool | None = None
    root: Path | None = field(default=None, compare=False)
    source_id: str | None = None
    place_code: str | None = None
    source_path: Path | None = field(default=None, compare=False)
    source_ref: str | None = field(default=None, compare=False)
    source_edit_lock: Path | None = field(default=None, compare=False)
    request_order_ns: int | None = field(default=None, compare=False)
    program_acquisition: AcquiredSource | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        place = self.place.expanduser().resolve()
        name = validate_agent_name(self.name)
        if self.placement not in {"resident", "visiting", "roaming"}:
            raise ValueError(f"invalid agent placement: {self.placement!r}")
        if self.place_code is not None:
            if self.placement == "resident":
                raise ValueError("place code does not match its placement")
            decode_place_code(
                self.place_code, marker="t" if self.placement == "visiting" else "s"
            )
        if self.instance is not None and (
            not self.instance or not self.instance.isalnum()
        ):
            raise ValueError("instance identity must be alphanumeric")
        if self.source_id is not None and (
            not isinstance(self.source_id, str)
            or len(self.source_id) not in {7, 10}
            or any(
                char not in "0123456789abcdefghjkmnpqrstvwxyz"
                for char in self.source_id
            )
        ):
            raise ValueError(
                "source identity must be 7 or 10 lowercase base32 characters"
            )
        object.__setattr__(self, "place", place)
        object.__setattr__(self, "name", name)
        if self.root is not None:
            object.__setattr__(self, "root", self.root.expanduser().resolve())
        if self.project_inputs is None:
            object.__setattr__(self, "project_inputs", self.placement == "roaming")
        if self.managed_membership is None:
            object.__setattr__(self, "managed_membership", self.placement == "resident")

    @classmethod
    def resident(cls, root: Path, name: str) -> AgentLayout:
        """Create a resident-agent layout under an explicit Toolang root."""

        return cls(place=root, name=name, placement="resident", root=root)

    @classmethod
    def visiting(cls, source: str, name: str, *, root: Path) -> AgentLayout:
        """Create the stable temporary layout for one remote Source Ref."""

        validate_agent_name(name)
        root = root.expanduser().resolve()
        token = visiting_place_code(source)
        return cls(
            place=VISITING_BASE / root_code(str(root)) / token,
            name=name,
            placement="visiting",
            root=root,
            source_id=token[1:],
            place_code=token,
            source_ref=source,
        )

    @classmethod
    def roaming(
        cls,
        source: Path,
        *,
        place_code: str | None = None,
        root: Path | None = None,
    ) -> AgentLayout:
        """Create the source-local layout for one roaming program."""

        source = source.expanduser().resolve()
        place = source.parent / ".toolang"
        if place_code is not None:
            if root is None:
                raise ValueError("a retained roaming place requires its root")
            place = place / root_code(str(root.expanduser().resolve())) / place_code
        return cls(
            place=place,
            name=derive_agent_name(source.stem),
            placement="roaming",
            source_id=roaming_place_code(str(source), 0)[1:8],
            place_code=place_code,
            source_path=source,
            root=root,
            request_order_ns=roaming_timestamp_ms(place_code) * 1_000_000
            if place_code is not None
            else None,
        )

    @property
    def address(self) -> str:
        return f"{self.name}@{self.place_code}" if self.place_code else self.name

    @property
    def home(self) -> Path:
        return self.place / "agents" / self.name

    @property
    def place_config(self) -> Path:
        return self.place / "config.toml"

    @property
    def place_env(self) -> Path:
        return self.place / ".env"

    @property
    def program(self) -> Path:
        return self.home / "agent.too"

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def env(self) -> Path:
        return self.home / ".env"

    @property
    def place_state(self) -> Path:
        return self.place / ".state" / "root"

    @property
    def home_state(self) -> Path:
        return self.home / ".state" / "home"

    @property
    def agent_state(self) -> Path:
        return self.home / ".state" / "agent"

    @property
    def place_edit_lock(self) -> Path:
        if self.source_edit_lock is not None:
            return self.source_edit_lock
        return place_edit_lock_path(
            (self.source_path.parent / ".toolang").resolve()
            if self.source_path is not None
            else self.place
        )

    @property
    def home_edit_lock(self) -> Path:
        return home_edit_lock_path(self.home)

    @property
    def place_runtime(self) -> Path:
        return self.place / ".runtime"

    @property
    def runtime(self) -> Path:
        return self.home / ".runtime"

    @property
    def runtime_status(self) -> Path:
        return self.home / ".runtime" / "status.json"

    @property
    def previous_sandbox_state(self) -> Path:
        return self.sandbox_home / "state.json"

    @property
    def legacy_sandbox_state(self) -> Path:
        """Return the former guest-writable sandbox control-state path."""

        return self.runtime / "sandbox.json"

    @property
    def run_store(self) -> Path:
        return self.runtime / "runs.db"

    @property
    def stats_store(self) -> Path:
        return self.runtime / "stats.db"

    @property
    def chore_store(self) -> Path:
        return self.run_store

    @property
    def runtime_log(self) -> Path:
        return self.runtime / "agent.log"

    def run_log(self, runnable: str | None, run_id: str) -> Path:
        """Return the log path for one script invocation."""

        return (
            self.runtime
            / "logs"
            / _safe_log_label(runnable or "default")
            / f"{run_id}.log"
        )

    def tool_room(self, plugin: str) -> Path:
        return self.runtime / "tools" / plugin

    @property
    def sandbox_home(self) -> Path:
        return self.place / ".sandbox" / self.name

    @property
    def sandbox_stage(self) -> Path:
        return self.place / ".sandbox" / self.name / "launches"


def place_edit_lock_path(place: Path) -> Path:
    return place / ".state" / "root" / "edit.lock"


def home_edit_lock_path(home: Path) -> Path:
    return home / ".state" / "home" / "edit.lock"


def _safe_log_label(value: str) -> str:
    text = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_"
        for char in value.strip()
    )
    return text.strip("._") or "default"


def ensure_scratch_workspace(home: Path) -> Path:
    """Create and validate one agent-owned implicit scratch workspace."""

    resolved_home = home.expanduser().resolve()
    if not resolved_home.is_dir():
        raise FileNotFoundError(f"agent home is not available: {resolved_home}")
    root = resolved_home / IMPLICIT_WORKSPACE_DIRECTORY
    if root.is_symlink():
        raise ValueError(f"implicit lab workspace must not be a symlink: {root}")
    root.mkdir(exist_ok=True)
    if not root.is_dir() or root.resolve().parent != resolved_home:
        raise ValueError(f"implicit lab workspace is not a directory: {root}")
    return root
