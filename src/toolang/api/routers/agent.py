"""Formal agent inspection routes."""

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from toolang.api.app import ApiContextDep
from toolang.common.errors import ToolangError
from toolang.execution.executor.prepare import (
    effective_agics,
    effective_origin_model_selectors,
    select_origin_agic,
)
from toolang.execution.history import RunHistory
from toolang.execution.schemas import ThreadInfo
from toolang.plugin.models.resolution import selectable_model_targets
from toolang.up import process as agents


router = APIRouter(tags=["agent"])


@router.get("/profile", summary="Get Profile")
def profile(context: ApiContextDep) -> dict[str, object]:
    runtime_state = agents.AgentProcess(context.root, context.name).state() or {}
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


@router.get("/models", summary="List Agent Models")
def models(context: ApiContextDep) -> dict[str, object]:
    try:
        selectors = effective_origin_model_selectors(
            context.executor,
            state=context.state_watcher.current(),
            origin="chat",
        )
        targets = selectable_model_targets(
            providers=context.executor.setup.model_providers,
            aliases=context.executor.model_aliases,
            environ=context.executor.model_environ,
            selectors=selectors,
            cache_dir=context.executor.model_cache_dir,
            refresh=context.executor.model_cache_refresh,
        )
    except ToolangError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "default": selectors[0] if selectors else None,
        "items": [
            _model_item(selector=selector, target=target)
            for selector, target in targets
        ],
    }


@router.get("/agics", summary="List Agent Agics")
def agics(context: ApiContextDep) -> dict[str, object]:
    program = context.state_watcher.current().program
    return {
        "default": _default_agic_name(program, origin="chat"),
        "items": [{"name": agic.name} for agic in effective_agics(program)],
    }


@router.get("/flows", summary="List Agent Flows")
def flows(context: ApiContextDep) -> dict[str, object]:
    program = context.state_watcher.current().program
    return {
        "default": None,
        "items": [{"name": flow.name} for flow in program.flows],
    }


def _profile_metrics(context) -> dict[str, object]:
    threads = RunHistory(context.executor.store).list_threads(limit=None)
    runs = context.executor.store.list_runs(limit=None)
    steps_by_run = context.executor.store.list_steps_for_runs(
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
                usage = step.noted.get("usage")
                if isinstance(usage, Mapping):
                    input_tokens += int(usage.get("input_tokens", 0) or 0)
                    output_tokens += int(usage.get("output_tokens", 0) or 0)
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
    context, *, runtime_state: dict[str, object]
) -> dict[str, object]:
    return {
        "sandbox": _runtime_sandbox_spec(runtime_state),
        "home": str(context.home),
        "endpoint": _runtime_endpoint(context, runtime_state=runtime_state),
    }


def _runtime_endpoint(
    context, *, runtime_state: dict[str, object] | None = None
) -> str:
    if runtime_state is not None:
        endpoint = runtime_state.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    return f"http://{context.host}:{context.port}"


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


def _default_agic_name(program: Any, *, origin: str) -> str | None:
    try:
        return select_origin_agic(program, origin=origin).name
    except ToolangError:
        return None
