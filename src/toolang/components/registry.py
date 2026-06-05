"""Runtime component ids and expansion rules."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, cast

ComponentNamespace = Literal["router", "runner", "trigger"]
ComponentLeaf = Literal["chat", "manage", "inspect", "task", "chore", "file", "pulse", "poll", "watch"]
ComponentName = Literal[
    "router.chat",
    "router.manage",
    "router.inspect",
    "runner.chat",
    "runner.task",
    "runner.chore",
    "runner.file",
    "trigger.pulse",
    "trigger.poll",
    "trigger.watch",
    "trigger.file",
]

ROUTER_COMPONENTS: tuple[ComponentName, ...] = ("router.chat", "router.manage", "router.inspect")
RUNNER_COMPONENTS: tuple[ComponentName, ...] = ("runner.chat", "runner.task", "runner.chore", "runner.file")
TRIGGER_COMPONENTS: tuple[ComponentName, ...] = (
    "trigger.pulse",
    "trigger.poll",
    "trigger.watch",
    "trigger.file",
)
ALL_COMPONENTS: tuple[ComponentName, ...] = (
    *ROUTER_COMPONENTS,
    *RUNNER_COMPONENTS,
    *TRIGGER_COMPONENTS,
)
DEFAULT_ENABLED_COMPONENTS: tuple[ComponentName, ...] = (
    "router.chat",
    "router.manage",
    "router.inspect",
    "runner.chat",
    "runner.task",
    "runner.chore",
    "trigger.pulse",
    "trigger.watch",
)

COMPONENT_NAMESPACES = frozenset({"router", "runner", "trigger"})
COMPONENT_LEAVES = frozenset({"chat", "manage", "inspect", "task", "chore", "file", "pulse", "poll", "watch"})

if COMPONENT_NAMESPACES & COMPONENT_LEAVES:
    raise RuntimeError("component namespace and leaf names must not overlap")


def normalize_component_names(component_names: Sequence[str]) -> tuple[ComponentName, ...]:
    """Expand component shorthands and de-duplicate ids while preserving order."""

    enabled: list[ComponentName] = []
    for raw_name in component_names:
        for component_name in _expand_component_name(raw_name):
            if component_name not in enabled:
                enabled.append(component_name)
    return tuple(enabled)


def component_group(
    component_names: Iterable[str],
    namespace: ComponentNamespace,
) -> tuple[str, ...]:
    """Return enabled leaf names for one component namespace."""

    prefix = f"{namespace}."
    return tuple(name.removeprefix(prefix) for name in component_names if name.startswith(prefix))


def format_component_group(component_names: Iterable[str], namespace: ComponentNamespace) -> str:
    """Format one component namespace for human logs."""

    values = component_group(component_names, namespace)
    if not values:
        return "none"
    return ",".join(values)


def _expand_component_name(raw_name: str) -> tuple[ComponentName, ...]:
    name = raw_name.strip()
    if not name:
        return ()
    if name in ALL_COMPONENTS:
        return (cast(ComponentName, name),)
    if name in COMPONENT_NAMESPACES:
        return tuple(component for component in ALL_COMPONENTS if component.startswith(f"{name}."))
    if name in COMPONENT_LEAVES:
        return tuple(component for component in ALL_COMPONENTS if component.endswith(f".{name}"))
    raise ValueError(f"unknown component: {name}")
