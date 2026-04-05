"""Long-lived runtime process for one active agent."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import BusStore
from toolang.channels import ChannelState, create_channel_plugin
from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget
from toolang.concepts.execution import Message, RuntimeLoop
from toolang.concepts.layout import AgentHome
from toolang.concepts.persisted import (
    ChannelBinding,
    ChannelsConfig,
    PollState,
    PulseItemState,
)
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ToolangError
from toolang.program.ast import Thunk

from .api_models import ChatRequest, RunRequest
from .execution_store import ExecutionStore
from .messages import chat_message
from .model_exec import ModelExecutionEventHandler
from .prepare import refresh_prepared
from .pulse import PulseSubmission, collect_pulse_submissions
from .requests import RunSubmission, RunSubmissionKind
from .runner import ChatResult, InvokeResult, Runner
from .scheduler import RuntimeScheduler
from .server.state import (
    activate_running_agent,
    deactivate_running_agent,
    has_running_state,
    touch_running_agent,
)
from .state import RuntimeState
from .watcher import watch_runtime_process
from .work import materialize_task_mirror_output

HEARTBEAT_INTERVAL_SEC = 5.0
IDLE_POLL_SLEEP_SEC = 0.25
FAILED_POLL_SLEEP_SEC = 1.0
PULSE_SCAN_INTERVAL_SEC = 1.0


class RuntimeProcess:
    """One long-lived runtime process with shared stores and scheduler."""

    def __init__(
        self,
        prepared: PreparedAgent,
        *,
        agents_db_path: Path,
        bus_db_path: Path,
        host: str,
        port: int,
        sandbox: str,
        public_host: str | None = None,
        runtime_loops: tuple[RuntimeLoop, ...] = ("server",),
        channels_config: ChannelsConfig | None = None,
    ) -> None:
        self.state = RuntimeState(prepared=prepared)
        self.agents_db_path = agents_db_path
        self.bus_db_path = bus_db_path
        self.host = host
        self.port = port
        self.public_host = public_host or host
        self.sandbox = SandboxSpec.parse(sandbox).spec
        self.runtime_loops = runtime_loops
        self.channels_config = channels_config or ChannelsConfig()
        self.endpoint = f"http://{self.public_host}:{self.port}"
        self.scheduler = RuntimeScheduler()
        self.activation_id = uuid.uuid4().hex

    @property
    def prepared(self) -> PreparedAgent:
        return self.state.current_prepared()

    @property
    def room(self):
        return self.state.require_room()

    @property
    def bus(self) -> BusStore:
        return self.state.require_bus()

    @property
    def execution(self) -> ExecutionStore:
        return self.state.require_execution()

    @property
    def channel_plugins(self):
        return self.state.channel_plugins

    def refresh_live(self) -> PreparedAgent:
        """Refresh and replace the current live prepared snapshot."""

        return self.state.replace_prepared(refresh_prepared(self.prepared))

    def current_prepared(self, *, refresh: bool = True) -> PreparedAgent:
        """Return the current live prepared snapshot."""

        if refresh:
            return self.refresh_live()
        return self.prepared

    def start(self) -> None:
        """Start the runtime process and persist one started activation."""

        if self.state.started:
            return
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        self.state.room = room
        self.state.bus = BusStore(self.bus_db_path)
        self.state.execution = ExecutionStore(room.execution_db_path)
        self.state.channel_plugins = {
            name: create_channel_plugin(binding.plugin, config=binding.config)
            for name, binding in self.channels_config.channels.items()
        }
        self.execution.begin_activation(
            agent=self.prepared.ref,
            activation_id=self.activation_id,
            activation_kind="runtime",
            sandbox=self.sandbox,
            cap_scopes=self.prepared.cap_scopes.labels(),
            runtime_loops=self.runtime_loops,
        )
        activate_running_agent(
            self.prepared,
            agents_db_path=self.agents_db_path,
            bus=self.bus,
            endpoint=self.endpoint,
            sandbox=self.sandbox,
        )
        self.state.stop_event = threading.Event()
        self.state.started = True
        self._start_background_thread(
            name=f"toolang-heartbeat-{self.prepared.ref.name}",
            target=self._heartbeat_loop,
        )
        self._start_background_thread(
            name=f"toolang-watch-{self.prepared.ref.name}",
            target=watch_runtime_process,
            args=(self,),
        )
        if "poll" in self.runtime_loops:
            for binding_name in sorted(self.channels_config.channels):
                self._start_background_thread(
                    name=f"toolang-poll-{self.prepared.ref.name}-{binding_name}",
                    target=self._poll_binding_loop,
                    args=(
                        room,
                        binding_name,
                        self.channels_config.channels[binding_name],
                    ),
                )
        if "pulse" in self.runtime_loops:
            self._start_background_thread(
                name=f"toolang-pulse-{self.prepared.ref.name}",
                target=self._pulse_loop,
                args=(room,),
            )

    def stop(self) -> None:
        """Stop the runtime process and persist one stopped activation."""

        if not self.state.started:
            return
        self.state.require_stop_event().set()
        for thread in self.state.threads:
            thread.join(timeout=5.0)
        self.state.threads.clear()
        self.scheduler.close()
        try:
            self.execution.finish_activation(
                activation_id=self.activation_id,
                status="stopped",
            )
            if has_running_state(self.prepared, agents_db_path=self.agents_db_path):
                deactivate_running_agent(
                    self.prepared,
                    agents_db_path=self.agents_db_path,
                    bus=self.bus,
                    endpoint=self.endpoint,
                    sandbox=self.sandbox,
                )
        finally:
            self.bus.close()
            self.execution.close()
            self.state.bus = None
            self.state.execution = None
            self.state.channel_plugins = {}
            self.state.pulse_pending = set()
            self.state.room = None
            self.state.stop_event = None
            self.state.started = False

    def touch(self) -> None:
        """Refresh the current-running summary for this process."""

        touch_running_agent(
            self.prepared,
            agents_db_path=self.agents_db_path,
            endpoint=self.endpoint,
        )

    def submit_chat(self, request: ChatRequest) -> ChatResult:
        """Submit one API chat run through the runtime scheduler."""

        submission, incoming = _build_api_chat_request(request)
        return self.scheduler.submit(
            submission,
            lambda: self._run_chat(request=request, message=incoming),
        )

    def submit_chat_stream(
        self,
        request: ChatRequest,
        on_event: ModelExecutionEventHandler,
    ) -> tuple[str, Future[ChatResult]]:
        """Submit one API chat run and stream text and tool-call events."""

        run_id = uuid.uuid4().hex
        submission, incoming = _build_api_chat_request(request)
        future = self.scheduler.submit_async(
            submission,
            lambda: self._run_chat(
                request=request,
                message=incoming,
                run_id=run_id,
                stream_event=on_event,
            ),
        )
        return run_id, future

    def submit_run(self, request: RunRequest) -> InvokeResult:
        """Submit one API run through the runtime scheduler."""

        submission = RunSubmission(kind="invoke", thread_id=None)
        return self.scheduler.submit(
            submission,
            lambda: self._run_invoke(request=request),
        )

    def submit_inbound(
        self,
        binding_name: str,
        delivery: InboundDelivery,
    ) -> ChatResult | InvokeResult:
        """Submit one channel delivery through the runtime scheduler."""

        bound_delivery = _bind_delivery(binding_name, delivery)
        if bound_delivery.origin == "chat":
            message = Message(
                origin="chat",
                channel=bound_delivery.channel,
                sender=bound_delivery.sender,
                thread_id=bound_delivery.thread_id,
                text=bound_delivery.text,
                meta=dict(bound_delivery.meta),
            )
            submission = RunSubmission(
                kind="chat",
                thread_id=bound_delivery.thread_id,
                message=message,
            )
            return self.scheduler.submit(
                submission,
                lambda: self._run_inbound_chat(
                    binding_name=binding_name,
                    delivery=bound_delivery,
                    message=message,
                ),
            )
        if bound_delivery.origin in {"invoke", "task", "chore", "will"}:
            submission = RunSubmission(
                kind=bound_delivery.origin,
                thread_id=bound_delivery.thread_id,
            )
            return self.scheduler.submit(
                submission,
                lambda: self._run_inbound_run(
                    binding_name=binding_name,
                    delivery=bound_delivery,
                ),
            )
        raise ToolangError(
            f"Inbound channel delivery origin is not supported yet: {bound_delivery.origin}"
        )

    def pulse_pending_keys(self) -> set[str]:
        """Return the current pulse-pending keys."""

        return self.state.pulse_pending_keys()

    def _runner(self, prepared: PreparedAgent) -> Runner:
        return Runner(
            prepared,
            bus_db_path=self.bus_db_path,
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
        )

    def _run_self_run(
        self,
        *,
        origin: RunSubmissionKind,
        thread_id: str,
        text: str,
    ) -> InvokeResult:
        current = self.current_prepared()
        selected_thunk = _select_named_or_origin_thunk(current, None, origin)
        return self._runner(current).invoke(
            selected_thunk,
            user_input=text,
            origin=origin,
            thread_id=thread_id,
            sender="self",
        )

    def _run_chat(
        self,
        *,
        request: ChatRequest,
        message: Message,
        run_id: str | None = None,
        stream_event: ModelExecutionEventHandler | None = None,
    ) -> ChatResult:
        current = self.current_prepared()
        selected_thunk = _select_chat_thunk(current, request.thunk)
        return self._runner(current).chat(
            selected_thunk,
            message=message,
            model=request.model,
            run_id=run_id,
            stream_event=stream_event,
        )

    def _run_invoke(self, *, request: RunRequest) -> InvokeResult:
        current = self.current_prepared()
        selected_thunk = current.program.get_thunk(request.thunk)
        return self._runner(current).invoke(
            selected_thunk,
            user_input=request.input,
            model=request.model,
        )

    def _run_inbound_chat(
        self,
        *,
        binding_name: str,
        delivery: InboundDelivery,
        message: Message,
    ) -> ChatResult:
        current = self.current_prepared()
        thunk_name = _optional_text(delivery.meta.get("thunk"))
        selected_thunk = _select_chat_thunk(current, thunk_name)
        result = self._runner(current).chat(
            selected_thunk,
            message=message,
            model=_optional_text(delivery.meta.get("model")),
        )
        if delivery.reply_target is not None:
            self._deliver_reply(
                binding_name=binding_name,
                target=delivery.reply_target,
                run_id=result.run_id,
                text=result.output,
            )
        return result

    def _run_inbound_run(
        self,
        *,
        binding_name: str,
        delivery: InboundDelivery,
    ) -> InvokeResult:
        current = self.current_prepared()
        thunk_name = _optional_text(delivery.meta.get("thunk"))
        selected_thunk = _select_named_or_origin_thunk(
            current,
            thunk_name,
            delivery.origin,
        )
        result = self._runner(current).invoke(
            selected_thunk,
            user_input=delivery.text,
            model=_optional_text(delivery.meta.get("model")),
            origin=delivery.origin,
            thread_id=delivery.thread_id,
            sender=delivery.sender,
            input_meta=dict(delivery.meta),
        )
        if delivery.reply_target is not None:
            self._deliver_reply(
                binding_name=binding_name,
                target=delivery.reply_target,
                run_id=result.run_id,
                text=result.output,
            )
        return result

    def _deliver_reply(
        self,
        *,
        binding_name: str,
        target: ReplyTarget,
        run_id: str,
        text: str,
    ) -> None:
        plugin = self.channel_plugins.get(binding_name)
        if plugin is None:
            raise ToolangError(f"Unknown runtime channel binding: {binding_name}")
        try:
            result = plugin.deliver(target, OutboundMessage(text=text))
        except Exception as exc:
            self.execution.append_step(
                run_id=run_id,
                step_kind="delivery",
                status="failed",
                input_json={
                    "channel": binding_name,
                    "address": target.address,
                    "text": text,
                },
                output_json={},
                error=str(exc),
            )
            return
        self.execution.append_step(
            run_id=run_id,
            step_kind="delivery",
            status="finished" if result.ok else "failed",
            input_json={
                "channel": binding_name,
                "address": target.address,
                "text": text,
            },
            output_json={
                "ok": result.ok,
                "remote_id": result.remote_id,
                "detail": result.detail,
                "meta": result.meta,
            },
            error=None if result.ok else result.detail,
        )

    def _start_background_thread(
        self,
        *,
        name: str,
        target,
        args: tuple[object, ...] = (),
    ) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self.state.threads.append(thread)

    def _heartbeat_loop(self) -> None:
        stop_event = self.state.require_stop_event()
        while not stop_event.wait(HEARTBEAT_INTERVAL_SEC):
            self.touch()

    def _poll_binding_loop(
        self,
        room,
        binding_name: str,
        binding: ChannelBinding,
    ) -> None:
        plugin = self.channel_plugins.get(binding_name)
        if plugin is None:
            return
        stop_event = self.state.require_stop_event()
        state_path = room.poll_state_path(binding_name)
        while not stop_event.is_set():
            persisted_state = PollState.load(state_path) if state_path.exists() else PollState()
            try:
                result = plugin.poll(
                    ChannelState(
                        cursor=persisted_state.cursor,
                        meta=dict(persisted_state.meta),
                    )
                )
                PollState(
                    cursor=result.next_state.cursor,
                    meta=dict(result.next_state.meta),
                ).save(state_path)
                for delivery in result.deliveries:
                    self.submit_inbound(binding_name, delivery)
                if not result.deliveries:
                    stop_event.wait(IDLE_POLL_SLEEP_SEC)
            except Exception:
                traceback.print_exc()
                stop_event.wait(FAILED_POLL_SLEEP_SEC)

    def _pulse_loop(self, room) -> None:
        stop_event = self.state.require_stop_event()
        state_path = room.pulse_state_path
        while not stop_event.is_set():
            try:
                persisted = self.state.load_pulse_state(state_path)
                next_state, submissions = collect_pulse_submissions(
                    room,
                    self.prepared.ref,
                    persisted,
                    pending_keys=self.state.pulse_pending_keys(),
                )
                if next_state != persisted:
                    self.state.save_pulse_state(state_path, next_state)
                for submission in submissions:
                    self._submit_pulse(submission)
                stop_event.wait(PULSE_SCAN_INTERVAL_SEC)
            except Exception:
                traceback.print_exc()
                stop_event.wait(FAILED_POLL_SLEEP_SEC)

    def _submit_pulse(self, submission: PulseSubmission) -> None:
        pending_key = f"{submission.kind}:{submission.key}"
        if not self.state.mark_pulse_pending(pending_key):
            return
        self._update_pulse_item(
            submission.kind,
            submission.key,
            last_started_at=_utc_datetime_now(),
            last_status=None,
            last_run_id=None,
        )
        future = self.scheduler.submit_async(
            RunSubmission(kind=submission.kind, thread_id=submission.thread_id),
            lambda: self._run_self_run(
                origin=submission.kind,
                thread_id=submission.thread_id,
                text=submission.text,
            ),
        )
        future.add_done_callback(
            lambda completed: self._finish_pulse_submission(
                pending_key,
                submission,
                completed,
            )
        )

    def _finish_pulse_submission(
        self,
        pending_key: str,
        submission: PulseSubmission,
        future,
    ) -> None:
        try:
            exc = future.exception()
            if exc is None:
                result = future.result()
                if submission.kind == "chore":
                    materialize_task_mirror_output(self.room, result.output)
                self._update_pulse_item(
                    submission.kind,
                    submission.key,
                    last_finished_at=_utc_datetime_now(),
                    last_status="finished",
                    last_run_id=result.run_id,
                )
            else:
                self._update_pulse_item(
                    submission.kind,
                    submission.key,
                    last_finished_at=_utc_datetime_now(),
                    last_status="failed",
                )
        finally:
            self.state.clear_pulse_pending(pending_key)

    def _update_pulse_item(self, kind: RunSubmissionKind, key: str, **changes) -> None:
        state_path = self.room.pulse_state_path
        state = self.state.load_pulse_state(state_path)
        if kind == "task":
            current = state.tasks.get(key, PulseItemState())
            state.tasks[key] = current.model_copy(update=changes)
        elif kind == "chore":
            current = state.chores.get(key, PulseItemState())
            state.chores[key] = current.model_copy(update=changes)
        elif kind == "will":
            state.will = state.will.model_copy(update=changes)
        else:
            return
        self.state.save_pulse_state(state_path, state)


def _select_chat_thunk(prepared: PreparedAgent, thunk_name: str | None) -> Thunk:
    return _select_named_or_origin_thunk(prepared, thunk_name, "chat")


def _build_api_chat_request(request: ChatRequest) -> tuple[RunSubmission, Message]:
    thread_id = request.thread.strip()
    if not thread_id:
        raise ToolangError("Chat thread may not be empty.")
    text = request.message.strip()
    if not text:
        raise ToolangError("Chat message may not be empty.")
    incoming = chat_message(
        channel="api",
        sender="owner",
        thread_id=thread_id,
        text=text,
    )
    return RunSubmission(kind="chat", thread_id=thread_id, message=incoming), incoming


def _select_named_or_origin_thunk(
    prepared: PreparedAgent,
    thunk_name: str | None,
    origin_name: str,
) -> Thunk:
    if thunk_name is not None:
        return prepared.program.get_thunk(thunk_name)
    try:
        return prepared.program.get_thunk(origin_name)
    except ToolangError:
        return prepared.program.default_thunk()


def _bind_delivery(binding_name: str, delivery: InboundDelivery) -> InboundDelivery:
    reply_target = delivery.reply_target
    if reply_target is not None and reply_target.channel != binding_name:
        reply_target = replace(reply_target, channel=binding_name)
    if delivery.channel == binding_name and reply_target is delivery.reply_target:
        return delivery
    return replace(delivery, channel=binding_name, reply_target=reply_target)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_datetime_now() -> datetime:
    return datetime.now(timezone.utc)
