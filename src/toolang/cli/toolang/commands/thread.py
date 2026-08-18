"""Local thread and run inspection and control commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
import json
import os
from typing import Annotated, Any, Literal, cast

import click
import typer

from toolang.base.types.message import Message
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import RunRecord
from toolang.execution.records import local_to_protocol_data
from toolang.execution.schemas import RunDetail, StepData
from toolang.execution.threads import ThreadManager
from toolang.execution.types import Local, RunStatus, StepPath
from toolang.setup import SetupWatcher
from toolang.state.watcher import StateWatcher

from ...common.context import context_layout, load_runtime_environ, user_call
from ...common.execution import ExecutionResources, open_execution
from ...common.output import echo_table, executable_label, parse_utc_timestamp


InspectDocument = dict[str, Any]


@dataclass(frozen=True, slots=True)
class InspectTarget:
    """One parsed thread, run, or step inspection target."""

    kind: Literal["thread", "run"]
    identifier: str
    path: tuple[int, ...] = ()

    @property
    def path_label(self) -> str | None:
        return ".".join(str(item) for item in self.path) if self.path else None


@dataclass(frozen=True, slots=True)
class _StepNode:
    run_id: str
    path: tuple[int, ...]
    step: StepData
    children: tuple[_StepNode, ...] = ()


def threads_command(
    ctx: typer.Context,
    origin: Annotated[
        str | None, typer.Option("--origin", help="Filter by origin.")
    ] = None,
    channel: Annotated[
        str | None, typer.Option("--channel", help="Filter by channel.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by thread status.")
    ] = None,
) -> None:
    """List threads from the selected agent's durable execution store."""

    with open_execution(ctx) as resources:
        items = (
            []
            if resources is None
            else RunHistory(resources.store).list_threads(
                origin=origin,
                channel=channel,
                status=status,
            )
        )
    rows = [
        (
            item.id,
            _truncate(item.title, width=48),
            str(item.run_count),
            item.status,
            item.updated_at,
        )
        for item in items
    ]
    echo_table(("THREAD", "TITLE", "RUNS", "STATUS", "UPDATED"), rows)


def runs_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None, typer.Option("--thread", help="Filter by thread id.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by run status.")
    ] = None,
) -> None:
    """List runs from the selected agent's durable execution store."""

    run_status = _run_status(status)
    with open_execution(ctx) as resources:
        items = (
            []
            if resources is None
            else RunHistory(resources.store).list_runs(
                thread_id=thread,
                status=run_status,
            )
        )
    if thread is not None:
        rows = [
            (
                item.id,
                _truncate(item.summary or item.input_text, width=48),
                _display_status(item.status),
                item.created_at,
            )
            for item in items
        ]
        echo_table(("RUN", "TITLE", "STATUS", "CREATED"), rows)
        return
    rows = [
        (
            item.thread_id,
            item.id,
            _truncate(item.summary or item.input_text, width=48),
            _display_status(item.status),
            item.created_at,
        )
        for item in items
    ]
    echo_table(("THREAD", "RUN", "TITLE", "STATUS", "CREATED"), rows)


def inspect_command(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="Thread id, run id, or run step path to inspect.")
    ],
    limit: Annotated[
        int, typer.Option("--limit", help="Maximum thread runs to read.")
    ] = 100,
    json_view: Annotated[
        bool, typer.Option("--json", help="Render inspection data as JSON.")
    ] = False,
) -> None:
    """Inspect one thread, run, or run step path."""

    if limit < 1:
        raise click.ClickException("--limit must be at least 1")
    parsed = parse_inspect_target(target)
    with open_execution(ctx, required=True) as resources:
        if resources is None:  # pragma: no cover - required=True guarantees this
            raise RuntimeError("execution resources were not opened")
        document = _inspect(resources, parsed, limit=limit)
    if json_view:
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))
        return
    _render_inspect(document)


def steer_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to steer. Thread id means its active run."
    ),
    message: str = typer.Argument(..., help="Instruction to steer the run."),
) -> None:
    """Persist one next-step steer control."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        user_call(
            RunExecutor(resources.store, resources.ids).steer,
            run_id=run_id,
            message=Message.user(message),
            timing="next_step",
        )
    typer.echo(f"steered {run_id}")


def cancel_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to cancel. Thread id means its active run."
    ),
) -> None:
    """Persist one immediate stop control."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        run_id = _active_run_id(RunHistory(resources.store), run)
        user_call(
            RunExecutor(resources.store, resources.ids).stop,
            run_id=run_id,
        )
    typer.echo(f"canceled {run_id}")


def retry_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ...,
        help="Run id to retry. Thread id means its latest visible run.",
    ),
    anchor: Annotated[
        str | None,
        typer.Option(
            "--anchor",
            help="Retry from this canonical or run-local step path.",
        ),
    ] = None,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set model=VALUE for retried work."),
    ] = None,
) -> None:
    """Retry one terminal root run from a durable step boundary."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, run_id = _anchor(RunHistory(resources.store), run)
        result = user_call(
            asyncio.run,
            _restart_run(
                resources,
                layout=context_layout(ctx),
                kind="retry",
                source=run_id,
                anchor=user_call(_retry_anchor, run_id, anchor),
                allow_options=allows,
                default_options=defaults,
                limit_options=limit,
            ),
        )
    status = _display_status(result.status)
    typer.echo(f"retried {result.id}: {status}")
    if result.status != "succeeded":
        raise typer.Exit(1)


def rerun_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ...,
        help="Run id to rerun. Thread id means its latest visible run.",
    ),
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set model=VALUE for the new run."),
    ] = None,
) -> None:
    """Start a new root run from one terminal source invocation."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        _thread_id, source = _anchor(RunHistory(resources.store), run)
        result = user_call(
            asyncio.run,
            _restart_run(
                resources,
                layout=context_layout(ctx),
                kind="rerun",
                source=source,
                anchor=None,
                allow_options=allows,
                default_options=defaults,
                limit_options=limit,
            ),
        )
    status = _display_status(result.status)
    typer.echo(f"reran {source} as {result.id}: {status}")
    if result.status != "succeeded":
        raise typer.Exit(1)


def rewind_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to rewind before. Thread id means rewind before its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the rewound thread.")
    ] = False,
) -> None:
    """Rewind one idle thread before a terminal anchor run."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
        user_call(
            ThreadManager(resources.store, resources.ids).rewind,
            thread_id=thread_id,
            run_id=run_id,
        )
    typer.echo(f"rewound {thread_id} before {run_id}")
    if chat:
        _open_chat(ctx, thread_id)


def fork_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to fork through. Thread id means fork through its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the forked thread.")
    ] = False,
) -> None:
    """Fork one thread through a terminal anchor run."""

    with open_execution(ctx, required=True, writable=True) as resources:
        if resources is None:  # pragma: no cover
            raise RuntimeError("execution resources were not opened")
        history = RunHistory(resources.store)
        thread_id, run_id = _anchor(history, point)
        forked_id = cast(
            str,
            user_call(
                ThreadManager(resources.store, resources.ids).fork,
                thread_id=thread_id,
                run_id=run_id,
            ),
        )
    typer.echo(f"forked {forked_id} through {run_id}")
    if chat:
        _open_chat(ctx, forked_id)


def parse_inspect_target(target: str) -> InspectTarget:
    """Parse a CLI inspection target."""

    identifier, separator, raw_path = target.partition(":")
    identifier = identifier.strip()
    if not identifier:
        raise click.ClickException("inspect target is required")
    if separator and not identifier.startswith("run_"):
        raise click.ClickException("step paths are only supported for run targets")
    path = _parse_step_path(raw_path) if separator else ()
    return InspectTarget(
        kind="run" if identifier.startswith("run_") else "thread",
        identifier=identifier,
        path=path,
    )


def _inspect(
    resources: ExecutionResources,
    target: InspectTarget,
    *,
    limit: int,
) -> InspectDocument:
    history = RunHistory(resources.store)
    if target.kind == "thread":
        thread = history.get_thread(target.identifier, run_limit=limit)
        if thread is None:
            raise click.ClickException(f"thread not found: {target.identifier}")
        return {
            "kind": "thread",
            "target": target.identifier,
            "thread": _inspect_data(thread),
        }

    run = history.get_run(target.identifier)
    if run is None:
        raise click.ClickException(f"run not found: {target.identifier}")
    thread = history.get_thread(run.thread_id, run_limit=None)
    runs = thread.runs if thread is not None else [run]
    run_by_id = {item.id: item for item in runs}
    display_run = run_by_id.get(run.id, run)
    nodes = _run_nodes(display_run, run_by_id=run_by_id)
    if target.path:
        node = _find_node(nodes, target.path)
        if node is None:
            raise click.ClickException(f"step path not found: {target.path_label}")
        return {
            "kind": "step",
            "target": f"{target.identifier}:{target.path_label}",
            "run": _run_data(display_run, include_steps=False),
            "step": _node_data(node),
        }
    return {
        "kind": "run",
        "target": target.identifier,
        "run": _run_data(display_run, include_steps=False),
        "steps": [_node_data(node) for node in nodes],
    }


def _run_nodes(
    run: RunDetail,
    *,
    run_by_id: Mapping[str, RunDetail],
    parent: StepPath | None = None,
    path: tuple[int, ...] = (),
    visited_runs: frozenset[str] = frozenset(),
) -> tuple[_StepNode, ...]:
    if run.id in visited_runs:
        return ()
    visited = visited_runs | {run.id}
    nodes: list[_StepNode] = []
    for step in run.steps:
        if step.path.parent != parent:
            continue
        step_path = step.path
        node_path = (*path, step.path.index)
        children = list(
            _run_nodes(
                run,
                run_by_id=run_by_id,
                parent=step_path,
                path=node_path,
                visited_runs=visited_runs,
            )
        )
        for child_run in run_by_id.values():
            if child_run.parent != step_path:
                continue
            children.extend(
                _run_nodes(
                    child_run,
                    run_by_id=run_by_id,
                    path=node_path,
                    visited_runs=visited,
                )
            )
        nodes.append(
            _StepNode(
                run_id=run.id,
                path=node_path,
                step=step,
                children=tuple(children),
            )
        )
    return tuple(nodes)


def _find_node(nodes: Sequence[_StepNode], path: tuple[int, ...]) -> _StepNode | None:
    for node in nodes:
        if node.path == path:
            return node
        if path[: len(node.path)] == node.path:
            found = _find_node(node.children, path)
            if found is not None:
                return found
    return None


def _node_data(node: _StepNode) -> dict[str, Any]:
    return _inspect_data(
        {
            "run_id": node.run_id,
            **_record_data(node.step),
            "children": [_node_data(child) for child in node.children],
        }
    )


def _run_data(run: RunDetail, *, include_steps: bool) -> dict[str, Any]:
    data = _record_data(run)
    if not include_steps:
        data.pop("steps", None)
    return data


def _inspect_data(value: Any) -> Any:
    if isinstance(value, StepPath):
        return str(value)
    if isinstance(value, Local):
        return local_to_protocol_data(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _record_data(value)
    if isinstance(value, dict):
        return {key: _inspect_data(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_inspect_data(item) for item in value]
    return value


def _record_data(value: Any) -> dict[str, Any]:
    return {
        field.name: _inspect_data(getattr(value, field.name)) for field in fields(value)
    }


def _render_inspect(document: Mapping[str, Any]) -> None:
    kind = document.get("kind")
    if kind == "thread":
        _render_thread(_mapping(document.get("thread")))
        return
    if kind == "step":
        _render_step(_mapping(document.get("step")))
        return
    _render_run(
        _mapping(document.get("run")),
        [_mapping(item) for item in _list(document.get("steps"))],
    )


def _render_thread(thread: Mapping[str, Any]) -> None:
    _section("thread")
    typer.echo(
        "  ".join(
            (
                f"thread {thread.get('id', '-')}",
                str(thread.get("status", "-")),
                f"runs={thread.get('run_count', 0)}",
            )
        )
    )
    runs = [
        _mapping(item)
        for item in _list(thread.get("runs"))
        if _mapping(item).get("parent") is None
    ]
    if not runs:
        return
    _section("runs")
    for run in runs:
        status = _display_status(run.get("status"))
        target = executable_label(
            _text(run.get("runnable_kind")),
            _text(run.get("runnable_name")),
        )
        summary = _text(run.get("input_text")) or _text(run.get("summary")) or ""
        elapsed = _elapsed(
            _text(run.get("started_at")),
            _text(run.get("finished_at")),
        )
        typer.echo(
            f"{_status_mark(status)} {run.get('id', '-')}  "
            f"{elapsed or '-':>6}  {target:<16}  {_truncate(summary, width=72)}"
        )


def _render_run(run: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> None:
    _section("run")
    target = executable_label(
        _text(run.get("runnable_kind")),
        _text(run.get("runnable_name")),
    )
    typer.echo(
        "  ".join(
            (
                f"run {run.get('id', '-')}",
                _display_status(run.get("status")),
                f"target={target}",
                f"thread={run.get('thread_id', '-')}",
            )
        )
    )
    if input_text := _text(run.get("input_text")):
        _section("input")
        typer.echo(input_text)
    if error := _failure_text(run):
        _section("output")
        typer.echo(f"error: {error}")
    elif summary := _text(run.get("summary")):
        _section("output")
        typer.echo(summary)
    if steps:
        _section("steps")
        for step in steps:
            _render_step_line(step)


def _render_step(step: Mapping[str, Any]) -> None:
    _section("step")
    typer.echo(
        "  ".join(
            (
                f"step {step.get('run_id', '-')}:{step.get('path', '-')}",
                _display_status(step.get("status")),
                f"kind={step.get('kind', 'step')}",
            )
        )
    )
    if error := _text(step.get("error")):
        _section("error")
        typer.echo(error)
    for label in ("input", "given", "output", "noted"):
        value = step.get(label)
        if value in (None, [], {}):
            continue
        _section(label)
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    children = [_mapping(item) for item in _list(step.get("children"))]
    if children:
        _section("children")
        for child in children:
            _render_step_line(child)


def _render_step_line(step: Mapping[str, Any], *, depth: int = 0) -> None:
    status = _display_status(step.get("status"))
    summary = _step_summary(step)
    line = (
        f"{'  ' * depth}{_status_mark(status)} "
        f"{str(step.get('path', '-')):<5} {str(step.get('kind', 'step')):<7}"
    )
    if summary:
        line = f"{line}  {_truncate(summary, width=100)}"
    typer.echo(line)
    for child in [_mapping(item) for item in _list(step.get("children"))]:
        _render_step_line(child, depth=depth + 1)


def _step_summary(step: Mapping[str, Any]) -> str:
    parts = _list(step.get("output"))
    summaries: list[str] = []
    for raw in parts:
        part = _mapping(raw)
        part_type = _text(part.get("type"))
        if part_type == "text" and (text := _text(part.get("text"))):
            summaries.append(" ".join(text.split()))
        elif part_type == "tool_call":
            summaries.append(
                f"{part.get('tool_name') or part.get('name') or 'tool'} call"
            )
        elif part_type == "tool_result":
            summaries.append(
                f"{part.get('tool_name') or part.get('name') or 'tool'} result"
            )
        elif part_type:
            summaries.append(f"[{part_type}]")
    return " ".join(summaries) or (_text(step.get("error")) or "")


def _active_run_id(history: RunHistory, target: str) -> str:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise click.ClickException(f"run not found: {target}")
        if run.status != "running":
            raise click.ClickException(f"run is not running: {target}")
        return run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise click.ClickException(f"thread not found: {target}")
    if thread.active_run is None:
        raise click.ClickException(f"thread has no active run: {target}")
    return thread.active_run.id


def _anchor(history: RunHistory, target: str) -> tuple[str, str]:
    if target.startswith("run_"):
        run = history.get_run(target)
        if run is None:
            raise click.ClickException(f"run not found: {target}")
        return run.thread_id, run.id
    thread = history.get_thread(target, run_limit=0)
    if thread is None:
        raise click.ClickException(f"thread not found: {target}")
    if thread.latest_run is None:
        raise click.ClickException(f"thread has no runs: {target}")
    return thread.id, thread.latest_run.id


def _open_chat(ctx: typer.Context, thread_id: str) -> None:
    from .chat import chat_command

    chat_command(ctx, thread=thread_id)


async def _restart_run(
    resources: ExecutionResources,
    *,
    layout: AgentLayout,
    kind: Literal["retry", "rerun"],
    source: str,
    anchor: StepPath | None,
    allow_options: list[str] | None,
    default_options: list[str] | None,
    limit_options: list[str] | None,
) -> RunRecord:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    cli_bindings = resolve_binding_overrides({}, default_options)
    if "runnable" in cli_bindings:
        raise ValueError("--default runnable does not apply to a persisted source run")
    binding_overrides = {
        **resolve_binding_overrides(environ),
        **cli_bindings,
    }
    setup = await SetupWatcher(
        layout,
        ceiling_overrides=resolve_ceiling_overrides(environ, allow_options or ()),
        binding_overrides=binding_overrides,
        limit_overrides=resolve_limit_overrides(environ, limit_options or ()),
    ).refresh()
    state = await StateWatcher(layout).refresh()
    executor = RunExecutor(resources.store, resources.ids)
    try:
        handle = (
            executor.retry(
                source,
                setup=setup,
                state=state,
                anchor=anchor,
                model=setup.bindings.model,
            )
            if kind == "retry"
            else executor.rerun(
                source,
                setup=setup,
                state=state,
                model=setup.bindings.model,
            )
        )
        return await handle
    finally:
        await executor.shutdown()


def _retry_anchor(run_id: str, value: str | None) -> StepPath | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise click.ClickException("--anchor requires a step path")
    if text.startswith("run_"):
        return StepPath.parse(text)
    return StepPath.from_local(run_id, text.replace(".", "/"))


def _parse_step_path(value: str) -> tuple[int, ...]:
    if not value:
        raise click.ClickException("step path is required after ':'")
    pieces = value.split(".")
    if any(not piece.isdecimal() for piece in pieces):
        raise click.ClickException(f"invalid step path: {value}")
    return tuple(int(piece) for piece in pieces)


def _run_status(value: str | None) -> RunStatus | None:
    if value is None:
        return None
    if value not in {"pending", "running", "succeeded", "failed", "canceled"}:
        raise click.ClickException(f"unknown run status: {value}")
    return cast(RunStatus, value)


def _failure_text(run: Mapping[str, Any]) -> str:
    error = run.get("error")
    if isinstance(error, str):
        return error
    step = _text(_mapping(error).get("step"))
    return f"step {step} failed" if step else ""


def _display_status(value: object) -> str:
    return str(value or "")


def _status_mark(status: str) -> str:
    return {
        "succeeded": "✓",
        "failed": "✗",
        "canceled": "-",
        "running": "…",
    }.get(status, "·")


def _elapsed(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return ""
    start = parse_utc_timestamp(started_at)
    finish = parse_utc_timestamp(finished_at)
    if start is None or finish is None:
        return ""
    seconds = max((finish - start).total_seconds(), 0)
    if seconds < 1:
        return f"{max(round(seconds * 1000), 1)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _truncate(value: object, *, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."


def _section(label: str) -> None:
    click.secho(f"# {label}", dim=True)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
