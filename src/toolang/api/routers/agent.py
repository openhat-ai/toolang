"""Formal agent inspection routes."""

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from toolang.api.app import AgentCoreDep
from toolang.common.errors import ToolangError
from toolang.execution.runnables import effective_agics, runnable_binding_defaults
from toolang.execution.schemas import ThreadInfo
from toolang.execution.executor.resources import agent_model_targets
from toolang.up import AgentCore, process as agents


router = APIRouter(tags=["agent"])


@router.get("/profile", summary="Get Profile")
def profile(core: AgentCoreDep) -> dict[str, object]:
    runtime_state = agents.AgentProcess(core.layout).state() or {}
    return {
        "agent": core.layout.name,
        "display_name": core.layout.name,
        "title": None,
        "summary": None,
        "description": None,
        "avatar": None,
        "environment": _profile_environment(core, runtime_state=runtime_state),
        "metrics": _profile_metrics(core),
    }


@router.get("/models", summary="List Agent Models")
def models(core: AgentCoreDep) -> dict[str, object]:
    try:
        setup = core.setup.current()
        state = core.state.current()
        resolved_default, targets = agent_model_targets(
            setup, state, setup.resource_filter
        )
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "default": setup.bindings.model or resolved_default,
        "items": [
            _model_item(selector=selector, target=target)
            for selector, target in targets
        ],
    }


@router.get("/agics", summary="List Agent Agics")
def agics(core: AgentCoreDep) -> dict[str, object]:
    setup = core.setup.current()
    program = core.state.current().program
    default, _flow = _runnable_defaults(program, setup.bindings.runnable)
    return {
        "default": default,
        "items": [{"name": agic.name} for agic in effective_agics(program)],
    }


@router.get("/flows", summary="List Agent Flows")
def flows(core: AgentCoreDep) -> dict[str, object]:
    setup = core.setup.current()
    program = core.state.current().program
    _agic, default = _runnable_defaults(program, setup.bindings.runnable)
    return {
        "default": default,
        "items": [{"name": flow.name} for flow in program.flows],
    }


def _profile_metrics(core: AgentCore) -> dict[str, object]:
    threads = core.history.list_threads(limit=None)
    runs = core.store.list_runs(limit=None)
    steps_by_run = core.store.list_steps_for_runs(
        run_ids=tuple(item.id for item in runs)
    )
    thread_counts = {"chat": 0, "chore": 0, "task": 0}
    step_total = 0
    model_total = 0
    tool_total = 0
    system_total = 0
    input_tokens = 0
    output_tokens = 0

    for thread in threads:
        thread_counts[_thread_metric_kind(thread)] += 1

    for step_items in steps_by_run.values():
        for step in step_items:
            step_total += 1
            if step.kind == "model":
                model_total += 1
                tokens = step.noted.get("tokens")
                if isinstance(tokens, Mapping):
                    input_tokens += int(tokens.get("input", 0) or 0)
                    output_tokens += int(tokens.get("output", 0) or 0)
            elif step.kind == "tool":
                tool_total += 1
            else:
                system_total += 1

    return {
        "threads": {"total": len(threads), **thread_counts},
        "steps": {
            "total": step_total,
            "model": model_total,
            "tool": tool_total,
            "system": system_total,
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
    }


def _thread_metric_kind(thread: ThreadInfo) -> Literal["chat", "chore", "task"]:
    if thread.id.startswith("task_") or thread.origin == "task":
        return "task"
    if thread.id.startswith("chore_") or thread.origin == "chore":
        return "chore"
    return "chat"


def _profile_environment(
    core: AgentCore, *, runtime_state: dict[str, object]
) -> dict[str, object]:
    return {
        "sandbox": _runtime_sandbox_spec(runtime_state),
        "home": str(core.layout.home),
        "endpoint": _runtime_endpoint(runtime_state),
    }


def _runtime_endpoint(runtime_state: dict[str, object]) -> str | None:
    endpoint = runtime_state.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    return None


def _runtime_sandbox_spec(runtime_state: dict[str, object]) -> str:
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, str) and sandbox.strip():
        return sandbox.strip()
    return "none"


def _model_item(*, selector: str, target: Any) -> dict[str, object]:
    return {
        "selector": selector,
        "name": target.name,
        "ref": target.ref,
        "provider": target.provider,
        "model": target.model,
        "adapter": target.adapter,
        "tools": target.tools,
        "streaming": target.streaming,
    }


def _runnable_defaults(
    program: Any,
    binding: str | None,
) -> tuple[str | None, str | None]:
    try:
        return runnable_binding_defaults(
            program,
            binding,
            fallback_agic="chat",
        )
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
