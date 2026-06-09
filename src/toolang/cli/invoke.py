"""Roaming invoke CLI helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from typing import Literal, cast

import click
from rich.console import Console
from rich.live import Live
from rich.text import Text
import typer
from typer import rich_utils
from typer.core import HAS_RICH
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.main import get_command

from .. import agents
from .. import file_requests
from .. import up as agent_up
from ..base.error import ToolangError
from ..config.env import load_runtime_environ
from ..config.log_spec import PY_LOG_ENV_VAR
from ..execution.events import RunEnd, RunStart, StepEnd, StepStart, TraceEvent
from ..execution.labels import executable_label
from ..execution.runner import RunOutcome
from ..models.errors import NO_AVAILABLE_MODELS_MESSAGE, NO_MATCHED_MODELS_MESSAGE
from ..program import Flow, ParamDecl, Thunk
from ..state.prepared import PreparedState
from ..caps import split_cap_selectors
from ..state.program import LiveProgram, load_live_program
from ..tools.registry import split_tool_selectors
from .progress import CliProgress, as_progress_sink, make_cli_progress

MarkupMode = Literal["markdown", "rich"]
HELP_FLAGS = {"--help", "-h"}


@dataclass(frozen=True, slots=True)
class RoamingInvokeRequest:
    thunk_name: str | None
    executable_kind: str
    verbosity: int
    input_text: str | None
    models: tuple[str, ...]
    tools: tuple[str, ...]
    caps: tuple[str, ...]
    invoke_params: dict[str, object]
    invoke_parts: list[dict[str, str]]
    quiet: bool = False


class _HelpOnlyArgument(TyperArgument):
    def make_metavar(self, ctx: click.Context | None = None) -> str:
        del ctx
        return self.metavar or "TEXT"

    def add_to_parser(self, parser: object, ctx: click.Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: click.core.cabc.Mapping[str, object],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args


class _MissingInvokeInput(click.ClickException):
    pass


class _RoamingInvokeHelpGroup(TyperGroup):
    usage_tail = "TARGET [OPTIONS] [PARAMS] [INPUT]..."

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not HAS_RICH or self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)
        _rich_format_roaming_help(
            obj=self,
            ctx=ctx,
            markup_mode=self.rich_markup_mode,
            show_commands=True,
        )

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, self.usage_tail)

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            *_help_arguments(
                show_thunk=False,
                show_params=True,
                show_parts=True,
                show_input_forms=True,
            ),
            *super().get_params(ctx),
        ]

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            limit = formatter.width - 6 - len(subcommand)
            rows.append((subcommand, cmd.get_short_help_str(limit)))
        if rows:
            with formatter.section("Targets"):
                formatter.write_dl(rows)


class _RoamingThunkHelpCommand(TyperCommand):
    usage_tail = "[OPTIONS]"
    show_params = False
    show_parts = False
    help_executable: Thunk | Flow | None = None

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not HAS_RICH or self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)
        _rich_format_roaming_help(
            obj=self,
            ctx=ctx,
            markup_mode=self.rich_markup_mode,
            show_commands=False,
        )

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        parent_path = ctx.parent.command_path if ctx.parent is not None else ctx.command_path
        command_path = f"{parent_path} TARGET".rstrip()
        formatter.write_usage(command_path, self.usage_tail)

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            *_help_arguments(
                show_thunk=False,
                show_params=self.show_params,
                show_parts=self.show_parts,
                show_input_forms=True,
                executable=self.help_executable,
            ),
            *super().get_params(ctx),
        ]


def roaming_source_path(token: str) -> Path | None:
    text = token.strip()
    if not text or text.startswith("-"):
        return None
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix != ".too":
        return None
    return resolved


def handle_roaming_invoke(global_args: list[str], body: list[str], *, prog_name: str) -> int:
    if _unsupported_roaming_global_args(global_args):
        typer.echo(
            "toolang error: too <path>.too does not support global CLI options",
            err=True,
        )
        return 1
    source_path = roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    source_label = body[0]
    remaining = body[1:]
    quiet, verbosity, leading_models, leading_tools, leading_caps, normalized_remaining = _consume_roaming_control_options(remaining)
    prepare_progress = _prepare_progress(quiet=quiet, argv=remaining)
    script_progress: _ScriptProgressSink | None = None
    request: RoamingInvokeRequest | None = None
    runtime_environ: dict[str, str] | None = None
    toolang_root: Path | None = None
    agent_name: str | None = None
    try:
        toolang_root, agent_name, prepared, program = _load_roaming_live_program(
            source_path,
            progress=as_progress_sink(prepare_progress),
        )
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        if normalized_remaining and normalized_remaining[0] in HELP_FLAGS:
            _show_roaming_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        if not remaining:
            _show_roaming_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        if not normalized_remaining:
            _show_roaming_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        executable_kind, executable, remainder = _select_roaming_executable(program, normalized_remaining)
        if any(token in HELP_FLAGS for token in remainder):
            _show_roaming_help(source_label, program, target_name=_executable_name(executable), prog_name=prog_name)
            return 0
        try:
            request = _parse_roaming_invoke_request(
                executable,
                remainder,
                executable_kind=executable_kind,
                leading_verbosity=verbosity,
                leading_models=leading_models,
                leading_tools=leading_tools,
                leading_caps=leading_caps,
            )
        except _MissingInvokeInput:
            _show_roaming_help(source_label, program, target_name=_executable_name(executable), prog_name=prog_name)
            return 0
        runtime_environ = load_runtime_environ(toolang_root, agent_name, base_environ=os.environ)
        script_progress = _script_progress_sink(
            thunk_name=request.thunk_name,
            quiet=quiet or request.quiet,
            verbosity=request.verbosity,
        )
        metadata: dict[str, object] = {
            "invoke_params": request.invoke_params,
            "invoke_parts": request.invoke_parts,
        }
        if request.executable_kind == "flow":
            metadata["executable_kind"] = "flow"
        if request.tools and request.caps:
            outcome = agent_up.invoke(
                toolang_root=toolang_root,
                agent_name=agent_name,
                thunk_name=request.thunk_name,
                input_text=request.input_text,
                models=request.models,
                tools=request.tools,
                caps=request.caps,
                metadata=metadata,
                environ=runtime_environ,
                response=script_progress,
                prepared_state=prepared,
            )
        elif request.tools:
            outcome = agent_up.invoke(
                toolang_root=toolang_root,
                agent_name=agent_name,
                thunk_name=request.thunk_name,
                input_text=request.input_text,
                models=request.models,
                tools=request.tools,
                metadata=metadata,
                environ=runtime_environ,
                response=script_progress,
                prepared_state=prepared,
            )
        elif request.caps:
            outcome = agent_up.invoke(
                toolang_root=toolang_root,
                agent_name=agent_name,
                thunk_name=request.thunk_name,
                input_text=request.input_text,
                models=request.models,
                caps=request.caps,
                metadata=metadata,
                environ=runtime_environ,
                response=script_progress,
                prepared_state=prepared,
            )
        else:
            outcome = agent_up.invoke(
                toolang_root=toolang_root,
                agent_name=agent_name,
                thunk_name=request.thunk_name,
                input_text=request.input_text,
                models=request.models,
                metadata=metadata,
                environ=runtime_environ,
                response=script_progress,
                prepared_state=prepared,
            )
    except KeyboardInterrupt:
        if script_progress is not None:
            script_progress.interrupt()
        if prepare_progress is not None:
            prepare_progress.interrupt()
        _emit_interrupt_message(
            script_progress=script_progress,
            toolang_root=toolang_root,
            agent_name=agent_name,
            thunk_name=request.thunk_name if request is not None else None,
            environ=runtime_environ,
        )
        return 130
    except (FileExistsError, FileNotFoundError, ValueError, ToolangError, click.ClickException) as exc:
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1
    return _emit_invoke_outcome(outcome)


def _unsupported_roaming_global_args(global_args: list[str]) -> bool:
    return bool(global_args)


def _consume_roaming_control_options(
    argv: list[str],
) -> tuple[bool, int, tuple[str, ...], tuple[str, ...], tuple[str, ...], list[str]]:
    quiet = False
    verbosity = 0
    models: list[str] = []
    tools: list[str] = []
    caps: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            remaining.extend(argv[index:])
            break
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
            continue
        if token == "--verbose":
            verbosity += 1
            index += 1
            continue
        if token.startswith("-v") and set(token) <= {"-", "v"}:
            verbosity += len(token) - 1
            index += 1
            continue
        if token.startswith("--models="):
            model = token.partition("=")[2].strip()
            if model:
                models.append(model)
                index += 1
                continue
        if token == "--models" and index + 1 < len(argv):
            model = argv[index + 1].strip()
            if model:
                models.append(model)
                index += 2
                continue
        if token.startswith("--tools="):
            tool = token.partition("=")[2].strip()
            if tool:
                tools.append(tool)
                index += 1
                continue
        if token == "--tools" and index + 1 < len(argv):
            tool = argv[index + 1].strip()
            if tool:
                tools.append(tool)
                index += 2
                continue
        if token.startswith("--caps="):
            cap = token.partition("=")[2].strip()
            if cap:
                caps.append(cap)
                index += 1
                continue
        if token == "--caps" and index + 1 < len(argv):
            cap = argv[index + 1].strip()
            if cap:
                caps.append(cap)
                index += 2
                continue
        remaining.append(token)
        index += 1
    return quiet, verbosity, tuple(models), split_tool_selectors(tuple(tools)), split_cap_selectors(tuple(caps)), remaining


def _prepare_progress(*, quiet: bool, argv: list[str]) -> "CliProgress | None":
    if quiet or not sys.stderr.isatty() or any(token in HELP_FLAGS for token in argv):
        return None
    return make_cli_progress()


def _load_roaming_live_program(
    source_path: Path,
    *,
    progress=None,
) -> tuple[Path, str, PreparedState, LiveProgram]:
    toolang_root, agent_name = agents.materialize_roaming_program(source_path)
    prepared = agent_up.prepare_agent(toolang_root=toolang_root, agent_name=agent_name, progress=progress)
    return toolang_root, agent_name, prepared, load_live_program(prepared.program)


def _select_roaming_executable(
    program: LiveProgram,
    argv: list[str],
) -> tuple[str, Thunk | Flow, list[str]]:
    for thunk in program.thunks:
        if _thunk_name(thunk) == argv[0]:
            return "thunk", thunk, argv[1:]
    for flow in program.flows:
        if flow.flow_name() == argv[0]:
            return "flow", flow, argv[1:]
    raise click.ClickException(f"unknown target: {argv[0]}")


def _parse_roaming_invoke_request(
    executable: Thunk | Flow,
    argv: list[str],
    *,
    executable_kind: str,
    leading_verbosity: int = 0,
    leading_models: tuple[str, ...] = (),
    leading_tools: tuple[str, ...] = (),
    leading_caps: tuple[str, ...] = (),
) -> RoamingInvokeRequest:
    executable_params = tuple(executable.params)
    param_index = {param.name: param for param in executable_params}
    invoke_params: dict[str, object] = {}
    parts: list[str] = []
    models = list(leading_models)
    tools = list(leading_tools)
    caps = list(leading_caps)
    quiet = False
    verbosity = leading_verbosity
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            parts.extend(argv[index + 1 :])
            break
        if token.startswith("--models="):
            model = token.partition("=")[2].strip()
            if not model:
                raise click.ClickException("--models requires a value")
            models.append(model)
            index += 1
            continue
        if token == "--models":
            if index + 1 >= len(argv):
                raise click.ClickException("--models requires a value")
            model = argv[index + 1].strip()
            if not model:
                raise click.ClickException("--models requires a value")
            models.append(model)
            index += 2
            continue
        if token.startswith("--tools="):
            tool = token.partition("=")[2].strip()
            if not tool:
                raise click.ClickException("--tools requires a value")
            tools.extend(split_tool_selectors((tool,)))
            index += 1
            continue
        if token == "--tools":
            if index + 1 >= len(argv):
                raise click.ClickException("--tools requires a value")
            tool = argv[index + 1].strip()
            if not tool:
                raise click.ClickException("--tools requires a value")
            tools.extend(split_tool_selectors((tool,)))
            index += 2
            continue
        if token.startswith("--caps="):
            cap = token.partition("=")[2].strip()
            if not cap:
                raise click.ClickException("--caps requires a value")
            caps.extend(split_cap_selectors((cap,)))
            index += 1
            continue
        if token == "--caps":
            if index + 1 >= len(argv):
                raise click.ClickException("--caps requires a value")
            cap = argv[index + 1].strip()
            if not cap:
                raise click.ClickException("--caps requires a value")
            caps.extend(split_cap_selectors((cap,)))
            index += 2
            continue
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
            continue
        if token == "--verbose":
            verbosity += 1
            index += 1
            continue
        if token.startswith("-v") and set(token) <= {"-", "v"}:
            verbosity += len(token) - 1
            index += 1
            continue
        if token.startswith("--"):
            raise click.ClickException(f"unknown Toolang invoke option: {token}")
        param_name, has_assignment, raw_value = token.partition("=")
        param = param_index.get(param_name) if has_assignment else None
        if param is not None:
            if param_name in invoke_params:
                raise click.ClickException(f"duplicate invoke parameter: {param_name}")
            invoke_params[param_name] = _coerce_invoke_value(raw_value, param=param)
            index += 1
            continue
        parts.append(token)
        index += 1
    missing = [param.name for param in executable_params if not param.optional and param.name not in invoke_params]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise click.ClickException(f"missing required invoke parameters: {joined}")
    target_name = _executable_name(executable)
    accepts_message = executable.input is not None
    if accepts_message and not parts:
        raise _MissingInvokeInput(f"target {target_name!r} requires at least one INPUT")
    if parts and not accepts_message:
        raise click.ClickException(f"target {target_name!r} does not accept INPUT")
    input_text, invoke_parts = _render_roaming_input(parts) if parts else (None, [])
    return RoamingInvokeRequest(
        thunk_name=target_name,
        executable_kind=executable_kind,
        verbosity=verbosity,
        input_text=input_text,
        models=tuple(models),
        tools=tuple(dict.fromkeys(tools)),
        caps=tuple(dict.fromkeys(caps)),
        invoke_params=invoke_params,
        invoke_parts=invoke_parts,
        quiet=quiet,
    )


def _parse_boolean_value(raw: str, *, option_name: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(f"{option_name} expects a boolean value")


def _coerce_invoke_value(raw: str, *, param: ParamDecl) -> object:
    type_name = param.type_name
    if type_name == "Number":
        try:
            if any(marker in raw for marker in (".", "e", "E")):
                return float(raw)
            return int(raw)
        except ValueError as exc:
            raise click.ClickException(f"{param.name} expects a number") from exc
    if type_name == "Boolean":
        return _parse_boolean_value(raw, option_name=param.name)
    if type_name == "Path":
        return str(Path(raw).expanduser().resolve())
    return raw


def _render_roaming_input(parts: list[str]) -> tuple[str, list[dict[str, str]]]:
    rendered: list[str] = []
    invoke_parts: list[dict[str, str]] = []
    for part in parts:
        if part.startswith("@@"):
            text = part[1:]
            rendered.append(text)
            invoke_parts.append({"type": "text", "text": text})
            continue
        if part.startswith("@"):
            candidate = Path(part[1:]).expanduser().resolve()
            if not candidate.exists():
                raise click.ClickException(f"invoke input not found: {candidate}")
            text, path_parts = file_requests.render_file_input(candidate)
            rendered.append(text)
            invoke_parts.extend(path_parts)
            continue
        rendered.append(part)
        invoke_parts.append({"type": "text", "text": part})
    return "\n\n".join(rendered), invoke_parts


def _emit_invoke_outcome(outcome: RunOutcome) -> int:
    if outcome.status == "failed":
        error = outcome.error or "invoke failed"
        typer.echo(f"toolang error: {error}", err=True)
        if _is_model_selection_error(error):
            return 1
        typer.echo(f"Run: {outcome.run_id}", err=True)
        if outcome.log_path:
            typer.echo(f"Log: {outcome.log_path}", err=True)
        return 1
    if outcome.output_text:
        typer.echo(outcome.output_text)
    return 0


def _is_model_selection_error(error: str) -> bool:
    return error in {NO_AVAILABLE_MODELS_MESSAGE, NO_MATCHED_MODELS_MESSAGE}


def _script_progress_sink(*, thunk_name: str | None, quiet: bool, verbosity: int) -> "_ScriptProgressSink":
    return _ScriptProgressSink(
        thunk_name=thunk_name or "main",
        render=not quiet and sys.stderr.isatty(),
        verbosity=verbosity,
    )


def _emit_interrupt_message(
    *,
    script_progress: "_ScriptProgressSink | None",
    toolang_root: Path | None,
    agent_name: str | None,
    thunk_name: str | None,
    environ: dict[str, str] | None,
) -> None:
    typer.echo("toolang interrupted", err=True)
    run_id = script_progress.run_id if script_progress is not None else None
    if run_id:
        typer.echo(f"Run: {run_id}", err=True)
    if not run_id or toolang_root is None or agent_name is None:
        return
    if environ is None or not environ.get(PY_LOG_ENV_VAR, "").strip():
        return
    typer.echo(
        f"Log: {agents.agent_script_run_log_path(toolang_root, agent_name, thunk_name=thunk_name, run_id=run_id)}",
        err=True,
    )


@dataclass(slots=True)
class _StageProgress:
    key: str
    index: int | None = None
    total: int | None = None
    kind: str = "stage"
    title: str = "stage"
    status: str = "running"
    input_shape: str | None = None
    output_shape: str | None = None
    item_total: int | None = None
    parallelism: int | None = None
    calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _CallProgress:
    key: str
    stage_key: str
    label: str
    run_id: str
    status: str = "running"
    item_index: int | None = None
    item_count: int | None = None
    lane_index: int | None = None
    parallelism: int | None = None
    steps: dict[int, str] = field(default_factory=dict)


class _ScriptProgressSink:
    """Render script progress to stderr without touching stdout."""

    wants_stream = False

    def __init__(self, *, thunk_name: str, render: bool, verbosity: int = 0) -> None:
        self._thunk_name = thunk_name
        self._render_enabled = render
        self._verbosity = max(0, verbosity)
        self._run_id: str | None = None
        self._title = ""
        self._finished = False
        self._run_labels: dict[str, str] = {}
        self._stage_order: list[str] = []
        self._stages: dict[str, _StageProgress] = {}
        self._calls: dict[str, _CallProgress] = {}
        self._run_call_keys: dict[str, str] = {}
        self._console = Console(file=sys.stderr, force_terminal=True, highlight=False)
        self._live: Live | None = None

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunStart):
            label = executable_label(event.executable_kind, event.executable_name, metadata=event.metadata)
            self._run_labels[event.run_id] = label
            if event.parent_run_id is None:
                self._run_id = event.run_id
                self._title = f"Running {label}: {event.run_id}"
                self._render()
                return
            if event.call_kind == "stage":
                stage = self._ensure_stage(event.metadata)
                call = self._ensure_call(
                    run_id=event.run_id,
                    stage=stage,
                    target_label=label,
                    payload=event.metadata,
                )
                call.status = "running"
                self._render()
            return
        if isinstance(event, StepStart):
            if event.kind not in {"flow_op", "child_call"}:
                self._update_call_step(event.run_id, event.step_index, f"{event.kind} running")
            return
        if isinstance(event, StepEnd):
            self._update_step(event)
            return
        if isinstance(event, RunEnd):
            if event.run_id == self._run_id:
                self._finished = True
                self._render()
                self.finish()
                return
            call = self._call_for_run(event.run_id)
            if call is not None:
                call.status = self._status_word(event.status)
                self._render()

    def finish(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def interrupt(self) -> None:
        self.finish()

    def _update_step(self, event: StepEnd) -> None:
        payload = event.payload.to_data()
        if event.kind == "flow_op":
            stage = self._ensure_stage(payload)
            op = str(payload.get("op", ""))
            metadata = self._metadata(payload)
            if input_preview := metadata.get("input_preview"):
                stage.input_shape = self._shape_label(input_preview)
            if preview := payload.get("output_preview"):
                if op.startswith("prepare_"):
                    stage.item_total = self._preview_count(preview) or stage.item_total
                if op == "set_current":
                    stage.output_shape = self._shape_label(preview)
                    stage.status = "done"
                elif stage.status != "done":
                    stage.status = "running"
            self._render()
            return
        if event.kind == "child_call":
            stage = self._ensure_stage(payload)
            for run_id in self._child_run_ids(payload, event):
                call = self._ensure_call(
                    run_id=run_id,
                    stage=stage,
                    target_label=executable_label(
                        str(payload.get("target_kind") or "run"),
                        str(payload.get("target")) if payload.get("target") is not None else None,
                        metadata=self._metadata(payload),
                    ),
                    payload=payload,
                )
                call.status = self._status_word(event.status)
            self._render()
            return
        self._update_call_step(event.run_id, event.step_index, f"{event.kind} {self._status_word(event.status)}")

    def _ensure_stage(self, payload: Mapping[str, object]) -> _StageProgress:
        ctx = self._context(payload)
        key = f"stage:{ctx.get('stage_index')}" if ctx.get("stage_index") is not None else "stage"
        stage = self._stages.get(key)
        if stage is None:
            stage = _StageProgress(key=key)
            self._stages[key] = stage
            self._stage_order.append(key)
        stage.index = self._int_payload(ctx.get("stage_index")) if ctx.get("stage_index") is not None else stage.index
        stage.total = self._int_payload(ctx.get("stage_total")) or stage.total
        stage.kind = str(ctx.get("stage_kind") or stage.kind)
        title = str(ctx.get("stage_title") or ctx.get("stage_doc") or ctx.get("stage_target") or ctx.get("stage_label") or stage.title).strip()
        if title:
            stage.title = title
        stage.parallelism = self._int_payload(ctx.get("parallelism")) or stage.parallelism
        if input_preview := ctx.get("input_preview"):
            stage.input_shape = self._shape_label(input_preview)
        if item_count := self._int_payload(ctx.get("item_count")):
            stage.item_total = item_count
        return stage

    def _ensure_call(
        self,
        *,
        run_id: str,
        stage: _StageProgress,
        target_label: str,
        payload: Mapping[str, object],
    ) -> _CallProgress:
        call_key = self._run_call_keys.get(run_id, run_id)
        ctx = self._context(payload)
        call = self._calls.get(call_key)
        if call is None:
            call = _CallProgress(
                key=call_key,
                stage_key=stage.key,
                label=self._call_label(target_label, ctx),
                run_id=run_id,
            )
            self._calls[call_key] = call
            self._run_call_keys[run_id] = call_key
            if call_key not in stage.calls:
                stage.calls.append(call_key)
        call.item_index = self._int_payload(ctx.get("item_index")) if ctx.get("item_index") is not None else call.item_index
        call.item_count = self._int_payload(ctx.get("item_count")) or call.item_count
        call.lane_index = self._int_payload(ctx.get("lane_index")) if ctx.get("lane_index") is not None else call.lane_index
        call.parallelism = self._int_payload(ctx.get("parallelism")) or call.parallelism
        if call.parallelism is not None:
            stage.parallelism = call.parallelism
        if call.item_count is not None:
            stage.item_total = call.item_count
        return call

    def _update_call_step(self, run_id: str, step_index: int, text: str) -> None:
        call = self._call_for_run(run_id)
        if call is None:
            return
        call.steps[step_index] = text
        self._render()

    def _call_for_run(self, run_id: str) -> _CallProgress | None:
        key = self._run_call_keys.get(run_id)
        return self._calls.get(key) if key is not None else None

    def _context(self, payload: Mapping[str, object]) -> dict[str, object]:
        metadata = self._metadata(payload)
        child = payload.get("child")
        if isinstance(child, Mapping):
            child = cast(Mapping[str, object], child)
        else:
            child = metadata.get("child")
            child = cast(Mapping[str, object], child) if isinstance(child, Mapping) else {}
        ctx: dict[str, object] = dict(child)
        ctx.update(metadata)
        ctx.update({key: value for key, value in payload.items() if key != "metadata"})
        if "item_index" not in ctx:
            item_indexes = payload.get("item_indexes")
            if isinstance(item_indexes, (list, tuple)) and item_indexes:
                ctx["item_index"] = item_indexes[0]
        return ctx

    def _metadata(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        metadata = payload.get("metadata")
        return cast(Mapping[str, object], metadata) if isinstance(metadata, Mapping) else {}

    def _child_run_ids(self, payload: Mapping[str, object], event: StepEnd) -> tuple[str, ...]:
        child_ids = payload.get("child_run_ids")
        if isinstance(child_ids, (list, tuple)):
            ids = tuple(str(item) for item in child_ids if item is not None)
            if ids:
                return ids
        return (f"{event.run_id}:{event.step_index}",)

    def _call_label(self, target_label: str, ctx: Mapping[str, object]) -> str:
        item_index = self._int_payload(ctx.get("item_index"))
        item_count = self._int_payload(ctx.get("item_count"))
        target = target_label.replace(":", " ", 1)
        if item_index is not None:
            item = f"item {item_index + 1}/{item_count}" if item_count else f"item {item_index + 1}"
            return f"{item} · {target}"
        return target

    def _status_word(self, status: str) -> str:
        return "done" if status == "finished" else status

    def _stage_prefix(self, stage: _StageProgress) -> str:
        if stage.status == "done":
            return "✓"
        if stage.status == "failed":
            return "✗"
        return "…"

    def _stage_label(self, stage: _StageProgress) -> str:
        index = "?"
        if stage.index is not None:
            index = str(stage.index + 1)
        if stage.total is not None:
            index = f"{index}/{stage.total}"
        title = self._truncate(stage.title, 56 if self._verbosity == 0 else 84)
        return f"{index} {stage.kind:<6} {title}"

    def _stage_tail(self, stage: _StageProgress) -> str:
        lanes = f"{stage.parallelism} lanes" if stage.parallelism and stage.parallelism > 1 else ""
        if stage.status == "done" and (stage.input_shape or stage.output_shape):
            shape = f"{stage.input_shape or '?'} -> {stage.output_shape or '?'}"
            return " · ".join(item for item in (shape, lanes) if item)
        done = self._stage_done_count(stage)
        failed = self._stage_failed_count(stage)
        if stage.item_total is not None:
            progress = f"{done}/{stage.item_total} items"
            if failed:
                progress = f"{progress} · {failed} failed"
            return " · ".join(item for item in (progress, lanes) if item)
        if failed:
            return f"{failed} failed"
        return "running"

    def _stage_done_count(self, stage: _StageProgress) -> int:
        return sum(1 for call in self._stage_calls(stage) if call.status in {"done", "failed", "canceled"})

    def _stage_failed_count(self, stage: _StageProgress) -> int:
        return sum(1 for call in self._stage_calls(stage) if call.status == "failed")

    def _stage_calls(self, stage: _StageProgress) -> list[_CallProgress]:
        calls = [self._calls[key] for key in stage.calls if key in self._calls]
        return sorted(calls, key=lambda call: (call.lane_index if call.lane_index is not None else 999_999, call.item_index if call.item_index is not None else 999_999, call.run_id))

    def _lane_calls(self, stage: _StageProgress) -> dict[int, list[_CallProgress]]:
        lanes: dict[int, list[_CallProgress]] = {}
        for call in self._stage_calls(stage):
            lane = call.lane_index if call.lane_index is not None else 0
            lanes.setdefault(lane, []).append(call)
        return lanes

    def _render_lines(self) -> list[str]:
        lines = [self._title or f"Running {self._thunk_name}"]
        for stage_key in self._stage_order:
            stage = self._stages[stage_key]
            lines.append(f"{self._stage_prefix(stage)} {self._stage_label(stage):<72} {self._stage_tail(stage)}")
            if self._verbosity <= 0:
                continue
            if stage.parallelism and stage.parallelism > 1:
                lanes = self._lane_calls(stage)
                for lane_index in range(stage.parallelism):
                    calls = lanes.get(lane_index, [])
                    lane_done = sum(1 for call in calls if call.status in {"done", "failed", "canceled"})
                    lines.append(f"  lane {lane_index + 1}/{stage.parallelism:<3} {lane_done}/{len(calls)} calls")
                    if self._verbosity <= 1:
                        continue
                    for call in calls:
                        lines.extend(self._render_call(call, indent="    ", include_steps=self._verbosity >= 3))
                continue
            for call in self._stage_calls(stage):
                lines.extend(self._render_call(call, indent="  ", include_steps=self._verbosity >= 2))
        if self._finished:
            failed = sum(1 for call in self._calls.values() if call.status == "failed")
            lines.append(f"Done · {len(self._stage_order)} stages · {len(self._calls)} calls · {failed} failed")
        return lines

    def _render_call(self, call: _CallProgress, *, indent: str, include_steps: bool) -> list[str]:
        prefix = "✓" if call.status == "done" else "✗" if call.status == "failed" else "…"
        lines = [f"{indent}{prefix} {call.label} · {call.run_id} {call.status}"]
        if include_steps:
            for _index, text in sorted(call.steps.items()):
                lines.append(f"{indent}  - {text}")
        return lines

    def _shape_label(self, preview: object) -> str:
        if isinstance(preview, Mapping):
            preview = cast(Mapping[str, object], preview)
            count = self._int_payload(preview.get("count"))
            if count is not None:
                return "1 item" if count == 1 else f"{count} items"
            if preview.get("type") == "list":
                count = self._int_payload(preview.get("count"))
                if count is not None:
                    return "1 item" if count == 1 else f"{count} items"
            if preview.get("type") == "object":
                return "object"
        if preview is None:
            return "unset"
        return "1 item"

    def _preview_count(self, preview: object) -> int | None:
        if isinstance(preview, Mapping):
            preview = cast(Mapping[str, object], preview)
            return self._int_payload(preview.get("count"))
        return None

    def _int_payload(self, value: object) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _truncate(self, text: str, width: int) -> str:
        text = " ".join(text.split())
        if len(text) <= width:
            return text
        return f"{text[: max(width - 1, 0)].rstrip()}…"

    def _render(self) -> None:
        if not self._render_enabled:
            return
        body = "\n".join(self._render_lines())
        text = Text(body, style="dim")
        if self._live is None:
            self._live = Live(
                text,
                console=self._console,
                refresh_per_second=10,
                transient=False,
            )
            self._live.start(refresh=True)
            return
        self._live.update(text, refresh=True)


def _show_roaming_help(
    source_label: str,
    program: LiveProgram,
    *,
    target_name: str | None,
    prog_name: str,
) -> None:
    app = _build_roaming_help_app(source_label, program)
    command = get_command(app)
    if not isinstance(command, _RoamingInvokeHelpGroup):
        raise RuntimeError("expected roaming help group")
    args = ["--help"] if target_name is None else [target_name, "--help"]
    try:
        command.main(
            args=args,
            prog_name=f"{prog_name} SCRIPT",
            standalone_mode=False,
        )
    except click.exceptions.Exit:
        return


def _build_roaming_help_app(source_label: str, program: LiveProgram) -> typer.Typer:
    app = typer.Typer(
        cls=_RoamingInvokeHelpGroup,
        add_completion=False,
        no_args_is_help=True,
        invoke_without_command=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
        help=f"Invoke a thunk or flow from a Toolang script.\n\nScript: {source_label}",
    )

    @app.callback()
    def _callback(
        model: list[str] | None = typer.Option(
            None,
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
        tools: list[str] | None = typer.Option(
            None,
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
        caps: list[str] | None = typer.Option(
            None,
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, tools, caps, quiet
        return None

    for thunk in program.thunks:
        app.command(
            _thunk_name(thunk),
            help=_roaming_executable_help_text(source_label, thunk),
            short_help=_executable_summary(thunk),
            cls=_make_roaming_executable_help_command_class(thunk),
            rich_help_panel="Thunks",
        )(_make_roaming_help_command())
    for flow in program.flows:
        app.command(
            flow.flow_name(),
            help=_roaming_executable_help_text(source_label, flow),
            short_help=_executable_summary(flow),
            cls=_make_roaming_executable_help_command_class(flow),
            rich_help_panel="Flows",
        )(_make_roaming_help_command())
    return app


def _roaming_executable_help_text(source_label: str, executable: Thunk | Flow) -> str:
    summary = _executable_summary(executable)
    intro = "Invoke a thunk or flow from a Toolang script." if summary == "-" else summary
    label = "Thunk" if isinstance(executable, Thunk) else "Flow"
    return f"{intro}\n\nScript: {source_label}\n{label}:  {_executable_name(executable)}"


def _make_roaming_executable_help_command_class(executable: Thunk | Flow) -> type[_RoamingThunkHelpCommand]:
    class _ConfiguredRoamingThunkHelpCommand(_RoamingThunkHelpCommand):
        usage_tail = _roaming_executable_usage_tail(executable)
        show_params = bool(executable.params)
        show_parts = executable.input is not None
        help_executable = executable

    return _ConfiguredRoamingThunkHelpCommand


def _make_roaming_help_command() -> Callable[..., None]:
    def command(
        model: list[str] | None = typer.Option(
            None,
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
        tools: list[str] | None = typer.Option(
            None,
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
        caps: list[str] | None = typer.Option(
            None,
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, tools, caps, quiet
        return None

    return command


def _roaming_executable_usage_tail(executable: Thunk | Flow) -> str:
    pieces = ["[OPTIONS]"]
    if executable.params:
        pieces.append("[PARAMS]")
    if executable.input is not None:
        pieces.append("[INPUT]...")
    return " ".join(pieces)


def _param_assignment_label(param: ParamDecl) -> str:
    type_name = param.type_name or "TEXT"
    if param.type_name == "Number":
        type_name = "NUMBER"
    elif param.type_name == "Boolean":
        type_name = "BOOLEAN"
    elif param.type_name == "Path":
        type_name = "PATH"
    elif type_name.islower():
        type_name = type_name.upper()
    return f"{param.name}={type_name}"


def _executable_summary(executable: Thunk | Flow) -> str:
    if isinstance(executable, Thunk):
        for line in executable.messages_text().splitlines():
            text = line.strip()
            if text:
                return text
    if isinstance(executable, Flow) and executable.stages:
        return f"{len(executable.stages)} flow stages"
    return "-"


def _help_arguments(
    *,
    show_thunk: bool,
    show_params: bool,
    show_parts: bool,
    show_input_forms: bool,
    executable: Thunk | Flow | None = None,
) -> list[click.Parameter]:
    args: list[click.Parameter] = []
    if show_thunk:
        args.append(
            _HelpOnlyArgument(
                param_decls=["target"],
                metavar="TARGET",
                required=False,
                default=None,
                expose_value=False,
                help="Thunk or flow to invoke.",
                rich_help_panel="Arguments",
            )
        )
    if show_params:
        executable_params = () if executable is None else tuple(executable.params)
        if not executable_params:
            args.append(
                _HelpOnlyArgument(
                    param_decls=["params"],
                    metavar="NAME=VALUE",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Set one named thunk parameter. Repeat as needed.",
                    rich_help_panel="Params",
                )
            )
        else:
            for param in executable_params:
                required = "required" if not param.optional else "optional"
                args.append(
                    _HelpOnlyArgument(
                        param_decls=[f"param_{param.name}"],
                        metavar=_param_assignment_label(param),
                        required=False,
                        default=None,
                        expose_value=False,
                        help=f"{param.type_name or 'Text'}; {required}.",
                        rich_help_panel="Params",
                    )
                )
    if show_parts:
        if show_input_forms:
            args.extend(
                [
                    _HelpOnlyArgument(
                        param_decls=["part_text"],
                        metavar="TEXT",
                        required=False,
                        default=None,
                        expose_value=False,
                        help="Text part. Use @@TEXT for literal text starting with @.",
                        rich_help_panel="Input",
                    ),
                    _HelpOnlyArgument(
                        param_decls=["part_file"],
                        metavar="@PATH",
                        required=False,
                        default=None,
                        expose_value=False,
                        help="File input. Modality is inferred from the extension.",
                        rich_help_panel="Input",
                    ),
                ]
            )
    return args


def _thunk_name(thunk: Thunk) -> str:
    return thunk.name or "main"


def _executable_name(executable: Thunk | Flow) -> str:
    if isinstance(executable, Thunk):
        return _thunk_name(executable)
    return executable.flow_name()


def _rich_format_roaming_help(
    *,
    obj: click.Command | click.Group,
    ctx: click.Context,
    markup_mode: MarkupMode,
    show_commands: bool,
) -> None:
    console = rich_utils._get_rich_console()
    console.print(
        rich_utils.Padding(rich_utils.highlighter(obj.get_usage(ctx)), 1),
        style=rich_utils.STYLE_USAGE_COMMAND,
    )
    if obj.help:
        console.print(
            rich_utils.Padding(
                rich_utils.Align(
                    rich_utils._get_help_text(obj=obj, markup_mode=markup_mode),
                    pad=False,
                ),
                (0, 1, 1, 1),
            )
        )

    options: list[click.Option] = []
    params_args: list[click.Argument] = []
    input_args: list[click.Argument] = []
    for param in obj.get_params(ctx):
        if getattr(param, "hidden", False):
            continue
        if isinstance(param, click.Option):
            options.append(param)
            continue
        if isinstance(param, click.Argument):
            panel_name = getattr(param, rich_utils._RICH_HELP_PANEL_NAME, None)
            if panel_name == "Params":
                params_args.append(param)
            elif panel_name == "Input":
                input_args.append(param)

    rich_utils._print_options_panel(
        name=rich_utils.OPTIONS_PANEL_TITLE,
        params=options,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if show_commands and isinstance(obj, click.Group):
        commands = [command for name in obj.list_commands(ctx) if (command := obj.get_command(ctx, name)) and not command.hidden]
        max_cmd_len = max((len(command.name or "") for command in commands), default=0)
        rich_utils._print_commands_panel(
            name="Targets",
            commands=commands,
            markup_mode=markup_mode,
            console=console,
            cmd_len=max_cmd_len,
        )

    _print_argument_examples_panel(
        name="Params",
        params=params_args,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )
    _print_argument_examples_panel(
        name="Input",
        params=input_args,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if obj.epilog:
        lines = obj.epilog.split("\n\n")
        epilogue = "\n".join([line.replace("\n", " ").strip() for line in lines])
        epilogue_text = rich_utils._make_rich_text(text=epilogue, markup_mode=markup_mode)
        console.print(rich_utils.Padding(rich_utils.Align(epilogue_text, pad=False), 1))


def _print_argument_examples_panel(
    *,
    name: str,
    params: list[click.Argument],
    ctx: click.Context,
    markup_mode: MarkupMode,
    console: Console,
) -> None:
    if not params:
        return
    table = rich_utils.Table(
        highlight=True,
        show_header=False,
        expand=True,
        box=getattr(rich_utils.box, rich_utils.STYLE_OPTIONS_TABLE_BOX, None),
        show_lines=rich_utils.STYLE_OPTIONS_TABLE_SHOW_LINES,
        leading=rich_utils.STYLE_OPTIONS_TABLE_LEADING,
        border_style=rich_utils.STYLE_OPTIONS_TABLE_BORDER_STYLE,
        row_styles=rich_utils.STYLE_OPTIONS_TABLE_ROW_STYLES,
        pad_edge=rich_utils.STYLE_OPTIONS_TABLE_PAD_EDGE,
        padding=rich_utils.STYLE_OPTIONS_TABLE_PADDING,
    )
    table.add_column(style=rich_utils.STYLE_METAVAR, no_wrap=True)
    table.add_column(ratio=10)
    for param in params:
        table.add_row(
            rich_utils.metavar_highlighter(param.make_metavar(ctx=ctx)),
            rich_utils._get_parameter_help(param=param, ctx=ctx, markup_mode=markup_mode),
        )
    console.print(
        rich_utils.Panel(
            table,
            border_style=rich_utils.STYLE_OPTIONS_PANEL_BORDER,
            title=name,
            title_align=rich_utils.ALIGN_OPTIONS_PANEL,
        )
    )
