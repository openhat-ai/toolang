"""Shared execution vocabulary and scalar types."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, TypeAlias

from pydantic_core import core_schema


RunId = str
PolicyGroup = Literal["allow", "default", "limit"]
PolicyValue: TypeAlias = tuple[str, ...] | str | int | Decimal | None
ALLOW_POLICY_FIELDS = frozenset(
    {
        "models",
        "tools",
        "caps",
        "psyches",
        "skills",
        "services",
        "prompts",
    }
)
DEFAULT_POLICY_FIELDS = frozenset({"model", "runnable"})
LIMIT_POLICY_FIELDS = frozenset(
    {"agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"}
)


@dataclass(frozen=True, slots=True)
class PolicyCommand:
    """One canonical caller command applied to execution policy."""

    group: PolicyGroup
    field: str
    value: PolicyValue

    def __post_init__(self) -> None:
        if self.group not in {"allow", "default", "limit"}:
            raise ValueError(f"unknown policy command group: {self.group}")
        if not self.field or self.field != self.field.strip():
            raise ValueError("policy command requires a canonical field")
        fields = {
            "allow": ALLOW_POLICY_FIELDS,
            "default": DEFAULT_POLICY_FIELDS,
            "limit": LIMIT_POLICY_FIELDS,
        }[self.group]
        if self.field not in fields:
            raise ValueError(f"unknown {self.group} field: {self.field}")
        if self.group == "allow":
            if self.value is not None and not (
                isinstance(self.value, tuple)
                and all(isinstance(item, str) for item in self.value)
            ):
                raise TypeError("allow policy value must be selectors, all, or none")
            return
        if self.group == "default":
            if self.value is not None and not isinstance(self.value, str):
                raise TypeError("default policy value must be a string or none")
            return
        if self.field == "cost":
            if self.value is not None and not isinstance(self.value, Decimal):
                raise TypeError("limit cost policy value must be a decimal or none")
        elif self.value is not None and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise TypeError(
                f"limit {self.field} policy value must be an integer or none"
            )


@dataclass(frozen=True, slots=True)
class StepPath:
    """Globally address one step within its owning run."""

    run: RunId
    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.run or "/" in self.run:
            raise ValueError(f"invalid step run id: {self.run!r}")
        if not self.indices or any(index < 0 for index in self.indices):
            raise ValueError("step path requires non-negative indices")

    @classmethod
    def parse(cls, value: StepPath | str) -> StepPath:
        """Parse one canonical step path."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("step path must be a string")
        run, separator, suffix = value.partition("/")
        if not separator or not run or not suffix:
            raise ValueError(f"invalid step path: {value!r}")
        raw_indices = suffix.split("/")
        if any(
            not item.isascii()
            or not item.isdigit()
            or str(int(item)) != item
            for item in raw_indices
        ):
            raise ValueError(f"invalid step path: {value!r}")
        return cls(run=run, indices=tuple(int(item) for item in raw_indices))

    @classmethod
    def from_local(cls, run: RunId, path: str) -> StepPath:
        """Build one step path from separately stored run and local path."""

        return cls.parse(f"{run}/{path}")

    @property
    def local(self) -> str:
        """Return the run-local index path."""

        return "/".join(str(index) for index in self.indices)

    @property
    def parent(self) -> StepPath | None:
        """Return the enclosing step within the same run."""

        if len(self.indices) == 1:
            return None
        return StepPath(self.run, self.indices[:-1])

    @property
    def index(self) -> int:
        """Return the final step index."""

        return self.indices[-1]

    def child(self, index: int) -> StepPath:
        """Return one direct child step."""

        return StepPath(self.run, (*self.indices, index))

    def __str__(self) -> str:
        return f"{self.run}/{self.local}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(
                cls.parse,
                core_schema.str_schema(),
            ),
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(cls),
                    core_schema.no_info_after_validator_function(
                        cls.parse,
                        core_schema.str_schema(),
                    ),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str,
                return_schema=core_schema.str_schema(),
            ),
        )


RunStatus = Literal["pending", "running", "finished", "failed", "canceled"]
StepStatus = Literal["running", "finished", "failed", "canceled"]
ControlStatus = Literal["pending", "finished", "canceled", "failed"]

StepKind = Literal[
    "run",
    "agent",
    "human",
    "model",
    "tool",
    "par",
    "loop",
    "system",
]
ControlTiming = Literal["immediate", "next_step", "next_call"]
RunControlKind = Literal["start", "rerun", "retry", "steer", "stop"]
ThreadControlKind = Literal["create", "fork", "rewind"]
ThreadPeerType = Literal["user", "agent"]


class ThreadPrefix(StrEnum):
    """Canonical prefixes for locally issued thread ids."""

    SCRIPT = "script"
    WEB = "web"
    TERM = "term"
