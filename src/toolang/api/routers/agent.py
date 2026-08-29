"""Formal agent inspection routes."""

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from toolang.api.app import AgentCoreDep
from toolang.api.schemas import RuntimeIdentityPayload, RuntimeSandboxPayload
from toolang.common.errors import ToolangError
from toolang.common.version import toolang_version
from toolang.execution.runnables import (
    parse_runnable_ref,
    resolve_state_runnable,
    runnable_binding_defaults,
)
from toolang.execution.schemas import ThreadInfo
from toolang.execution.types import ModelStepNoted
from toolang.execution.executor.resources import agent_model_targets
from toolang.up import AgentCore, process as agents
from toolang.state.state import state_program


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
        "runtime": _profile_runtime(runtime_state=runtime_state).model_dump(),
        "environment": _profile_environment(core, runtime_state=runtime_state),
        "metrics": _profile_metrics(core),
    }


@router.get("/models", summary="List Agent Models")
def models(core: AgentCoreDep) -> dict[str, object]:
    try:
        setup = core.setup.current()
        state = core.state.current()
        resolved_default, targets = agent_model_targets(setup, state, setup.ceiling)
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
async def agics(core: AgentCoreDep) -> dict[str, object]:
    setup = core.setup.current()
    state = await _fresh_state(core)
    default, _flow = _runnable_defaults(state, setup.bindings.runnable)
    return {
        "default": default,
        "items": [{"name": name} for name in state.agics],
    }


@router.get("/flows", summary="List Agent Flows")
async def flows(core: AgentCoreDep) -> dict[str, object]:
    setup = core.setup.current()
    state = await _fresh_state(core)
    _agic, default = _runnable_defaults(state, setup.bindings.runnable)
    return {
        "default": default,
        "items": [{"name": name} for name in state.flows],
    }


@router.get("/prompt-completions", summary="List Prompt Completions")
async def prompt_completions(
    core: AgentCoreDep,
    runnable: str | None = Query(default=None),
) -> dict[str, object]:
    setup = core.setup.current()
    state = await _fresh_state(core)
    selected = runnable or setup.bindings.runnable
    if selected is None:
        default_agic, default_flow = _runnable_defaults(state, None)
        if default_agic is not None:
            selected = f"agic:{default_agic}"
        elif default_flow is not None:  # pragma: no cover - exclusive fallback
            selected = f"flow:{default_flow}"
        else:  # pragma: no cover - runnable fallback invariant
            raise HTTPException(status_code=500, detail="chat has no default runnable")
    try:
        name, kind = parse_runnable_ref(selected)
        module, _declaration = resolve_state_runnable(state, name, kind=kind)
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [
            {
                "name": prompt.name,
                "params": [
                    {"name": parameter.name, "optional": parameter.optional}
                    for parameter in prompt.params
                ],
            }
            for prompt in state_program(state, module).caps
            if prompt.kind == "prompt"
        ]
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
                if isinstance(step.noted, ModelStepNoted):
                    if step.noted.accounting is not None:
                        input_tokens += step.noted.accounting.input_tokens
                        output_tokens += step.noted.accounting.output_tokens
                    elif step.noted.tokens:
                        input_tokens += step.noted.tokens.input
                        output_tokens += step.noted.tokens.output
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


def _profile_runtime(*, runtime_state: dict[str, object]) -> RuntimeIdentityPayload:
    sandbox_spec = _runtime_sandbox_spec(runtime_state)
    driver = _runtime_token(sandbox_spec.partition(":")[0], label="sandbox driver")
    return RuntimeIdentityPayload(
        version=_runtime_label(toolang_version(), label="Toolang version"),
        sandbox=RuntimeSandboxPayload(
            driver=driver,
            selector=_runtime_label(sandbox_spec, label="sandbox selector"),
            instance=_runtime_instance(runtime_state, driver=driver),
            description=_runtime_description(runtime_state, driver=driver),
        ),
    )


def _runtime_instance(runtime_state: dict[str, object], *, driver: str) -> str | None:
    raw = runtime_state.get("sandbox_instance")
    if driver != "docker":
        if raw is not None:
            raise HTTPException(
                status_code=500,
                detail="runtime sandbox instance is invalid",
            )
        return None
    if not isinstance(raw, str) or len(raw.strip()) < 12:
        raise HTTPException(
            status_code=500,
            detail="runtime sandbox instance is unavailable",
        )
    return _runtime_token(raw.strip(), label="sandbox instance")


def _runtime_description(
    runtime_state: dict[str, object], *, driver: str
) -> str | None:
    raw = runtime_state.get("sandbox_description")
    if driver == "docker":
        if raw is not None:
            raise HTTPException(
                status_code=500,
                detail="runtime sandbox description is invalid",
            )
        return None
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=500,
            detail="runtime sandbox description is unavailable",
        )
    text = raw.strip()
    if not text or text != raw or not text.isprintable():
        raise HTTPException(
            status_code=500,
            detail="runtime sandbox description is invalid",
        )
    return text


def _runtime_token(value: str, *, label: str) -> str:
    text = _runtime_label(value, label=label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text) is None:
        raise HTTPException(status_code=500, detail=f"invalid runtime {label}")
    return text


def _runtime_label(value: str, *, label: str) -> str:
    text = value.strip()
    if (
        not text
        or text != value
        or not text.isprintable()
        or any(character.isspace() for character in text)
        or any(character in text for character in ",()")
    ):
        raise HTTPException(status_code=500, detail=f"invalid runtime {label}")
    return text


def _runtime_endpoint(runtime_state: dict[str, object]) -> str | None:
    endpoint = runtime_state.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    return None


def _runtime_sandbox_spec(runtime_state: dict[str, object]) -> str:
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, str) and sandbox.strip():
        return sandbox.strip()
    return "host"


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


async def _fresh_state(core: AgentCore) -> Any:
    refresh = getattr(core.state, "refresh", None)
    return await refresh() if callable(refresh) else core.state.current()
