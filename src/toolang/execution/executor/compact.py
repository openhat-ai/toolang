"""A durable, framework-owned compact Run; callers hold the target permit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json

from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import ToolResultPart
from toolang.base.types.model import Model, ModelRequest
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.common.time import utc_now
from toolang.lang.input import CallInput
from .. import batched_compaction as reducer
from ..accounting import build_model_accounting
from ..inspection.history import RunHistory
from ..errors import HistoryChangedError
from ..records import RunControlPayload, RunRecord
from ..schemas import CompactionOutput
from ..store import RunStore
from ..types import (
    AgentResources,
    ErrorMessage,
    FieldRef,
    Local,
    ModelStepGiven,
    ModelStepNoted,
    Output,
    RunRef,
    StepRef,
    ThreadRef,
    ToolStepGiven,
)
from .limits import _RunLimitState

RUNNABLE = reducer.RUNNABLE
READ_TOOL = reducer.READ_TOOL


@dataclass(frozen=True)
class CompactSpec:
    """Concrete caller-resolved policy, with no environment or CLI defaults."""

    target: ThreadRef
    roots: tuple[RunRef, ...]
    begin: RunRef
    end: RunRef
    summary: str
    prior: RunRef | None
    model: Model
    request: ModelRequest
    adapter: ModelAdapter
    environ: Mapping[str, str]
    setup: str
    state: str
    sandbox: str
    limits: RunLimits
    size: int
    versions: Mapping[str, str]

    def input(self) -> CallInput:
        return CallInput(
            {
                "thread": str(self.target),
                "start": str(self.roots[0]),
                "begin": str(self.begin),
                "end": str(self.end),
                "summary": self.summary,
                "snapshot": json.dumps(
                    [str(root) for root in self.roots[: self.roots.index(self.end) + 1]]
                ),
                "prior": str(self.prior) if self.prior else "",
                "versions": json.dumps(dict(self.versions), sort_keys=True),
                "policy": json.dumps(
                    {
                        "version": 1,
                        "setup": self.setup,
                        "size": self.size,
                        "output": self.request.max_output,
                        "reasoning": self.request.reasoning.to_data()
                        if self.request.reasoning
                        else None,
                        "model": self.model.ref,
                        "limit": dict(self.model.limit),
                    },
                    sort_keys=True,
                ),
            }
        )


def _finish_abandoned_steps(store: RunStore, run: RunRecord) -> None:
    for step in store.list_steps(run_id=run.id):
        if step.status == "running":
            store.finish_step(
                ref=step.ref,
                kind=step.kind,
                status="canceled",
                output=None,
                noted=None,
                error=None,
                finished_at=utc_now(),
            )


def _candidate(store: RunStore, spec: CompactSpec) -> RunRecord | None:
    expected = spec.input()
    for run in reversed(
        store.list_thread_runs_chronological(thread_id=f"compact_{spec.target}")
    ):
        if run.parent is not None:
            continue
        entry = store.get_run_control(run_id=run.id, index=0)
        if (
            entry is None
            or not isinstance(entry.payload, RunControlPayload)
            or entry.payload.runnable != RUNNABLE
        ):
            if run.status in {"pending", "running"}:
                raise ToolangError(f"compaction already running: {run.id}")
            continue
        if run.status in {"failed", "canceled"}:
            continue
        compatible = (
            entry.payload.input == expected
            and entry.payload.model_request == spec.request
            and entry.payload.state == spec.state
            and entry.payload.limits == spec.limits
            and entry.payload.sandbox == spec.sandbox
        )
        try:
            saved = entry.payload.input
            captured = tuple(
                RunRef.parse(root) for root in json.loads(str(saved["snapshot"]))
            )
            saved_end = RunRef.parse(str(saved["end"]))
            # Successful producers can cover an earlier requested boundary. Later
            # appended roots or a changed reducer policy do not invalidate them.
            reusable = (
                saved["thread"] == str(spec.target)
                and saved["prior"] == (str(spec.prior) if spec.prior else "")
                and saved["summary"] == spec.summary
                and saved["begin"] == str(spec.begin)
                and captured == spec.roots[: len(captured)]
                and captured[-1] == saved_end
                and spec.roots.index(saved_end) > spec.roots.index(spec.begin)
            )
            if reusable and (run.status == "succeeded" or compatible):
                reducer.validate_versions(store, saved)
                cursor, summary = reducer.read_checkpoint(store, run)
                if run.status == "succeeded":
                    output = RunHistory(store).read_compaction(
                        RunRef(run.id), spec.target, spec.roots
                    )
                    if cursor != len(captured) - 1 or summary != output.result.summary:
                        raise ValueError(
                            "compact Run has incomplete checkpoint coverage"
                        )
                return run
        except (ValueError, TypeError, KeyError, IndexError, HistoryChangedError):
            pass
        if run.status in {"pending", "running"}:
            _finish_abandoned_steps(store, run)
            store.finish_run(
                run_id=run.id,
                status="failed",
                error=ErrorMessage("stale compact attempt"),
            )
            store.fail_pending_run_controls(
                run_id=run.id, finished_at=utc_now(), error="stale compact attempt"
            )
    return None


def _publish(store: RunStore, run: RunRecord, spec: CompactSpec) -> None:
    with store.write_transaction():
        target = store.get_thread(thread_id=str(spec.target))
        if target is None or target.horizon != spec.prior:
            raise ValueError("published compact horizon changed")
        entry = store.get_run_control(run_id=run.id, index=0)
        if entry is None or not isinstance(entry.payload, RunControlPayload):
            raise ValueError("compact Run is missing its entry contract")
        reducer.validate_versions(store, entry.payload.input)
        store.publish_compaction(RunRef(run.id), roots=spec.roots)


async def produce(
    store: RunStore,
    spec: CompactSpec,
    *,
    issue_run: Callable[[], str],
) -> CompactionOutput:
    """Recover or produce one summary, then publish in a separate transaction."""
    history = RunHistory(store)
    current = history.get_compaction(spec.target)
    if current is not None and current.result.end == str(spec.end):
        return history.read_compaction(current.ref, spec.target, spec.roots)
    if (current.ref if current else None) != spec.prior:
        raise ValueError("published compact horizon changed")
    # Validate the fixed prefix before inspecting candidates or calling a provider.
    visible = tuple(
        RunRef(run.id)
        for run in history.thread_view(str(spec.target), include_children=False).roots
    )
    stop = spec.roots.index(spec.end)
    if not 0 <= spec.roots.index(spec.begin) < stop:
        raise ValueError("compact requires nonempty half-open coverage")
    if spec.begin != (
        RunRef(current.result.end) if current else spec.roots[0]
    ) or spec.summary != (current.result.summary if current else ""):
        raise ValueError("compact request must extend its published prior summary")
    if visible[: stop + 1] != spec.roots[: stop + 1]:
        raise ValueError("compact range changed")
    reducer.validate_versions(store, spec.input())
    reducer._select_runs(history, spec.target, spec.begin, spec.roots[stop - 1])
    run = _candidate(store, spec)
    if run is not None and run.status == "succeeded":
        _publish(store, run, spec)
        return history.read_compaction(RunRef(run.id), spec.target, spec.roots)
    if run is None:
        thread = f"compact_{spec.target}"
        if store.get_thread(thread_id=thread) is None:
            store.create_thread(thread_id=thread, origin="script", created_at=utc_now())
        run, _ = store.accept_run(
            run_id=issue_run(),
            parent=None,
            thread=thread,
            resources=AgentResources(models=(spec.model.ref,)),
            limits=spec.limits,
            state=spec.state,
            runnable=RUNNABLE,
            model_request=spec.request,
            input=spec.input(),
            sandbox=spec.sandbox,
            occurrence=None,
            request_id=None,
            created_at=utc_now(),
        )
    driver = _CompactDriver(store, spec, run)
    try:
        async with asyncio.timeout(spec.limits.time):
            await driver.execute()
    except asyncio.CancelledError:
        driver.fail(canceled=True)
        raise
    except Exception as exc:
        driver.fail(error=exc)
        raise
    # A failure here leaves the successful producer intact for zero-call recovery.
    _publish(store, run, spec)
    return history.read_compaction(RunRef(run.id), spec.target, spec.roots)


class _CompactDriver:
    def __init__(self, store: RunStore, spec: CompactSpec, run: RunRecord) -> None:
        self.store, self.spec, self.run = store, spec, run
        self.limits = _RunLimitState(spec.limits)
        steps = store.list_steps(run_id=run.id)
        self.next_step = max((step.ref.indices[0] for step in steps), default=-1) + 1
        self.calls = 0

    def fail(self, *, canceled: bool = False, error: Exception | None = None) -> None:
        # Never overwrite a committed success, including interruption after finish_run.
        run = self.store.get_run(run_id=self.run.id)
        if run is not None and run.status in {"pending", "running"}:
            _finish_abandoned_steps(self.store, run)
            self.store.finish_run(
                run_id=run.id,
                status="canceled" if canceled else "failed",
                error=ErrorMessage(str(error) or type(error).__name__)
                if error
                else None,
            )

            self.store.fail_pending_run_controls(
                run_id=run.id,
                finished_at=utc_now(),
                error="compact producer terminated",
            )

    async def execute(self) -> None:
        store, spec, run = self.store, self.spec, self.run
        steps = store.list_steps(run_id=run.id)
        for step in steps:
            if step.kind == "model":
                self.calls += 1
                if isinstance(step.noted, ModelStepNoted):
                    self.limits.record_model(self.spec.model, step.noted.accounting)

        cursor, summary = reducer.read_checkpoint(store, run)
        _finish_abandoned_steps(store, run)
        if run.status == "pending":
            store.begin_run(run_id=run.id, control=run.control, started_at=utc_now())
        store.finish_run_controls(run_id=run.id, indexes=(0,), finished_at=utc_now())
        stop = spec.roots.index(spec.end)
        if cursor < stop:
            records = {
                RunRef(run.id): run
                for run in RunHistory(store)
                .thread_view(str(spec.target), include_children=False)
                .roots
            }
            summary, _ = await reducer.compact(
                spec.roots[cursor],
                spec.roots[stop - 1],
                spec.size,
                history=RunHistory(store),
                thread=spec.target,
                load_unit=lambda ref: reducer._history_unit(store, records[ref]),
                model=spec.model,
                max_output_tokens=spec.request.max_output,
                adapter=spec.adapter,
                environ=spec.environ,
                summary=summary,
                reasoning=spec.request.reasoning,
                invoke=self.invoke,
            )
        store.finish_run(
            run_id=run.id,
            status="succeeded",
            output=Output(Local.typed("Text", summary), "_"),
        )

    async def invoke(
        self, call: ModelCall, units: Sequence[reducer.HistoryUnit]
    ) -> ModelCallResult:
        store, spec = self.store, self.spec
        limit = spec.limits.agic_model_calls
        if limit is not None and self.calls >= limit:
            raise ToolangError(f"compact model call limit exceeded: {limit}")
        self.calls += 1
        read = StepRef.from_local(self.run.id, (self.next_step,))
        model = StepRef.from_local(self.run.id, (self.next_step + 1,))
        self.next_step += 2
        roots = [str(unit.run_id) for unit in units]
        tool = ToolCall(str(read), str(read), READ_TOOL, {"roots": roots})
        store.begin_step(
            ref=read,
            kind="tool",
            input=(),
            given=ToolStepGiven("_toolang", tool, trigger="runtime"),
            started_at=utc_now(),
        )
        result = ToolResultPart(
            tool_call_id=tool.tool_call_id,
            call_id=tool.call_id,
            tool_name=READ_TOOL,
            tool_family=READ_TOOL,
            output={
                "roots": roots,
                "content": str(FieldRef.from_path(model, "given", "call", "messages")),
            },
        )
        store.finish_step(
            ref=read,
            kind="tool",
            status="succeeded",
            output=Output(Local.typed("Part", result), "_"),
            noted=None,
            error=None,
            finished_at=utc_now(),
        )
        store.begin_step(
            ref=model,
            kind="model",
            input=(),
            given=ModelStepGiven(spec.model.ref, call, setup=spec.setup),
            started_at=utc_now(),
        )
        response: ModelCallResult | None = None
        try:
            response = await spec.adapter.invoke(spec.model, call, environ=spec.environ)
            reducer.summary_text(response)
        except BaseException as exc:
            # Hard process termination is recovered from persisted running Steps.
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
            usage = (
                exc.usage
                if isinstance(exc, ModelResponseError)
                else response.usage
                if response
                else None
            )
            accounting = build_model_accounting(spec.model, usage)
            store.finish_step(
                ref=model,
                kind="model",
                status="canceled"
                if isinstance(exc, asyncio.CancelledError)
                else "failed",
                output=None,
                noted=ModelStepNoted(accounting=accounting),
                error=ErrorMessage(str(exc)) if str(exc) else None,
                finished_at=utc_now(),
            )
            if usage is not None:
                self.limits.record_model(spec.model, accounting)
            raise
        accounting = build_model_accounting(spec.model, response.usage)
        assert response.message is not None
        store.finish_step(
            ref=model,
            kind="model",
            status="succeeded",
            output=Output(Local.typed("Part[]", tuple(response.message.parts)), "_"),
            noted=ModelStepNoted(accounting=accounting),
            error=None,
            finished_at=utc_now(),
        )
        self.limits.record_model(spec.model, accounting)
        return response
