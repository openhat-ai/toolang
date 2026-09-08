"""Run the built-in compact script against one agent's local history."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import json
import os
from typing import Annotated, cast

import typer
from typer._click.exceptions import ClickException

from toolang.base.errors import ToolangError
from toolang.base.types.policy import RunBindings
from toolang.common.files import file_write_lock
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.executor.compact import compact_state, compact_tools, permit
from toolang.execution.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.types import FieldRef, RunRef, ThreadRef, local_to_protocol_data
from toolang.lang.input import (
    CallInput,
    RunnableInput,
    resolve_input_parts,
    resolve_runnable_input,
)
from toolang.setup import SetupWatcher
from toolang.setup.models import select_compact_model
from toolang.setup.types import AgentSetup

from ...common.context import (
    ModelCatalogOption,
    context_layout,
    load_runtime_environ,
    resolve_model_catalog_option,
    user_call,
)
from ...common.execution import open_execution
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.parameters import LimitOptions, TextType
from ...common.policy import (
    resolve_ceiling_overrides,
    resolve_compact_override,
    resolve_limit_overrides,
)
from ...common.script_progress import ScriptRunPresenter
from .script import await_script_run, collect_named_arguments


def compact_command(
    ctx: typer.Context,
    arguments: Annotated[
        list[str],
        typer.Argument(
            metavar="NAME=VALUE...",
            click_type=TextType(),
            help="Script inputs: thread (required), begin, end, bare (default false).",
        ),
    ],
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            metavar="MODEL_SPEC",
            help="Override compact.model, including model parameters.",
        ),
    ] = None,
    limit: LimitOptions = None,
    model_catalog: ModelCatalogOption = None,
) -> None:
    """Compact local history with thread=THREAD [begin=RUN] [end=RUN] [bare=true]."""
    state = compact_state()
    runnable = next(
        flow for flow in state.modules["agent"].flows if flow.name == "compact"
    )
    # The same declaration drives CLI input and execution; previous is supplied here.
    public = replace(
        runnable, params=tuple(p for p in runnable.params if p.name != "previous")
    )
    authored, extra = collect_named_arguments(public, items=tuple(arguments))
    if extra:
        raise typer.BadParameter(f"unexpected compact input: {extra[0]}")
    input = user_call(
        resolve_runnable_input,
        public,
        {
            name: user_call(resolve_input_parts, value)
            for name, value in authored.items()
        },
    )
    layout = context_layout(ctx)
    environ = load_runtime_environ(layout, base_environ=os.environ)
    allow = user_call(resolve_ceiling_overrides, environ)
    watcher = SetupWatcher(
        layout,
        sandbox="host",
        model_catalog=resolve_model_catalog_option(model_catalog),
        allow_overrides={
            name: value for name, value in allow.items() if name == "models"
        },
        compact_override=user_call(resolve_compact_override, environ, model),
        limit_overrides=user_call(resolve_limit_overrides, environ, limit),
    )
    try:
        with open_execution(ctx, required=True, writable=True) as resources:
            assert resources is not None
            result = asyncio.run(
                _run(
                    resources.store,
                    resources.ids,
                    watcher,
                    input,
                    authored,
                    max_width=resolve_progress_max_width(environ),
                )
            )
    except (OSError, ToolangError, KeyError, ValueError, RuntimeError) as exc:
        raise ClickException(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def _prepare(
    store: RunStore,
    setup: AgentSetup,
    input: RunnableInput,
    authored: CallInput[str],
) -> tuple[RunSpec, tuple[str, ...]]:
    thread = str(ThreadRef.parse(cast(str, input["thread"])))
    if thread.startswith("compact_"):
        raise ToolangError("cannot compact a compact Thread")
    history = RunHistory(store)
    roots = history.thread_view(thread, include_children=False).roots
    ids = tuple(run.id for run in roots)
    terminal = [run for run in roots if run.status not in {"pending", "running"}]
    if len(terminal) < 2:
        raise ToolangError(
            "compact requires a nonempty prefix and a retained terminal root"
        )
    end = cast(str, input.get("end", terminal[-1].id))
    RunRef.parse(end)
    if end not in ids:
        raise ToolangError("compact end must be a visible root")
    stop = ids.index(end)
    previous = history.get_compaction(thread)
    old = (
        cast(Mapping[str, object], previous.output.local.value)
        if previous is not None
        else {}
    )
    old_end = cast(str | None, old.get("end"))
    begin = cast(str | None, input.get("begin"))
    if begin is None:
        begin = (
            old_end if old_end is not None and ids.index(old_end) <= stop else ids[0]
        )
    RunRef.parse(begin)
    if begin not in ids:
        raise ToolangError("compact begin must be a visible root")
    start = ids.index(begin)
    if start >= stop:
        raise ToolangError("nothing to compact: begin must precede end")
    if any(
        run.status in {"pending", "running"} for run in roots[start:stop]
    ) or not any(run.status not in {"pending", "running"} for run in roots[stop:]):
        raise ToolangError(
            "compact must exclude active roots and retain a terminal root"
        )
    reuse = previous is not None and begin == old_end and not input.get("bare", False)
    resolved = {"thread": thread, "end": end, "bare": not reuse}
    if start:
        resolved["begin"] = begin
    if reuse:
        assert previous is not None
        resolved["previous"] = str(previous.ref)
    request = select_compact_model(setup.models, setup.compact_model)
    return RunSpec(
        setup=replace(setup, tools=compact_tools()),
        state=compact_state(),
        thread=f"compact_{thread}",
        bindings=RunBindings(model=request.ref, runnable="flow:compact"),
        model_request=request,
        limits=setup.limits,
        input=RunnableInput(resolved),
        authored_input=authored,
    ), ids[: stop + 1]


async def _run(
    store: RunStore,
    ids: IdIssuer,
    watcher: SetupWatcher,
    input: RunnableInput,
    authored: CallInput[str],
    *,
    max_width: int,
) -> dict[str, object]:
    spec, prefix = _prepare(store, await watcher.refresh(), input, authored)
    thread = cast(str, spec.input["thread"])
    history = RunHistory(store)

    def check_range() -> None:
        current = history.thread_view(thread, include_children=False).roots
        if tuple(run.id for run in current[: len(prefix)]) != prefix:
            raise ToolangError("compact range changed; submit a new request")

    lock = store.db_path.with_name(f"{store.db_path.name}.{thread}.compact.lock")
    async with permit(lock):
        check_range()
        with file_write_lock(store.thread_lock_path):
            if store.get_thread(thread_id=spec.thread) is None:
                store.create_thread(
                    thread_id=spec.thread, origin="script", created_at=utc_now()
                )
        executor = RunExecutor(store, ids)
        tracer = ScriptRunPresenter(
            run_id=None, operation="compact", max_width=max_width
        )
        executor.start()
        try:
            result = await await_script_run(executor.run(spec, tracer=tracer))
        finally:
            try:
                await executor.stop()
            finally:
                tracer.close()
        if result.status != "succeeded":
            error = (
                store.resolve_error(result.error)
                if result.error is not None
                else result.status
            )
            raise ToolangError(f"compact Run {result.id}: {error}")
        check_range()
        output = history.get_output(result.id)
        raw = output.local.value if output is not None else None
        value = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
        begin = None if spec.input.get("previous") else spec.input.get("begin")
        if (
            value.get("thread") != thread
            or value.get("begin") != begin
            or value.get("end") != spec.input["end"]
            or not isinstance(value.get("summary"), str)
            or not cast(str, value["summary"]).strip()
        ):
            raise ToolangError(
                f"compact Run {result.id}: output must match its coverage and contain a nonempty summary"
            )
        assert output is not None
        return {
            "run": result.id,
            "horizon": str(FieldRef.from_path(RunRef(result.id), "output"))
            if begin is None
            else None,
            "output": local_to_protocol_data(output.local)["value"],
        }
