"""Formal read-only agent API routes."""

from __future__ import annotations

from dataclasses import asdict
from collections.abc import AsyncIterator, Container, Sequence
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from toolang.base.types.message import message_summary
from ..execution.detail import (
    RunDetail,
    ThreadInfo,
    run_detail_from_record,
    thread_info_from_runs,
)
from ..execution.events import MessageData, run_message_data
from ..execution.records import ModelCallStepPayload
from .. import agents, work
from ..state.durable import scan_durable_state
from ..state.prepared import PreparedEntry, load_prepared_state

if TYPE_CHECKING:
    from ..up import UptimeContext
    from ..execution.runner import RunOutcome
    from ..execution.records import RunRecord

CapKind = Literal["psyche", "skill", "service", "prompt"]
RUN_LOOPS = frozenset({"chat", "pulse", "poll", "hook"})
HTTP_LOOPS = frozenset({"chat", "hook", "control", "inspect"})
BACKGROUND_LOOPS = frozenset({"pulse", "poll", "prepare", "reload"})
COLLECTION_TO_KIND: dict[str, CapKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


def create_router() -> APIRouter:
    """Build the formal read-only agent API route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/profile", tags=["agent"], summary="Get Profile")
    async def profile(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        runtime_state = agents.load_runtime_state(context.root, context.name) or {}
        return {
            "agent": context.name,
            "display_name": context.name,
            "title": None,
            "summary": None,
            "description": None,
            "avatar": None,
            "environment": _profile_environment(context, runtime_state=runtime_state),
            "metrics": _profile_metrics(context),
        }

    @router.get("/caps", tags=["caps"], summary="Get Caps Summary")
    async def caps_summary(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        collections = {
            "psyches": _cap_collection(context, kind="psyche"),
            "skills": _cap_collection(context, kind="skill"),
            "services": _cap_collection(context, kind="service"),
            "prompts": _cap_collection(context, kind="prompt"),
        }
        return {
            "agent": context.name,
            **collections,
            "counts": {key: len(value) for key, value in collections.items()},
        }

    @router.get("/psyches", tags=["caps"], summary="List Psyches")
    @router.get("/skills", tags=["caps"], summary="List Skills")
    @router.get("/services", tags=["caps"], summary="List Services")
    @router.get("/prompts", tags=["caps"], summary="List Prompts")
    async def cap_list(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(str(request.url.path).rsplit("/", 1)[-1])
        return {"items": _cap_collection(context, kind=kind)}

    @router.get("/psyches/{name}", tags=["caps"], summary="Get Psyche")
    @router.get("/skills/{name}", tags=["caps"], summary="Get Skill")
    @router.get("/services/{name}", tags=["caps"], summary="Get Service")
    @router.get("/prompts/{name}", tags=["caps"], summary="Get Prompt")
    async def cap_detail(request: Request, name: str) -> dict[str, object]:
        context = request.app.state.runtime
        collection = str(request.url.path).split("/")[3]
        kind = _collection_kind(collection)
        entry = _live_entry_by_name(context, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.get("/runs", tags=["activity"], summary="List Runs")
    async def runs(request: Request, limit: int = Query(default=50), thread_id: str | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        runs = context.store.list_runs(limit=limit, thread_id=thread_id)
        steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
        items = [_run_item(item, steps=steps_by_run.get(item.run_id, ())) for item in runs]
        return {"items": items}

    @router.get("/runs/{run_id}", tags=["activity"], summary="Get Run")
    async def run_detail(request: Request, run_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        run = context.store.get_run(run_id=run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return _run_detail_data(_run_detail(context, run))

    @router.get("/instructions/{instructions_hash}", tags=["activity"], summary="Get Instructions")
    async def instructions(request: Request, instructions_hash: str) -> dict[str, object]:
        context = request.app.state.runtime
        body = context.store.get_instruction_blob(instructions_hash=instructions_hash)
        if body is None:
            raise HTTPException(status_code=404, detail=f"instructions not found: {instructions_hash}")
        return {"hash": instructions_hash, "body": body}

    @router.get("/threads", tags=["activity"], summary="List Threads")
    async def threads(
        request: Request,
        limit: int = Query(default=50),
        origin: str | None = None,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        items = _thread_items(context)
        if origin is not None:
            items = [item for item in items if item.origin == origin]
        return {"items": [asdict(item) for item in items[:limit]]}

    @router.get("/threads/{thread_id:path}", tags=["activity"], summary="Get Thread")
    async def thread_detail(request: Request, thread_id: str, limit: int = Query(default=50)) -> dict[str, object]:
        context = request.app.state.runtime
        items = _thread_items(context)
        info = next((item for item in items if item.id == thread_id), None)
        if info is None:
            raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
        runs = [
            _run_detail(context, item)
            for item in sorted(
                context.store.list_runs(limit=limit, thread_id=thread_id),
                key=lambda run: run.created_at,
            )
        ]
        return {
            "info": asdict(info),
            "runs": [_run_detail_data(item) for item in runs],
        }

    @router.get("/events", tags=["activity"], summary="List Events")
    async def events(request: Request, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        return {
            "items": [asdict(item) for item in context.store.list_updates(limit=limit)]
        }

    @router.get("/events/stream", tags=["activity"], summary="Stream Events")
    async def events_stream() -> StreamingResponse:
        return StreamingResponse(_events_stream(), media_type="text/event-stream")

    @router.get("/tasks", tags=["jobs"], summary="List Tasks")
    async def tasks(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _task_collection(context)}

    @router.get("/chores", tags=["jobs"], summary="List Chores")
    async def chores(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _chore_collection(context)}

    @router.get("/will", tags=["jobs"], summary="Get Will")
    async def will() -> dict[str, object]:
        return {"item": None}

    return router


def snapshot_context(
    context: UptimeContext,
    *,
    enabled_loops: Sequence[str],
) -> dict[str, object]:
    """Return the internal runtime snapshot used by tests and diagnostics."""

    durable = scan_durable_state(context.root, context.name)
    prepared = load_prepared_state(context.root, context.name)
    runner_snapshot = context.runner.snapshot()
    live_operational_facts: dict[str, object] = {
        "queue_pending": len(context.runner),
        "active_runs": _runner_in_flight(runner_snapshot),
        "completed_runs": len(context.runner.completed()),
    }
    durable_operational_facts: dict[str, object] = {
        "prepared_fingerprint": prepared.fingerprint,
        **live_operational_facts,
    }
    recent_runs = context.store.list_runs(limit=20)
    recent_steps = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in recent_runs))
    return {
        "enabled_loops": list(enabled_loops),
        "http_loops": _select_loops(enabled_loops, HTTP_LOOPS),
        "run_loops": _select_loops(enabled_loops, RUN_LOOPS),
        "background_loops": _select_loops(enabled_loops, BACKGROUND_LOOPS),
        "queue_pending": len(context.runner),
        "durable": {
            "toolang_root": str(durable.toolang_root),
            "agent_name": durable.agent_name,
            "fingerprint": durable.fingerprint,
            "scanned_at": durable.scanned_at,
            "definitions": {
                "program_source": durable.program_source,
                "config_paths": list(durable.config_paths),
                "global_entries": [entry.to_snapshot() for entry in _authored_entries(context, scope="global")],
                "agent_entries": [entry.to_snapshot() for entry in _authored_entries(context, scope="agent")],
            },
            "operational_facts": durable_operational_facts,
        },
        "prepared": prepared.to_snapshot(),
        "live": context.live.to_snapshot(operational_facts=live_operational_facts),
        "runner": runner_snapshot,
        "channels": _channel_items(context),
        "execution": {
            "recent_updates": [asdict(item) for item in context.store.list_updates(limit=20)],
            "recent_runs": [asdict(item) for item in recent_runs],
            "recent_messages": [
                asdict(item)
                for run in sorted(recent_runs, key=lambda item: item.created_at)
                for item in run_message_data(run, steps=recent_steps.get(run.run_id, ()))
            ],
        },
        "completed_runs": [_run_outcome_data(result) for result in context.runner.completed()],
    }


def _cap_collection(context: UptimeContext, *, kind: CapKind) -> list[dict[str, object]]:
    return [
        _cap_summary_item(context, entry)
        for entry in context.live.cap_entries
        if entry.kind == kind
    ]


def _task_collection(context: UptimeContext) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for entry in context.live.job_entries:
        if entry.kind != "task":
            continue
        path = context.root / entry.path
        document = work.TaskFile.load(path, persist_id=True)
        items.append(
            {
                "id": document.task_id(),
                "name": entry.name,
                "body": document.body,
                "status": document.status,
                "requester": document.requester,
                "thread_id": document.thread_id(),
                "paused": document.paused,
                "path": str(path),
            }
        )
    return items


def _chore_collection(context: UptimeContext) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for entry in context.live.job_entries:
        if entry.kind != "chore":
            continue
        path = context.root / entry.path
        document = work.ChoreFile.load(path)
        items.append(
            {
                "id": entry.name,
                "title": document.title,
                "body": document.body,
                "rrule": document.rrule,
                "paused": document.paused,
                "path": str(path),
            }
        )
    return items


def _cap_summary_item(context: UptimeContext, entry: PreparedEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "description": str(entry.meta["description"]) if entry.meta.get("description") is not None else None,
        "path": entry.path,
        "ref": entry.ref if entry.source.form == "remote" else None,
        "scope": _entry_scope(context, entry),
        "source": entry.source.form,
        "editable": True,
    }


def _cap_detail_item(context: UptimeContext, entry: PreparedEntry) -> dict[str, object]:
    item = _cap_summary_item(context, entry)
    content_path = context.root / entry.path
    content = content_path.read_text(encoding="utf-8") if content_path.is_file() else None
    files = None
    if entry.shape == "dir":
        files = sorted(
            str(item.relative_to(content_path.parent))
            for item in content_path.parent.rglob("*")
            if item.is_file()
        )
    return {
        **item,
        "kind": entry.kind,
        "content": content,
        "entry_path": entry.path,
        "files": files,
    }


def _live_entry_by_name(context: UptimeContext, *, kind: CapKind, name: str) -> PreparedEntry:
    for entry in context.live.cap_entries:
        if entry.kind == kind and entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _run_item(run: RunRecord, *, steps: Sequence) -> dict[str, object]:
    detail = run_detail_from_record(run, steps=steps)
    input_text = message_summary(detail.input.parts) if detail.input is not None else ""
    last_step_message = next(
        (item.message for item in reversed(detail.output.steps) if item.message is not None),
        None,
    )
    summary = (
        message_summary(last_step_message.parts)
        if last_step_message is not None
        else input_text
    )
    return {
        "id": run.run_id,
        "origin": run.origin,
        "thread_id": run.thread_id,
        "input_text": input_text,
        "summary": summary,
        "status": run.status,
        "type": "run",
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.finished_at or run.started_at,
    }


def _run_detail_data(run_detail: RunDetail) -> dict[str, object]:
    output_steps: list[dict[str, object]] = []
    for item in run_detail.output.steps:
        payload = asdict(item)
        if item.message is not None:
            payload["message"] = item.message.to_data()
        output_steps.append(payload)
    return {
        "info": asdict(run_detail.info),
        "input": run_detail.input.to_data() if run_detail.input is not None else None,
        "output": {
            "status": run_detail.output.status,
            "error": run_detail.output.error,
            "steps": output_steps,
        },
    }


def _profile_metrics(context: UptimeContext) -> dict[str, object]:
    threads = _thread_items(context)
    runs = context.store.list_runs(limit=None)
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
    thread_counts = {"chat": 0, "chore": 0, "task": 0}
    step_total = 0
    model_call_total = 0
    tool_call_total = 0
    runtime_total = 0
    input_tokens = 0
    output_tokens = 0

    for thread in threads:
        thread_counts[_thread_metric_kind(thread)] += 1

    for step_items in steps_by_run.values():
        for step in step_items:
            step_total += 1
            if step.kind == "model_call":
                model_call_total += 1
                if isinstance(step.payload, ModelCallStepPayload):
                    input_tokens += step.payload.input_tokens
                    output_tokens += step.payload.output_tokens
            elif step.kind == "tool_call":
                tool_call_total += 1
            else:
                runtime_total += 1

    return {
        "threads": {
            "total": len(threads),
            **thread_counts,
        },
        "steps": {
            "total": step_total,
            "model_call": model_call_total,
            "tool_call": tool_call_total,
            "runtime": runtime_total,
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
    }


def _profile_environment(
    context: UptimeContext,
    *,
    runtime_state: dict[str, object],
) -> dict[str, object]:
    return {
        "sandbox": _runtime_sandbox_spec(runtime_state),
        "home": str(context.home),
        "endpoint": _runtime_endpoint(context, runtime_state=runtime_state),
    }


def _run_detail(context: UptimeContext, run: RunRecord):
    raw_steps = context.store.list_steps(run_id=run.run_id)
    return run_detail_from_record(run, steps=raw_steps)


def _run_messages(
    context: UptimeContext,
    *,
    run: RunRecord,
    raw_steps: Sequence,
) -> list[MessageData]:
    del context
    return run_message_data(run, steps=raw_steps)


def _thread_items(context: UptimeContext) -> list[ThreadInfo]:
    runs = context.store.list_runs(limit=None)
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
    grouped_runs: dict[str, list[RunRecord]] = {}
    for run in runs:
        grouped_runs.setdefault(run.thread_id, []).append(run)
    items: list[ThreadInfo] = []
    for thread_id, runs in grouped_runs.items():
        ordered_runs = sorted(runs, key=lambda item: item.created_at)
        items.append(
            thread_info_from_runs(
                thread_id,
                ordered_runs,
                steps_by_run=steps_by_run,
            )
        )
    return sorted(items, key=lambda item: item.updated_at, reverse=True)


def _thread_metric_kind(thread: ThreadInfo) -> Literal["chat", "chore", "task"]:
    if thread.id.startswith("task:") or thread.origin == "task":
        return "task"
    if thread.id.startswith("chore:") or thread.origin == "chore":
        return "chore"
    return "chat"


def _runtime_endpoint(
    context: UptimeContext,
    *,
    runtime_state: dict[str, object] | None = None,
) -> str | None:
    if runtime_state is not None:
        endpoint = runtime_state.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    host = context.config.get("server.host")
    port = context.config.get("server.port")
    if isinstance(host, str) and isinstance(port, int):
        return f"http://{host}:{port}"
    return None


def _runtime_sandbox_spec(runtime_state: dict[str, object]) -> str:
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if isinstance(selector, dict):
            selector_data = {str(key): value for key, value in selector.items()}
            driver = selector_data.get("driver")
            target = selector_data.get("target")
            if isinstance(driver, str) and driver.strip():
                if isinstance(target, str) and target.strip():
                    return f"{driver.strip()}:{target.strip()}"
                return driver.strip()
    return "none"


def _channel_items(context: UptimeContext) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in sorted(context.channel_bindings):
        binding = context.channel_bindings[name]
        plugin = context.channel_plugins.get(name)
        channel_context = context.channel_context(name)
        state_path = channel_context.room / "state.json"
        health = (
            plugin.health(channel_context).to_data()
            if plugin is not None
            else {"ok": False, "detail": "not loaded", "meta": {}}
        )
        items.append(
            {
                "name": name,
                "plugin": binding.plugin,
                "config_keys": sorted(binding.config),
                "poll_state_path": str(state_path),
                "health": health,
            }
        )
    return items


async def _events_stream() -> AsyncIterator[str]:
    yield ": ok\n\n"


def _collection_kind(collection: str) -> CapKind:
    kind = COLLECTION_TO_KIND.get(collection)
    if kind is None:
        raise HTTPException(status_code=404, detail=f"unsupported cap collection: {collection}")
    return kind


def _entry_scope(context: UptimeContext, entry: PreparedEntry) -> str:
    agent_prefix = f"agents/{context.name}/"
    return "agent" if entry.path.startswith(agent_prefix) else "global"


def _authored_entries(context: UptimeContext, *, scope: str) -> tuple[PreparedEntry, ...]:
    from .. import caps

    return caps.list_entries(context.root, context.name, scope=cast(Literal["global", "agent"], scope))


def _select_loops(enabled_loops: Sequence[str], allowed: Container[str]) -> list[str]:
    return [loop for loop in enabled_loops if loop in allowed]


def _run_outcome_data(result: RunOutcome) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "group": result.group,
        "origin": result.origin,
        "input_text": result.input_text,
        "thunk_name": result.thunk_name,
        "thread_id": result.thread_id,
        "status": result.status,
        "output_text": result.output_text,
        "error": result.error,
        "live_fingerprint": result.live_fingerprint,
    }


def _runner_in_flight(runner_snapshot: dict[str, object]) -> int:
    concurrency_groups = cast(list[dict[str, object]], runner_snapshot["concurrency_groups"])
    return sum(cast(int, item["in_flight"]) for item in concurrency_groups)
