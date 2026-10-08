"""Run execution, inspection, control, and live event routes."""

from collections.abc import AsyncIterator
from dataclasses import replace
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.api.app import AgentCoreDep
from toolang.api.common import RUN_ID_HEADER, StreamAttachmentDep, sse_stream
from toolang.execution.subscriptions import EventSubscription, StreamScope
from toolang.api.conversion import (
    parse_authored_rerun,
    parse_authored_run,
    parse_authored_retry,
    parse_input_part,
    parse_user_message,
)
from toolang.api.schemas import (
    InputReferencesRequest,
    InputReferencesResponse,
    AuthoredRerunRequest,
    AuthoredRunRequest,
    AuthoredRetryRequest,
    RunCancelRequest,
    RunCommandResult,
    RunCreateRequest,
    RunRerunRequest,
    RunRetryRequest,
    RunSteerRequest,
    WorkdirResolveRequest,
)
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunBindings
from toolang.common.errors import ToolangError
from toolang.plugin.models.query import first_model_ref
from toolang.execution.calls import require_exact_model_request
from toolang.execution.executor import LocalRunHandle, RunSpec
from toolang.execution.records import (
    ControlRecord,
    RunRecord,
)
from toolang.execution.schemas import ControlInfo, RunDetail, RunInfo
from toolang.execution.types import RunStatus
from toolang.lang.input import decode_runnable_input
from toolang.lang.ast import AgicDecl
from toolang.execution.runnables import (
    available_runnable_defaults,
    resolve_runnable_reference,
)
from toolang.state.state import AgentState
from toolang.up import AgentCore

router = APIRouter(prefix="/runs", tags=["runs"])
_AcceptedRunStream = tuple[LocalRunHandle, EventSubscription]


async def _reject_duplicate_input_keys(request: Request) -> None:
    """Check collected input members before JSON dictionaries discard duplicates."""

    try:
        pairs = json.loads(await request.body(), object_pairs_hook=tuple)
    except (ValueError, UnicodeDecodeError):
        return  # FastAPI reports malformed JSON through its normal validation.
    if not isinstance(pairs, tuple):
        return
    runnable = dict(pairs).get("runnable")
    if not isinstance(runnable, tuple):
        return
    input = dict(runnable).get("input")
    if not isinstance(input, tuple):
        return
    seen: set[str] = set()
    for name, _value in input:
        if name in seen:
            raise HTTPException(
                status_code=422, detail=f"duplicate input argument: {name}"
            )
        seen.add(name)


async def _run_stream(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    payload: RunCreateRequest,
    response: Response,
) -> AsyncIterator[_AcceptedRunStream]:
    thread_id = _run_thread(core, payload.thread_id)
    setup = core.setup.current()
    try:
        state = core.state.current()
        resolved_runnable = resolve_runnable_reference(state, payload.runnable.ref)
        module = resolved_runnable.module
        runnable = resolved_runnable.executable
        model_request = require_exact_model_request(
            payload.model,
            setup=setup,
        )
        if isinstance(runnable, AgicDecl) and payload.model is None:
            raise ValueError("run request requires a model for an agic runnable")
        handle = core.executor.run(
            RunSpec(
                setup=setup,
                state=state,
                thread=thread_id,
                bindings=RunBindings(
                    runnable=resolved_runnable.ref,
                    model=model_request.ref if model_request is not None else None,
                ),
                model_request=model_request,
                limits=payload.policy.limits,
                ceilings=payload.policy.allow,
                input=decode_runnable_input(
                    runnable,
                    payload.runnable.input,
                    structs={item.name: item for item in state.modules[module].structs},
                    part_decoder=parse_input_part,
                ),
            ),
            request_id=payload.request_id,
        )
    except (ToolangError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    subscription = attachment.attach(StreamScope(root=handle.run_id), start="new")
    response.headers[RUN_ID_HEADER] = handle.run_id
    try:
        yield handle, subscription
    finally:
        subscription.close()


async def _run_authored_stream(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    response: Response,
    payload: AuthoredRunRequest,
) -> AsyncIterator[_AcceptedRunStream]:
    _run_thread(core, payload.thread_id)
    run_request = parse_authored_run(payload)
    if run_request.attachments is None:
        run_request = replace(run_request, attachments={})
    try:
        handle = core.executor.run(run_request)
    except (OSError, ToolangError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    subscription = attachment.attach(StreamScope(root=handle.run_id), start="new")
    response.headers[RUN_ID_HEADER] = handle.run_id
    try:
        yield handle, subscription
    finally:
        subscription.close()


async def _retry_authored_stream(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    response: Response,
    run_id: str,
    payload: AuthoredRetryRequest,
) -> AsyncIterator[_AcceptedRunStream]:
    source = _terminal_root_or_409(core, run_id)
    request = parse_authored_retry(source.id, payload)
    try:
        handle = core.executor.retry(request)
    except (ToolangError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    subscription = attachment.attach(StreamScope(root=handle.run_id), start="retry")
    response.headers[RUN_ID_HEADER] = handle.run_id
    try:
        yield handle, subscription
    finally:
        subscription.close()


async def _rerun_authored_stream(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    response: Response,
    run_id: str,
    payload: AuthoredRerunRequest,
) -> AsyncIterator[_AcceptedRunStream]:
    source = _terminal_root_or_409(core, run_id)
    request = parse_authored_rerun(source.id, payload)
    try:
        handle = core.executor.rerun(request)
    except (ToolangError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    subscription = attachment.attach(StreamScope(root=handle.run_id), start="new")
    response.headers[RUN_ID_HEADER] = handle.run_id
    try:
        yield handle, subscription
    finally:
        subscription.close()


async def _subscribe_root_run(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    run_id: str,
) -> AsyncIterator[EventSubscription]:
    run = _run_or_404(core, run_id)
    if run.parent is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run stream requires a root run: {run_id}; "
                f"subscribe to {core.store.root_run_id(run_id=run_id)}"
            ),
        )
    subscription = attachment.attach(StreamScope(root=run_id))
    try:
        yield subscription
    finally:
        subscription.close()


@router.get("", summary="List Runs", response_model=list[RunInfo])
def runs(
    core: AgentCoreDep,
    limit: int = Query(default=50),
    thread_id: str | None = None,
    status: RunStatus | None = None,
) -> list[RunInfo]:
    return core.history.list_runs(
        limit=limit,
        thread_id=thread_id,
        status=status,
    )


@router.post(
    "/stream",
    summary="Execute Run Stream",
    dependencies=[Depends(_reject_duplicate_input_keys)],
    response_class=EventSourceResponse,
)
async def execute_run_stream(
    core: AgentCoreDep,
    request: Request,
    accepted: Annotated[_AcceptedRunStream, Depends(_run_stream)],
) -> AsyncIterator[ServerSentEvent]:
    _handle, subscription = accepted
    async for event in sse_stream(
        request,
        subscription,
    ):
        yield event


@router.post(
    "/authored/stream",
    dependencies=[Depends(_reject_duplicate_input_keys)],
    summary="Execute Authored Run Stream",
    response_class=EventSourceResponse,
)
async def execute_authored_run_stream(
    core: AgentCoreDep,
    request: Request,
    accepted: Annotated[_AcceptedRunStream, Depends(_run_authored_stream)],
) -> AsyncIterator[ServerSentEvent]:
    _handle, subscription = accepted
    async for event in sse_stream(
        request,
        subscription,
    ):
        yield event


@router.post(
    "/{run_id}/retry/stream",
    summary="Retry Authored Run Stream",
    response_class=EventSourceResponse,
)
async def retry_authored_run_stream(
    core: AgentCoreDep,
    request: Request,
    accepted: Annotated[_AcceptedRunStream, Depends(_retry_authored_stream)],
) -> AsyncIterator[ServerSentEvent]:
    _handle, subscription = accepted
    async for event in sse_stream(
        request,
        subscription,
    ):
        yield event


@router.post(
    "/{run_id}/rerun/stream",
    summary="Rerun Authored Run Stream",
    response_class=EventSourceResponse,
)
async def rerun_authored_run_stream(
    core: AgentCoreDep,
    request: Request,
    accepted: Annotated[_AcceptedRunStream, Depends(_rerun_authored_stream)],
) -> AsyncIterator[ServerSentEvent]:
    _handle, subscription = accepted
    async for event in sse_stream(
        request,
        subscription,
    ):
        yield event


@router.get("/defaults", summary="Get Run Defaults")
async def run_defaults(
    core: AgentCoreDep,
    thread_id: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Return concrete defaults and workdir for a client-owned run session."""

    setup = core.setup.current()
    state = core.state.current()
    model = setup.defaults.model
    if model is None:
        fallback = first_model_ref(setup.models_effective())
        model = ModelRequest(fallback) if fallback is not None else None
    runnable = setup.defaults.runnable
    if runnable is None:
        default_agic, default_flow = available_runnable_defaults(
            state,
            fallback_agic="chat",
        )
        if default_agic is not None:
            runnable = f"agic:{default_agic}"
        elif default_flow is not None:
            runnable = f"flow:{default_flow}"
    if runnable is not None:
        try:
            runnable = resolve_runnable_reference(state, runnable).ref
        except (ToolangError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        workdir = core.executor.initial_workdir(setup, state, thread_id)
    except (OSError, ToolangError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "model": model,
        "runnable": runnable,
        "workdir": workdir,
        "policy": {
            "allow": [],
            "limits": {
                "agic_model_calls": setup.limits.agic_model_calls,
                "agic_tool_calls": setup.limits.agic_tool_calls,
                "tokens": setup.limits.tokens,
                "cost": (
                    str(setup.limits.cost) if setup.limits.cost is not None else None
                ),
                "time": setup.limits.time,
            },
        },
    }


@router.post("/workdir/resolve", summary="Resolve Run Workdir")
def resolve_run_workdir(
    core: AgentCoreDep,
    payload: WorkdirResolveRequest,
) -> dict[str, str]:
    """Resolve a proposed location without accepting or mutating a Run."""

    try:
        workdir = core.executor.resolve_workdir(
            core.setup.current(),
            core.state.current(),
            workdir=payload.workdir,
            workdir_base=payload.workdir_base,
            thread=payload.thread_id,
        )
    except (OSError, ToolangError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workdir": workdir}


@router.post("/input-references", summary="Discover Client File Inputs")
def input_references(
    core: AgentCoreDep, payload: InputReferencesRequest
) -> InputReferencesResponse:
    from toolang.execution.calls import input_file_references

    state = core.state.current()
    try:
        references = input_file_references(state, payload.runnable, payload.input)
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return InputReferencesResponse(state=state.revision, references=references)


@router.get("/{run_id}", summary="Get Run", response_model=RunDetail)
def run_detail(core: AgentCoreDep, run_id: str) -> RunDetail:
    with core.store.read_transaction():
        detail = core.history.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return replace(detail, output=core.history.get_output(run_id))


@router.get(
    "/{run_id}/stream",
    summary="Stream Run Events",
    response_class=EventSourceResponse,
)
async def run_stream(
    core: AgentCoreDep,
    request: Request,
    run_id: str,
    subscription: Annotated[EventSubscription, Depends(_subscribe_root_run)],
) -> AsyncIterator[ServerSentEvent]:
    async for event in sse_stream(
        request,
        subscription,
    ):
        yield event


@router.post(
    "/{run_id}/cancel",
    summary="Cancel Run",
    response_model=RunCommandResult,
)
def cancel_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunCancelRequest | None = None,
) -> RunCommandResult:
    run = _active_run_or_409(core, run_id)
    try:
        control = core.executor.cancel(
            run_id=run.id,
            timing=payload.mode if payload else "immediate",
            request_id=payload.request_id if payload else None,
            reason=payload.reason if payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _control_result(core, run.id, control)


@router.post(
    "/{run_id}/steer",
    summary="Steer Run",
    status_code=202,
    response_model=RunCommandResult,
)
def steer_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunSteerRequest,
) -> RunCommandResult:
    run = _active_run_or_409(core, run_id)
    try:
        control = core.executor.steer(
            run_id=run.id,
            timing=payload.mode,
            request_id=payload.request_id,
            message=parse_user_message(payload.message),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _control_result(core, run.id, control)


@router.post(
    "/{run_id}/retry",
    summary="Retry Run",
    status_code=202,
    response_model=RunCommandResult,
)
async def retry_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunRetryRequest | None = None,
) -> RunCommandResult:
    source = _terminal_root_or_409(core, run_id)
    request = payload or RunRetryRequest()
    setup = core.setup.current()
    try:
        handle = core.executor.retry(
            source.id,
            setup=setup,
            state=_recorded_state(core, source),
            anchor=request.anchor,
            limits=(
                request.limits.to_limits(setup.limits)
                if request.limits is not None
                else None
            ),
            request_id=request.request_id,
        )
    except (ToolangError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    control = core.store.list_run_controls(run_id=handle.run_id)[-1]
    return _control_result(core, handle.run_id, control)


@router.post(
    "/{run_id}/rerun",
    summary="Rerun Run",
    status_code=202,
    response_model=RunCommandResult,
)
async def rerun_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunRerunRequest | None = None,
) -> RunCommandResult:
    source = _terminal_root_or_409(core, run_id)
    request = payload or RunRerunRequest()
    setup = core.setup.current()
    try:
        state = core.state.current()
        model_request = require_exact_model_request(
            request.model,
            setup=setup,
        )
        handle = core.executor.rerun(
            source.id,
            setup=setup,
            state=state,
            model_request=model_request,
            limits=(
                request.limits.to_limits(setup.limits)
                if request.limits is not None
                else None
            ),
            request_id=request.request_id,
        )
    except (ToolangError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    control = core.store.get_run_control(run_id=handle.run_id, index=0)
    if control is None:  # pragma: no cover - executor acceptance is atomic
        raise HTTPException(status_code=500, detail="rerun control not found")
    return _control_result(core, handle.run_id, control)


def _run_or_404(core: AgentCore, run_id: str) -> RunRecord:
    run = core.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _recorded_state(core: AgentCore, run: RunRecord) -> AgentState:
    return core.state.load(core.store.resolve_state_revision(run.state))


def _active_run_or_409(core: AgentCore, run_id: str) -> RunRecord:
    run = _run_or_404(core, run_id)
    if run.status not in {"pending", "running"}:
        raise HTTPException(status_code=409, detail=f"run is not active: {run_id}")
    return run


def _terminal_root_or_409(core: AgentCore, run_id: str) -> RunRecord:
    run = _run_or_404(core, run_id)
    if run.parent is not None:
        raise HTTPException(status_code=409, detail=f"run is not a root: {run_id}")
    if run.status in {"pending", "running"}:
        raise HTTPException(status_code=409, detail=f"run is not terminal: {run_id}")
    return run


def _control_result(
    core: AgentCore,
    run_id: str,
    control: ControlRecord,
) -> RunCommandResult:
    run = _run_or_404(core, run_id)
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after control: {run_id}",
        )
    return RunCommandResult(
        run=detail,
        command=ControlInfo.from_record(run, control),
    )


def _run_thread(core: AgentCore, thread_id: str) -> str:
    if core.store.get_thread(thread_id=thread_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"thread not found: {thread_id}",
        )
    return thread_id
