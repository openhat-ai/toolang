"""CLI inspect command implementation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import click
import tomli_w
import typer

from ... import agents
from ...base.types.message import parts_to_data
from ...execution.db import ExecutionStore, execution_db_path
from ...execution.detail import run_detail_from_record, thread_info_from_record, thread_info_from_runs
from ...execution.labels import child_call_summary, executable_label, flow_op_summary
from ...execution.projection import child_run_ids
from ...execution.records import step_input_items_to_data, step_payload_to_data
from ..utils import (
    _context_root,
    _required_prefix_agent,
    _ui_base_url,
)


InspectView = Literal["human", "json", "toml"]
InspectData = dict[str, Any]
StepData = dict[str, Any]


@dataclass(frozen=True, slots=True)
class InspectTarget:
    kind: Literal["thread", "run"]
    identifier: str
    path: tuple[int, ...] = ()

    @property
    def path_label(self) -> str | None:
        if not self.path:
            return None
        return ".".join(str(item) for item in self.path)


def inspect_command(
    ctx: typer.Context,
    target: str,
    *,
    tree: bool,
    depth: int,
    limit: int,
    view: InspectView,
) -> None:
    """Inspect one thread, run, or run step path."""

    if limit < 1:
        raise click.ClickException("--limit must be at least 1")
    if depth < 1:
        raise click.ClickException("--depth must be at least 1")
    parsed = parse_inspect_target(target)
    raw = _inspect_detail(ctx, parsed.identifier, limit=limit, include_thread=parsed.kind == "run")
    document = preprocess_inspect(raw, target=parsed)
    render_inspect(document, view=view, tree=tree, depth=depth)


def parse_inspect_target(target: str) -> InspectTarget:
    identifier, separator, raw_path = target.partition(":")
    identifier = identifier.strip()
    if not identifier:
        raise click.ClickException("inspect target is required")
    if separator and not identifier.startswith("run_"):
        raise click.ClickException("step paths are only supported for run targets")
    path = _parse_inspect_step_path(raw_path) if separator else ()
    kind: Literal["thread", "run"] = "run" if identifier.startswith("run_") else "thread"
    return InspectTarget(kind=kind, identifier=identifier, path=path)


def preprocess_inspect(raw: Mapping[str, Any], *, target: InspectTarget) -> InspectData:
    """Convert API detail payloads into one normalized inspect document."""

    if raw.get("kind") == "thread":
        if target.path:
            raise click.ClickException("step paths are only supported for run targets")
        return {
            "kind": "thread",
            "target": target.identifier,
            "thread": _preprocess_thread(_mapping(raw.get("thread"))),
        }

    run_payload = _mapping(raw.get("run"))
    thread_payload = _mapping(raw.get("thread"))
    run_by_id = _inspect_thread_run_map(thread_payload, fallback=run_payload)
    run_id = _text(_mapping(run_payload.get("info")).get("id"))
    display_payload = run_by_id.get(run_id, run_payload) if run_id is not None else run_payload
    run = _preprocess_run(display_payload)
    if not target.path:
        steps = _step_tree(display_payload, run_by_id=run_by_id, full=False)
        return {
            "kind": "run",
            "target": target.identifier,
            "run": run,
            "steps": steps,
        }
    full_steps = _step_tree(display_payload, run_by_id=run_by_id, full=True)
    step = _find_step(full_steps, target.path_label or "")
    if step is None:
        raise click.ClickException(f"step path not found: {target.path_label}")
    return {
        "kind": "step",
        "target": f"{target.identifier}:{target.path_label}",
        "run": run,
        "step": step,
    }


def render_inspect(
    document: InspectData,
    *,
    view: InspectView,
    tree: bool,
    depth: int,
) -> None:
    if view == "json":
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))
        return
    if view == "toml":
        typer.echo(tomli_w.dumps(_toml_document(document)))
        return
    _render_human(document, tree=tree, depth=depth)


def _parse_inspect_step_path(raw_path: str) -> tuple[int, ...]:
    if not raw_path:
        raise click.ClickException("step path is required after ':'")
    path: list[int] = []
    for piece in raw_path.split("."):
        if not piece.isdecimal():
            raise click.ClickException(f"invalid step path: {raw_path}")
        value = int(piece)
        if value < 1:
            raise click.ClickException(f"invalid step path: {raw_path}")
        path.append(value)
    return tuple(path)


def _inspect_detail(ctx: typer.Context, target: str, *, limit: int, include_thread: bool = True) -> dict[str, Any]:
    if target.startswith("run_"):
        run = _inspect_run_detail(ctx, target)
        info = _mapping(run.get("info"))
        thread_id = _text(info.get("thread_id"))
        thread = _inspect_thread_detail(ctx, thread_id, limit=limit) if include_thread and thread_id else None
        return {"kind": "run", "target": target, "run": run, "thread": thread}
    thread = _inspect_thread_detail(ctx, target, limit=limit)
    return {"kind": "thread", "target": target, "thread": thread}


def _inspect_run_detail(ctx: typer.Context, run_id: str) -> dict[str, Any]:
    return _runtime_json_or_offline(
        ctx,
        f"/api/v1/runs/{run_id}",
        lambda: _offline_run_detail_json(ctx, run_id),
    )


def _inspect_thread_detail(ctx: typer.Context, thread_id: str, *, limit: int) -> dict[str, Any]:
    return _runtime_json_or_offline(
        ctx,
        f"/api/v1/threads/{thread_id}?{urlencode({'limit': str(limit)})}",
        lambda: _offline_thread_detail_json(ctx, thread_id, limit=limit),
    )


def _runtime_base_url(ctx: typer.Context) -> str:
    agent_name = _required_prefix_agent(ctx, command_name=str(ctx.info_name or "runtime"))
    status = agents.get_agent_status(_context_root(ctx), agent_name, ui_base_url=_ui_base_url())
    if status is None or status.status != "running" or status.endpoint is None:
        raise click.ClickException(f"agent is not running: {agent_name}")
    return status.endpoint.rstrip("/")


def _runtime_json(ctx: typer.Context, path: str) -> dict[str, Any]:
    url = f"{_runtime_base_url(ctx)}{path}"
    try:
        with urlopen(url, timeout=30) as response:
            return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _runtime_json_or_offline(
    ctx: typer.Context,
    path: str,
    offline: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    try:
        return _runtime_json(ctx, path)
    except click.ClickException as exc:
        result = offline()
        if result is None:
            raise exc
        return result


def _open_offline_execution_store(ctx: typer.Context) -> ExecutionStore | None:
    agent_name = _required_prefix_agent(ctx, command_name=str(ctx.info_name or "runtime"))
    path = execution_db_path(_context_root(ctx), agent_name)
    if not path.exists():
        return None
    return ExecutionStore(path)


def _offline_run_detail_json(ctx: typer.Context, run_id: str) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        run = store.get_run(run_id=run_id)
        if run is None:
            raise click.ClickException(f"run not found: {run_id}")
        return _run_detail_json(store, run)
    finally:
        store.close()


def _offline_thread_detail_json(ctx: typer.Context, thread_id: str, *, limit: int) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        runs = store.list_thread_runs_chronological(thread_id=thread_id, limit=limit)
        thread_record = store.get_thread(thread_id=thread_id)
        if not runs and thread_record is None:
            raise click.ClickException(f"thread not found: {thread_id}")
        if runs:
            all_runs = store.list_thread_runs_chronological(thread_id=thread_id, limit=None)
            steps_by_run = store.list_steps_for_runs(run_ids=tuple(item.run_id for item in all_runs))
            commands_by_run = {run.run_id: store.list_commands(run_id=run.run_id) for run in all_runs}
            info = thread_info_from_runs(
                thread_id,
                all_runs,
                commands_by_run=commands_by_run,
                steps_by_run=steps_by_run,
                thread=thread_record,
            )
        else:
            info = thread_info_from_record(cast(Any, thread_record))
        return {
            "info": asdict(info),
            "runs": [_run_detail_json(store, run) for run in runs],
            "event_cursor": store.latest_event_cursor(domain="thread", domain_id=thread_id),
        }
    finally:
        store.close()


def _run_detail_json(store: ExecutionStore, run: Any) -> dict[str, Any]:
    detail = run_detail_from_record(
        run,
        inputs=store.list_commands(run_id=run.run_id),
        steps=store.list_steps(run_id=run.run_id),
    )
    data = {
        "info": asdict(detail.info),
        "input": detail.input.to_data() if detail.input is not None else None,
        "inputs": [
            {
                "record": asdict(item.record),
                "message": item.message.to_data() if item.message is not None else None,
            }
            for item in detail.inputs
        ],
        "output": {
            "status": detail.output.status,
            "error": detail.output.error,
            "steps": [
                {
                    "record": _step_record_json(item.record),
                    "message": item.message.to_data() if item.message is not None else None,
                }
                for item in detail.output.steps
            ],
        },
    }
    prompts = _run_prompt_bodies(store, data)
    if prompts:
        data["prompts"] = prompts
    return data


def _step_record_json(step: Any) -> dict[str, Any]:
    return {
        "run_id": step.run_id,
        "step_index": step.step_index,
        "kind": step.kind,
        "status": step.status,
        "input": step_input_items_to_data(step.input),
        "output": parts_to_data(step.output),
        "payload": step_payload_to_data(step.payload),
        "error": step.error,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def _run_prompt_bodies(store: ExecutionStore, run: Mapping[str, Any]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for prompt_hash in _run_prompt_hashes(run):
        body = store.get_prompt(prompt_hash=prompt_hash)
        if body is not None:
            prompts[prompt_hash] = body
    return prompts


def _run_prompt_hashes(run: Mapping[str, Any]) -> tuple[str, ...]:
    hashes: list[str] = []
    for step in _run_steps(run):
        payload = _mapping(_mapping(step.get("record")).get("payload"))
        for key in ("instruct", "context"):
            value = _text(payload.get(key))
            if value is not None and value not in hashes:
                hashes.append(value)
    return tuple(hashes)


def _preprocess_thread(thread: Mapping[str, Any]) -> InspectData:
    info = dict(_mapping(thread.get("info")))
    runs = [_mapping(item) for item in _list(thread.get("runs"))]
    thread_id = _text(info.get("id")) or "-"
    return {
        "id": thread_id,
        "status": _text(info.get("status")) or "-",
        "title": _text(info.get("title")),
        "run_count": _int_or_none(info.get("run_count")),
        "info": info,
        "runs": [_preprocess_thread_run(run) for run in _top_level_runs(runs)],
    }


def _preprocess_thread_run(run: Mapping[str, Any]) -> InspectData:
    info = dict(_mapping(run.get("info")))
    output = _mapping(run.get("output"))
    target = executable_label(
        _text(info.get("executable_kind")) or "run",
        _text(info.get("executable_name")),
        metadata=_mapping(info.get("metadata")),
    )
    return {
        "id": _text(info.get("id")) or "-",
        "status": _display_run_status(output.get("status")),
        "target": target,
        "elapsed": _elapsed(_text(info.get("started_at")), _text(info.get("finished_at"))) or None,
        "failure": _failure_summary(run) or None,
        "info": info,
    }


def _preprocess_run(run: Mapping[str, Any]) -> InspectData:
    info = dict(_mapping(run.get("info")))
    output = _mapping(run.get("output"))
    target = executable_label(
        _text(info.get("executable_kind")) or "run",
        _text(info.get("executable_name")),
        metadata=_mapping(info.get("metadata")),
    )
    return {
        "id": _text(info.get("id")) or "-",
        "thread_id": _text(info.get("thread_id")) or "-",
        "status": _display_run_status(output.get("status")),
        "target": target,
        "input": dict(_mapping(run.get("input"))) or None,
        "input_summary": _message_summary(_mapping(run.get("input"))),
        "failure": _failure_summary(run) or None,
        "info": info,
    }


def _preprocess_step(
    run: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    path: tuple[int, ...],
    run_by_id: Mapping[str, Mapping[str, Any]],
    full: bool,
) -> StepData:
    record = _mapping(step.get("record"))
    message = _mapping(step.get("message"))
    kind = _text(record.get("kind")) or "step"
    status = _display_run_status(record.get("status"))
    run_id = _text(_mapping(run.get("info")).get("id")) or "-"
    children: list[StepData] = []
    step_index = _int_or_none(record.get("step_index"))
    if step_index is not None:
        for child_run in _child_runs_for_step(run_id, step_index, step=step, run_by_id=run_by_id):
            children.extend(_step_tree(child_run, run_by_id=run_by_id, path_prefix=path, full=full))
    data: StepData = {
        "path": _path_label(path),
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "summary": _step_summary(record, message),
        "error": _text(record.get("error")),
        "children": children,
    }
    if not full:
        return data
    input_items = [dict(_mapping(item)) for item in _list(record.get("input"))]
    output = [dict(_mapping(item)) for item in _list(record.get("output"))]
    payload = dict(_mapping(record.get("payload")))
    data["record"] = dict(record)
    if message:
        data["message"] = dict(message)
    data.update(_step_detail_fields(kind, input_items=input_items, output=output, payload=payload, children=children, run=run))
    return data


def _step_detail_fields(
    kind: str,
    *,
    input_items: Sequence[Mapping[str, Any]],
    output: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    if children:
        return {
            "variant": "compound",
            "input_refs": [dict(item) for item in input_items],
            "output": [dict(item) for item in output],
            "child_steps": [
                {"path": child.get("path"), "kind": child.get("kind"), "status": child.get("status")}
                for child in children
            ],
        }
    if kind == "model":
        return _model_step_fields(input_items=input_items, output=output, payload=payload, run=run)
    if kind == "tool":
        return _tool_step_fields(input_items=input_items, output=output, run=run)
    return {
        "variant": "generic",
        "input_refs": [dict(item) for item in input_items],
        "output": [dict(item) for item in output],
    }


def _model_step_fields(
    *,
    input_items: Sequence[Mapping[str, Any]],
    output: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    prompts = _mapping(run.get("prompts"))
    request = _mapping(payload.get("adapter_request"))
    if not request:
        request = _reconstructed_model_request(input_items=input_items, payload=payload, run=run, prompts=prompts)
    fields: dict[str, Any] = {
        "variant": "model",
        "model": {
            "ref": payload.get("model_ref"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "adapter": payload.get("adapter"),
            "base_url": payload.get("base_url"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
        },
        "adapter_request": request,
        "output": [dict(item) for item in output],
    }
    if reasoning := _text(payload.get("reasoning_content")):
        fields["reasoning_content"] = reasoning
    return fields


def _tool_step_fields(
    *,
    input_items: Sequence[Mapping[str, Any]],
    output: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    other_parts: list[dict[str, Any]] = []
    request_parts = _tool_call_parts_from_input_refs(run, input_items)
    for part in output:
        if part.get("type") != "tool_result":
            other_parts.append(dict(part))
            continue
        call_id = _text(part.get("tool_call_id")) or _text(part.get("call_id"))
        request = request_parts.get(call_id or "")
        calls.append(
            {
                "name": part.get("tool_name") or part.get("tool_family") or (request or {}).get("tool_name"),
                "family": part.get("tool_family") or (request or {}).get("tool_family"),
                "call_id": call_id,
                "input": part.get("input") if part.get("input") is not None else (request or {}).get("input"),
                "result": part.get("output"),
                "error": part.get("error"),
            }
        )
    return {
        "variant": "tool",
        "input_refs": [dict(item) for item in input_items],
        "tool_calls": calls,
        "other_output": other_parts,
    }


def _tool_call_parts_from_input_refs(
    run: Mapping[str, Any],
    input_items: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    parts_by_call_id: dict[str, Mapping[str, Any]] = {}
    for typed in input_items:
        if _text(typed.get("kind")) != "step":
            continue
        step_index = _int_or_none(typed.get("index"))
        if step_index is None:
            continue
        step = _run_step_by_index(run, step_index)
        if step is None:
            continue
        record = _mapping(step.get("record"))
        parts = [_mapping(item) for item in _list(record.get("output"))]
        part_index = _int_or_none(typed.get("part"))
        if part_index is not None:
            parts = parts[part_index : part_index + 1] if 0 <= part_index < len(parts) else []
        for part in parts:
            if part.get("type") != "tool_call":
                continue
            call_id = _text(part.get("tool_call_id")) or _text(part.get("call_id"))
            if call_id is not None:
                parts_by_call_id[call_id] = part
    return parts_by_call_id


def _step_tree(
    run: Mapping[str, Any],
    *,
    run_by_id: Mapping[str, Mapping[str, Any]],
    path_prefix: tuple[int, ...] = (),
    full: bool,
) -> list[StepData]:
    nodes: list[StepData] = []
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        step_index = _int_or_none(record.get("step_index"))
        if step_index is None:
            continue
        path = (*path_prefix, step_index)
        nodes.append(_preprocess_step(run, step, path=path, run_by_id=run_by_id, full=full))
    return nodes


def _child_runs_for_step(
    run_id: str,
    step_index: int,
    *,
    step: Mapping[str, Any],
    run_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    child_runs: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    record = _mapping(step.get("record"))
    payload = _mapping(record.get("payload"))
    for child_id in child_run_ids(payload, record):
        child = run_by_id.get(child_id)
        if child is not None and child_id not in seen:
            child_runs.append(child)
            seen.add(child_id)
    for child in run_by_id.values():
        info = _mapping(child.get("info"))
        child_id = _text(info.get("id"))
        if child_id is None or child_id in seen:
            continue
        if _text(info.get("parent_run_id")) != run_id:
            continue
        if _int_or_none(info.get("parent_step_index")) != step_index:
            continue
        child_runs.append(child)
        seen.add(child_id)
    return child_runs


def _find_step(nodes: Sequence[Mapping[str, Any]], path: str) -> StepData | None:
    for node in nodes:
        if node.get("path") == path:
            return dict(node)
        if path.startswith(f"{node.get('path')}."):
            found = _find_step([_mapping(child) for child in _list(node.get("children"))], path)
            if found is not None:
                return found
    return None


def _reconstructed_model_request(
    *,
    input_items: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    run: Mapping[str, Any],
    prompts: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "instructions": _prompt_body(payload.get("instruct"), prompts=prompts),
        "context": _prompt_body(payload.get("context"), prompts=prompts),
        "messages": _messages_from_input_refs(run, input_items),
        "tools": None,
        "state": None,
    }


def _prompt_body(value: object, *, prompts: Mapping[str, Any]) -> str | None:
    prompt_hash = _text(value)
    if prompt_hash is None:
        return None
    body = prompts.get(prompt_hash)
    return str(body) if body is not None else prompt_hash


def _messages_from_input_refs(
    run: Mapping[str, Any],
    input_items: Sequence[Mapping[str, Any]],
    *,
    seen_steps: set[int] | None = None,
) -> list[Mapping[str, Any]]:
    seen = seen_steps or set()
    messages: list[Mapping[str, Any]] = []
    for typed in input_items:
        kind = _text(typed.get("kind"))
        if kind == "message":
            message = _mapping(typed.get("message"))
            if message:
                messages.append(message)
            continue
        if kind == "command":
            command_message = _command_message(run, _int_or_none(typed.get("index")) or 0)
            if command_message:
                messages.append(command_message)
            continue
        if kind == "step":
            step_index = _int_or_none(typed.get("index"))
            if step_index is None or step_index in seen:
                continue
            seen.add(step_index)
            step = _run_step_by_index(run, step_index)
            if step is None:
                continue
            record = _mapping(step.get("record"))
            messages.extend(_messages_from_input_refs(run, [dict(_mapping(item)) for item in _list(record.get("input"))], seen_steps=seen))
            message = _step_output_message(step, part_index=_int_or_none(typed.get("part")))
            if message:
                messages.append(message)
    return messages


def _command_message(run: Mapping[str, Any], index: int) -> Mapping[str, Any] | None:
    for item in _list(run.get("inputs")):
        typed = _mapping(item)
        record = _mapping(typed.get("record"))
        if _int_or_none(record.get("index")) == index:
            message = _mapping(typed.get("message"))
            return message or None
    if index == 0:
        message = _mapping(run.get("input"))
        return message or None
    return None


def _run_step_by_index(run: Mapping[str, Any], step_index: int) -> Mapping[str, Any] | None:
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        if _int_or_none(record.get("step_index")) == step_index:
            return step
    return None


def _step_output_message(step: Mapping[str, Any], *, part_index: int | None) -> Mapping[str, Any] | None:
    message = _mapping(step.get("message"))
    if message:
        if part_index is None:
            return message
        parts = _list(message.get("parts"))
        if 0 <= part_index < len(parts):
            return {**message, "parts": [parts[part_index]]}
        return message
    record = _mapping(step.get("record"))
    role = _step_output_role(_text(record.get("kind")))
    if role is None:
        return None
    parts = _list(record.get("output"))
    if part_index is not None and 0 <= part_index < len(parts):
        parts = [parts[part_index]]
    if not parts:
        return None
    return {"role": role, "parts": parts}


def _step_output_role(kind: str | None) -> str | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _path_label(path: Sequence[int]) -> str:
    return ".".join(str(item) for item in path)


def _render_human(document: Mapping[str, Any], *, tree: bool, depth: int) -> None:
    if document.get("kind") == "thread":
        _render_human_thread(_mapping(document.get("thread")))
        return
    if document.get("kind") == "step":
        _render_human_step(_mapping(document.get("step")))
        return
    _render_human_run(
        _mapping(document.get("run")),
        [_mapping(step) for step in _list(document.get("steps"))],
        depth=depth if tree else 1,
    )


def _render_human_thread(thread: Mapping[str, Any]) -> None:
    typer.echo(f"thread {_text(thread.get('id')) or '-'}  {_text(thread.get('status')) or '-'}")
    if title := _text(thread.get("title")):
        typer.echo(f"title   {title}")
    run_count = thread.get("run_count")
    if run_count is not None:
        typer.echo(f"runs    {run_count} total")
    typer.echo("runs")
    for run in [_mapping(item) for item in _list(thread.get("runs"))]:
        status = _text(run.get("status")) or ""
        pieces = [_status_mark(status), _text(run.get("id")) or "-", _text(run.get("target")) or "-"]
        if elapsed := _text(run.get("elapsed")):
            pieces.append(elapsed)
        typer.echo(f"  {'  '.join(pieces)}")


def _render_human_run(run: Mapping[str, Any], steps: Sequence[Mapping[str, Any]], *, depth: int) -> None:
    typer.echo(f"run {_text(run.get('id')) or '-'}  {_text(run.get('status')) or '-'}  {_text(run.get('target')) or '-'}")
    if failure := _text(run.get("failure")):
        typer.echo("failure")
        typer.echo(f"  {failure}")
    if input_summary := _text(run.get("input_summary")):
        typer.echo(f"input  {input_summary}")
    if steps:
        typer.echo("steps")
    for step in steps:
        _render_human_step_line(step, depth=depth, level=0)


def _render_human_step_line(step: Mapping[str, Any], *, depth: int, level: int) -> None:
    indent = "  " * (level + 1)
    status = _text(step.get("status")) or ""
    line = f"{indent}{_status_mark(status)} {_text(step.get('path')) or '-'} {_text(step.get('kind')) or 'step'}"
    summary = _text(step.get("summary"))
    if summary and summary != "-":
        line = f"{line}  {summary}"
    typer.echo(line)
    if level + 1 >= depth:
        return
    for child in [_mapping(item) for item in _list(step.get("children"))]:
        _render_human_step_line(child, depth=depth, level=level + 1)


def _render_human_step(step: Mapping[str, Any]) -> None:
    typer.echo(f"step {_text(step.get('path')) or '-'} {_text(step.get('kind')) or 'step'}  {_text(step.get('status')) or '-'}")
    typer.echo(f"run  {_text(step.get('run_id')) or '-'}")
    if error := _text(step.get("error")):
        _render_text_section("error", error)
    variant = _text(step.get("variant"))
    if variant == "model":
        _render_human_model_step(step)
        return
    if variant == "tool":
        _render_human_tool_step(step)
        return
    if variant == "compound":
        _render_section("input_refs", step.get("input_refs"))
        _render_section("output", step.get("output"))
        _render_section("child_steps", step.get("child_steps"))
        return
    _render_section("input_refs", step.get("input_refs"))
    _render_section("output", step.get("output"))


def _render_human_model_step(step: Mapping[str, Any]) -> None:
    typer.echo("model")
    for key, value in _mapping(step.get("model")).items():
        _render_kv(str(key), value)
    _render_section("adapter_request", step.get("adapter_request"))
    _render_section("output", step.get("output"))
    if reasoning := _text(step.get("reasoning_content")):
        _render_text_section("reasoning_content", reasoning)


def _render_human_tool_step(step: Mapping[str, Any]) -> None:
    _render_section("input_refs", step.get("input_refs"))
    for index, call in enumerate(_list(step.get("tool_calls")), start=1):
        typed = _mapping(call)
        typer.echo(f"tool_call {index}")
        _render_kv("name", typed.get("name"))
        _render_kv("family", typed.get("family"))
        _render_kv("call_id", typed.get("call_id"))
        _render_section("input", typed.get("input"))
        _render_section("result", typed.get("result"))
        _render_section("error", typed.get("error"))
    _render_section("other_output", step.get("other_output"))


def _render_kv(label: str, value: object) -> None:
    if value is None or value == "":
        return
    typer.echo(f"  {label}: {value}")


def _render_text_section(label: str, text: str) -> None:
    typer.echo(label)
    for line in text.splitlines() or [""]:
        typer.echo(f"  {line}")


def _render_section(label: str, value: object) -> None:
    if value is None or value == [] or value == {}:
        return
    if isinstance(value, str):
        _render_text_section(label, value)
        return
    typer.echo(label)
    for line in _full_value(value).splitlines():
        typer.echo(f"  {line}")


def _inspect_thread_run_map(thread: Mapping[str, Any], *, fallback: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    runs = [_mapping(item) for item in _list(thread.get("runs"))]
    if not runs:
        runs = [fallback]
    result: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        run_id = _text(_mapping(run.get("info")).get("id"))
        if run_id is not None:
            result[run_id] = run
    return result


def _top_level_runs(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    roots = [run for run in runs if _text(_mapping(run.get("info")).get("parent_run_id")) is None]
    return roots or list(runs)


def _run_steps(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output = _mapping(run.get("output"))
    return [_mapping(item) for item in _list(output.get("steps"))]


def _failure_summary(run: Mapping[str, Any]) -> str:
    output = _mapping(run.get("output"))
    failure = _mapping(output.get("failure"))
    reason = _text(failure.get("reason")) or _text(output.get("error"))
    if not reason:
        return ""
    step_index = failure.get("step_index")
    step_kind = _text(failure.get("step_kind"))
    if step_index is not None and step_kind:
        return f"{reason} (step {step_index} {step_kind})"
    if step_index is not None:
        return f"{reason} (step {step_index})"
    return reason


def _step_summary(record: Mapping[str, Any], message: Mapping[str, Any]) -> str:
    payload = _mapping(record.get("payload"))
    kind = _text(record.get("kind"))
    if kind == "model":
        model = _text(payload.get("model_ref")) or _text(payload.get("model"))
        text = _message_summary(message) or _parts_summary(record.get("output"))
        requests = "; ".join(line.removeprefix("requested ") for line in _tool_request_lines(record))
        request_summary = f"requested {requests}" if requests else ""
        return " ".join(item for item in (model, text, request_summary) if item)
    if kind == "tool":
        return _tool_result_summary(record)
    if kind == "run":
        return child_call_summary(payload)
    if kind in {"step", "parallel", "bind"}:
        return flow_op_summary(payload)
    text = _parts_summary(record.get("output"))
    return text or _text(record.get("error")) or "-"


def _tool_request_lines(record: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "tool_call":
            continue
        name = _text(typed.get("tool_name")) or _text(typed.get("tool_family")) or "tool"
        tool_input = _tool_input_summary(typed.get("input"))
        suffix = f": {tool_input}" if tool_input else ""
        lines.append(f"requested {name}{suffix}")
    return lines


def _tool_result_summary(record: Mapping[str, Any]) -> str:
    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "tool_result":
            continue
        name = _text(typed.get("tool_name")) or _text(typed.get("tool_family")) or "tool"
        tool_input = _tool_input_summary(typed.get("input"))
        suffix = f": {tool_input}" if tool_input else ""
        return f"{name}{suffix}"
    return _parts_summary(record.get("output")) or _text(record.get("error")) or "-"


def _tool_input_summary(tool_input: object) -> str:
    if not isinstance(tool_input, Mapping) or not tool_input:
        return ""
    return ", ".join(f"{key}={_plain_value(value)}" for key, value in tool_input.items())


def _message_summary(message: Mapping[str, Any]) -> str:
    return _parts_summary(message.get("parts"))


def _parts_summary(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        typed = _mapping(part)
        if typed.get("type") == "text":
            texts.append(str(typed.get("text") or ""))
    return _truncate("".join(texts).strip(), width=72)


def _plain_value(value: object) -> str:
    if isinstance(value, str):
        return _truncate(value, width=160)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return _truncate(json.dumps(value, ensure_ascii=False, separators=(",", ":")), width=160)


def _display_run_status(status: object) -> str:
    text = str(status or "")
    return "succeeded" if text == "finished" else text


def _status_mark(status: str) -> str:
    if status == "succeeded":
        return "✓"
    if status == "failed":
        return "✗"
    if status == "canceled":
        return "-"
    if status == "running":
        return "…"
    return "·"


def _elapsed(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return ""
    start = _parse_utc_timestamp(started_at)
    finish = _parse_utc_timestamp(finished_at)
    if start is None or finish is None:
        return ""
    seconds = max((finish - start).total_seconds(), 0)
    if seconds < 1:
        return f"{max(round(seconds * 1000), 1)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _parse_utc_timestamp(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _truncate(value: object, *, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."


def _full_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


_TOML_NONE = object()


def _toml_safe(value: object) -> object:
    if value is None:
        return _TOML_NONE
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            converted = _toml_safe(item)
            if converted is not _TOML_NONE:
                result[str(key)] = converted
        return result
    if isinstance(value, list):
        return [converted for item in value if (converted := _toml_safe(item)) is not _TOML_NONE]
    if isinstance(value, tuple):
        return [converted for item in value if (converted := _toml_safe(item)) is not _TOML_NONE]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _toml_document(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _toml_safe(value))


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
