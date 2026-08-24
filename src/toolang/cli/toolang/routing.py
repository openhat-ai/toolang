"""Declarative command grammar for the Toolang CLI entry point."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolang.common.layout import AgentLayout, AgentPlacement
from toolang.up import process as agents
from ..caps.commands import CAP_KINDS
from ..common.output import echo_error
from ..common.progress import as_progress_sink, make_cli_progress
from ..common.routing import explicit_agent, extract_root_args
from .commands import runtime, script

TargetPosition = Literal["none", "before", "after"]
Preparation = Literal["layout", "program"]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """The accepted target grammar for one top-level command."""

    name: str
    targets: frozenset[TargetPosition]
    placements: frozenset[AgentPlacement] = frozenset()
    prepare: Preparation | None = None

    def accepts(self, position: TargetPosition, placement: AgentPlacement) -> bool:
        return position in self.targets and placement in self.placements


@dataclass(frozen=True, slots=True)
class TargetHelp:
    """One target-only route and its human-readable label."""

    selector: str
    label: str
    placement: AgentPlacement


_ALL_PLACEMENTS = frozenset[AgentPlacement]({"resident", "visiting", "roaming"})
_RESIDENT = frozenset[AgentPlacement]({"resident"})
_GLOBAL_VALUE_OPTIONS = ("--models",)


def _extract_global_args(argv: list[str]) -> tuple[list[str], list[str]]:
    return extract_root_args(argv, extra_value_options=_GLOBAL_VALUE_OPTIONS)


def _command(
    name: str,
    *targets: TargetPosition,
    placements: frozenset[AgentPlacement] = frozenset(),
    prepare: Preparation | None = None,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        targets=frozenset(targets),
        placements=placements,
        prepare=prepare,
    )


COMMAND_SPECS: Mapping[str, CommandSpec] = {
    spec.name: spec
    for spec in (
        _command("new", "none"),
        _command("clone", "none"),
        _command("list", "none"),
        _command("remove", "after", placements=_RESIDENT, prepare="layout"),
        _command(
            "info",
            "before",
            "after",
            placements=_ALL_PLACEMENTS,
            prepare="program",
        ),
        _command(
            "run",
            "before",
            "after",
            placements=_ALL_PLACEMENTS,
            prepare="program",
        ),
        _command(
            "start",
            "before",
            "after",
            placements=_RESIDENT,
            prepare="program",
        ),
        _command(
            "stop",
            "before",
            "after",
            placements=_RESIDENT,
            prepare="layout",
        ),
        _command("task", "before", placements=_RESIDENT, prepare="program"),
        _command("chore", "before", placements=_RESIDENT, prepare="program"),
        _command("chat", "before", placements=_ALL_PLACEMENTS, prepare="program"),
        _command("threads", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("runs", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("inspect", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("steer", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("cancel", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("retry", "before", placements=_ALL_PLACEMENTS, prepare="program"),
        _command("rerun", "before", placements=_ALL_PLACEMENTS, prepare="program"),
        _command("rewind", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        _command("fork", "before", placements=_ALL_PLACEMENTS, prepare="layout"),
        *(
            _command(
                name,
                "none",
                "before",
                placements=_RESIDENT,
                prepare="program",
            )
            for name in ("caps", *CAP_KINDS)
        ),
        _command("models", "none"),
        _command("providers", "none"),
        _command("adapters", "none"),
        _command("tools", "none"),
        _command("channel", "none"),
        _command("sandboxes", "none"),
        _command("hidden", "none"),
        _command("fmt", "none"),
        _command("parse", "none"),
        _command("serve", "after", placements=_RESIDENT, prepare="program"),
    )
}


class RoutingError(ValueError):
    """A CLI target cannot be routed through the declared command grammar."""


def command_spec(name: str) -> CommandSpec:
    """Return the required route metadata for a registered command."""

    try:
        return COMMAND_SPECS[name]
    except KeyError as exc:  # pragma: no cover - checked at module assembly
        raise RuntimeError(f"top-level command has no routing spec: {name}") from exc


def validate_command_registration(names: set[str]) -> None:
    """Require the Typer surface and routing grammar to have identical names."""

    expected = set(COMMAND_SPECS)
    if names != expected:
        missing = sorted(expected - names)
        extra = sorted(names - expected)
        raise RuntimeError(
            f"command routing mismatch: missing={missing!r}, extra={extra!r}"
        )


def select_target_help(
    argv: list[str],
    *,
    residents: Collection[str],
) -> TargetHelp | None:
    """Select one unambiguous target that has no command yet."""

    _global_args, body = _extract_global_args(argv)
    if not body or len(body) > 2:
        return None
    if len(body) == 2 and body[1] not in {"--help", "-h"}:
        return None
    target = body[0]
    if target in COMMAND_SPECS or target.startswith("-"):
        return None
    if _source_path(target) is not None:
        return None
    explicit = explicit_agent(target)
    if explicit is not None:
        return TargetHelp(selector=target, label=explicit, placement="resident")
    if _is_visiting(target):
        return TargetHelp(selector=target, label=target, placement="visiting")
    if target in residents:
        return TargetHelp(selector=target, label=target, placement="resident")
    return None


def dispatch_roaming(
    argv: list[str],
    *,
    prog_name: str,
    run_app: Callable[[list[str], AgentLayout], int],
) -> int | None:
    """Route a local source target or fall through to runnable invocation."""

    global_args, body = _extract_global_args(argv)
    selected = _selected_roaming(body)
    if selected is None:
        return None
    source, command, position = selected
    if command is not None:
        if global_args:
            return _unsupported_global_options()
        spec = command_spec(command)
        if not spec.accepts(position, "roaming"):
            return _unsupported_target(command, "roaming", position)
        try:
            layout = _roaming_layout(source, spec.prepare)
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            echo_error(str(exc))
            return 1
        return run_app(
            _selected_command_args(body, position, target=layout.name),
            layout,
        )
    if runtime.is_roaming_file_request(body[1:]):
        if global_args:
            return _unsupported_global_options()
        return runtime.run_roaming_file(source, body[1:])
    return script.dispatch(global_args, body, prog_name=prog_name)


def dispatch_visiting(
    argv: list[str],
    *,
    run_app: Callable[[list[str], AgentLayout], int],
) -> int | None:
    """Route a supported remote-selector command through a visiting layout."""

    global_args, body = _extract_global_args(argv)
    selected = _selected_visiting(body)
    if selected is None:
        return None
    selector, command, position = selected
    spec = command_spec(command)
    if not spec.accepts(position, "visiting"):
        return _unsupported_target(command, "visiting", position)
    progress = make_cli_progress() if spec.prepare == "program" else None
    try:
        layout = (
            agents.resolve_visiting_layout(
                selector,
                progress=as_progress_sink(progress),
            )
            if spec.prepare == "program"
            else agents.visiting_layout(selector)
        )
    except KeyboardInterrupt:
        if progress is not None:
            progress.interrupt()
        return 130
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        if progress is not None:
            progress.finish(details=False)
        echo_error(str(exc))
        return 1
    if progress is not None:
        progress.finish(details=False)
    return run_app(
        [
            *global_args,
            *_selected_command_args(body, position, target=layout.name),
        ],
        layout,
    )


def normalize(argv: list[str]) -> tuple[list[str], str | None]:
    """Normalize resident target-first syntax after non-resident routing."""

    global_args, body = _extract_global_args(argv)
    try:
        rewritten_body, agent = _rewrite_resident(body)
    except RoutingError:
        raise
    except ValueError as exc:
        raise RoutingError(str(exc)) from exc
    return [*global_args, *rewritten_body], agent


def _rewrite_resident(body: list[str]) -> tuple[list[str], str | None]:
    if not body:
        return body, None
    first = body[0]
    first_spec = COMMAND_SPECS.get(first)
    if first_spec is not None:
        if any(token in {"--help", "-h"} for token in body[1:]):
            return body, None
        if len(body) >= 2 and explicit_agent(body[1]) is not None:
            if not first_spec.accepts("after", "resident"):
                raise RoutingError(_target_order_error(first_spec))
            return [first, explicit_agent(body[1]) or "", *body[2:]], None
        return body, None
    if len(body) < 2:
        return body, None
    command = body[1]
    spec = COMMAND_SPECS.get(command)
    if spec is None:
        return body, None
    explicit = explicit_agent(first)
    agent = explicit or first
    if not spec.accepts("before", "resident"):
        raise RoutingError(_target_order_error(spec))
    if "after" in spec.targets:
        return [command, agent, *body[2:]], None
    return [command, *body[2:]], agent


def _selected_roaming(
    body: list[str],
) -> tuple[Path, str | None, TargetPosition] | None:
    if not body:
        return None
    if (source := _source_path(body[0])) is not None:
        command = body[1] if len(body) >= 2 and body[1] in COMMAND_SPECS else None
        return source, command, "before"
    spec = COMMAND_SPECS.get(body[0])
    if (
        spec is not None
        and len(body) >= 2
        and "after" in spec.targets
        and (source := _source_path(body[1])) is not None
    ):
        return source, spec.name, "after"
    return None


def _selected_visiting(
    body: list[str],
) -> tuple[str, str, TargetPosition] | None:
    if len(body) >= 2 and _is_visiting(body[0]) and body[1] in COMMAND_SPECS:
        return body[0], body[1], "before"
    spec = COMMAND_SPECS.get(body[0]) if body else None
    if (
        spec is not None
        and len(body) >= 2
        and "after" in spec.targets
        and _is_visiting(body[1])
    ):
        return body[1], spec.name, "after"
    return None


def _selected_command_args(
    body: list[str], position: TargetPosition, *, target: str
) -> list[str]:
    if position == "before":
        command = body[1]
        rest = body[2:]
    elif position == "after":
        command = body[0]
        rest = body[2:]
    else:
        raise AssertionError(f"selected target cannot use position {position!r}")
    if "after" in command_spec(command).targets:
        return [command, target, *rest]
    return [command, *rest]


def _roaming_layout(source: Path, prepare: Preparation | None) -> AgentLayout:
    if prepare == "program":
        return agents.materialize_roaming_program(source)
    return AgentLayout.roaming(source)


def _source_path(token: str) -> Path | None:
    text = token.strip()
    if (
        not text
        or text.startswith(("-", "agent:", "agic:", "flow:", "runnable:"))
        or "://" in text
    ):
        return None
    try:
        source = Path(text).expanduser().resolve()
    except OSError:
        return None
    return source if source.suffix == ".too" else None


def _is_visiting(token: str) -> bool:
    try:
        return agents.parse_agent_selector(token).form != "name"
    except ValueError:
        return False


def _target_order_error(spec: CommandSpec) -> str:
    if spec.targets == frozenset({"before"}):
        return f"{spec.name} requires TARGET before the command"
    if spec.targets == frozenset({"after"}):
        return f"{spec.name} requires TARGET after the command"
    return f"{spec.name} does not accept an agent target here"


def _unsupported_target(
    command: str,
    placement: AgentPlacement,
    position: TargetPosition,
) -> int:
    spec = command_spec(command)
    if position not in spec.targets:
        message = _target_order_error(spec)
    else:
        message = f"{command} does not support {placement} agents"
    echo_error(message)
    return 2


def _unsupported_global_options() -> int:
    echo_error("too <path>.too does not support global CLI options")
    return 1
