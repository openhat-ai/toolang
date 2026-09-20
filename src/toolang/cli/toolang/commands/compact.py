"""Compact or forget local history through a selected producer."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from functools import lru_cache
from typing import Annotated, cast

import typer
from typer._click.exceptions import ClickException

from toolang.base.errors import ToolangError
from toolang.base.types.policy import RunBindings
from toolang.common.files import file_write_lock
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.executor.compact import (
    compact_state,
    compact_tools,
    permit,
    algorithm_input,
    result_spec,
    result_state,
)
from toolang.execution.compaction import assemble_compaction
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.types import FieldRef, RunRef, ThreadRef
from toolang.lang.ast import AgicDecl
from toolang.lang.types import Value
from toolang.lang.input import (
    CallInput,
    RunnableInput,
    resolve_input_parts,
    resolve_runnable_input,
)
from toolang.state.builtin import prepare_builtin_state
from toolang.state.state import AgentState
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


@lru_cache(maxsize=1)
def compact_runnable() -> AgicDecl:
    """Stable CLI inputs, independent of producer declarations."""
    state = prepare_builtin_state(
        "agic compact(thread: Text, before?: Text) -> Json:\n  user: {{thread}}\n"
    )
    runnable = state.modules["agent"].find_agic("compact")
    assert runnable is not None
    return runnable


def _program(algorithm: str) -> AgentState:
    if algorithm == "DEFAULT":
        return compact_state()
    if algorithm == "FORGET":
        return result_state()
    path = Path(algorithm).expanduser().resolve()
    if path.suffix != ".too":
        raise ToolangError("compact algorithm must be DEFAULT, FORGET, or a .too file")
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolangError(f"cannot read compact algorithm {path}: {exc}") from exc
    state = prepare_builtin_state(source)
    runnable = state.modules["agent"].find_agic("compact")
    expected = compact_state().modules["agent"].find_agic("compact")
    assert expected is not None

    def signature(item: AgicDecl) -> dict[str, tuple[str | None, bool]]:
        return {p.name: (p.type_name, p.optional) for p in item.params}

    if (
        runnable is None
        or runnable.output != "Text"
        or signature(runnable) != signature(expected)
    ):
        raise ToolangError(
            "compact algorithm requires agic compact(thread: Text, begin: Text, "
            "end: Text, previous_summary: Text) -> Text"
        )
    return state


def compact_command(
    ctx: typer.Context,
    arguments: Annotated[
        list[str],
        typer.Argument(
            metavar="[ARGUMENTS]",
            click_type=TextType(),
            hidden=True,
        ),
    ],
    algorithm: Annotated[
        str,
        typer.Option(
            "--algorithm",
            metavar="DEFAULT|FORGET|FILE",
            help="Compaction producer (FORGET makes no model calls)",
        ),
    ] = "DEFAULT",
    limit: LimitOptions = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            metavar="MODEL_SPEC",
            help="Override compact.model, including model parameters",
        ),
    ] = None,
    model_catalog: ModelCatalogOption = None,
) -> None:
    """Compact history before a Run, keeping that Run and later history."""
    public = compact_runnable()
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
    if algorithm == "FORGET":
        if "before" not in input:
            raise typer.BadParameter("FORGET requires before=RUN")
        if model is not None:
            raise typer.BadParameter("FORGET does not accept --model")
    program = user_call(_program, algorithm)
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
        compact_override=None
        if algorithm == "FORGET"
        else user_call(resolve_compact_override, environ, model),
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
                    algorithm=algorithm,
                    program=program,
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
    *,
    algorithm: str = "DEFAULT",
    program: AgentState | None = None,
) -> tuple[RunSpec, tuple[str, ...]]:
    if set(input) - {"thread", "before"}:
        raise ToolangError("compact accepts only thread=THREAD and before=RUN")
    forget = algorithm == "FORGET"
    if forget and "before" not in input:
        raise ToolangError("FORGET requires before=RUN")
    program = program if program is not None else _program(algorithm)
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
    end = cast(str, input.get("before", terminal[-1].id))
    RunRef.parse(end)
    if end not in ids:
        raise ToolangError("compact before must identify a visible root")
    stop = ids.index(end)
    previous = None if forget else history.get_compaction(thread)
    old_end = str(previous.result.end) if previous is not None else None
    begin = old_end if old_end is not None and ids.index(old_end) <= stop else ids[0]
    start = ids.index(begin)
    if start >= stop:
        raise ToolangError("nothing to compact before the selected Run")
    if any(
        run.status in {"pending", "running"} for run in roots[start:stop]
    ) or not any(run.status not in {"pending", "running"} for run in roots[stop:]):
        raise ToolangError(
            "compact must exclude active roots and retain a terminal root"
        )
    reuse = previous is not None and begin == old_end
    resolved: dict[str, Value] = {
        "thread": thread,
        "begin": begin,
        "end": end,
        "bare": not reuse,
    }
    if reuse:
        assert previous is not None
        resolved["previous"] = str(previous.ref)
    if forget:
        resolved["_"] = assemble_compaction(
            "Earlier history was intentionally forgotten.",
            thread=ThreadRef.parse(thread),
            roots=tuple(RunRef(ref) for ref in ids),
            request=resolved,
        ).to_data()
    request = (
        None if forget else select_compact_model(setup.models, setup.compact_model)
    )
    return RunSpec(
        setup=replace(setup, tools=compact_tools()),
        state=program,
        thread=f"compact_{thread}",
        bindings=RunBindings(
            model=request.ref if request is not None else None,
            runnable="flow:result" if forget else "agic:compact",
        ),
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
    algorithm: str = "DEFAULT",
    program: AgentState | None = None,
) -> dict[str, object]:
    program = program if program is not None else _program(algorithm)
    setup = await watcher.refresh()
    history = RunHistory(store)
    # Freeze coverage and summary generation from the same durable snapshot.
    # FORGET changes the latter without changing the target's root sequence.
    with store.read_transaction():
        spec, prefix = _prepare(
            store,
            setup,
            input,
            authored,
            algorithm=algorithm,
            program=program,
        )
        thread = cast(str, spec.input["thread"])
        previous = history.get_compaction(thread)
        summary_ref = previous.ref if previous is not None else None

    def check_range() -> tuple[RunRef, ...]:
        current = history.thread_view(thread, include_children=False).roots
        if tuple(run.id for run in current[: len(prefix)]) != prefix:
            raise ToolangError("compact range changed; submit a new request")
        return tuple(RunRef(run.id) for run in current)

    lock = store.db_path.with_name(f"{store.db_path.name}.{thread}.compact.lock")
    async with permit(lock):
        check_range()
        current = history.get_compaction(thread)
        if (current.ref if current is not None else None) != summary_ref:
            raise ToolangError(
                "compact summary changed while waiting; submit a new request"
            )
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
            if algorithm == "FORGET":
                result = await await_script_run(executor.run(spec, tracer=tracer))
            else:
                prior = (
                    previous.result
                    if previous is not None and spec.input.get("previous")
                    else None
                )
                producer = await await_script_run(
                    executor.run(
                        replace(spec, input=algorithm_input(spec.input, prior)),
                        tracer=tracer,
                    )
                )
                if producer.status != "succeeded":
                    error = (
                        store.resolve_error(producer.error)
                        if producer.error is not None
                        else producer.status
                    )
                    raise ToolangError(f"compact Run {producer.id}: {error}")
                roots = check_range()
                raw = history.get_output(producer.id)
                try:
                    assembled = assemble_compaction(
                        raw.local.value if raw is not None else None,
                        thread=ThreadRef.parse(thread),
                        roots=roots,
                        request=spec.input,
                        previous=prior,
                    )
                except (ValueError, TypeError) as exc:
                    raise ToolangError(
                        f"compact Run {producer.id}: invalid summary: {exc}"
                    ) from exc
                result = await await_script_run(
                    executor.run(result_spec(spec, assembled, RunRef(producer.id)))
                )
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
        roots = check_range()
        try:
            output = history.read_compaction(
                FieldRef.from_path(RunRef(result.id), "output"),
                ThreadRef.parse(thread),
                roots,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolangError(
                f"compact Run {result.id}: output must match its coverage and contain a nonempty summary: {exc}"
            ) from exc
        return {
            "run": result.id,
            "horizon": str(output.ref)
            if str(output.result.begin) == prefix[0]
            else None,
            "output": output.result.to_data(),
        }
