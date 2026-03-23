"""Long-lived runtime host for one active agent process."""

from __future__ import annotations

from dataclasses import replace
import threading
import traceback
import uuid
from pathlib import Path

from toolang.agent.prepared import PreparedAgent, prepare_agent
from toolang.bus.db import BusStore
from toolang.channels import ChannelPlugin, ChannelState, create_channel_plugin
from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget
from toolang.concepts.execution import Message, RuntimeLoop
from toolang.concepts.layout import AgentHome
from toolang.concepts.persisted import ChannelBinding, ChannelsConfig, PollState, PulseState
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ToolangError
from toolang.program.ast import Thunk

from .api_models import ChatRequest, RunRequest
from .chats import ChatStore
from .execution_store import ExecutionStore
from .invoke import (
    ChatResult,
    InvokeResult,
    chat_prepared_agent,
    invoke_prepared_agent,
)
from .messages import chat_message
from .pulse import PulseSubmission, collect_pulse_submissions
from .requests import TurnRequest, TurnRequestKind
from .scheduler import RuntimeScheduler
from .server.state import (
    activate_running_agent,
    deactivate_running_agent,
    has_running_state,
    touch_running_agent,
)

HEARTBEAT_INTERVAL_SEC = 5.0
IDLE_POLL_SLEEP_SEC = 0.25
FAILED_POLL_SLEEP_SEC = 1.0
PULSE_SCAN_INTERVAL_SEC = 1.0


class RuntimeHost:
    """One long-lived runtime run with shared stores and scheduler."""

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
        self.prepared = prepared
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
        self.run_id = uuid.uuid4().hex
        self._bus: BusStore | None = None
        self._chats: ChatStore | None = None
        self._execution: ExecutionStore | None = None
        self._channel_plugins: dict[str, ChannelPlugin] = {}
        self._pulse_pending: set[str] = set()
        self._pulse_pending_lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._threads: list[threading.Thread] = []
        self._started = False

    @property
    def bus(self) -> BusStore:
        if self._bus is None:
            raise ToolangError("Runtime host has not been started.")
        return self._bus

    @property
    def chats(self) -> ChatStore:
        if self._chats is None:
            raise ToolangError("Runtime host has not been started.")
        return self._chats

    @property
    def execution(self) -> ExecutionStore:
        if self._execution is None:
            raise ToolangError("Runtime host has not been started.")
        return self._execution

    def start(self) -> None:
        """Start the runtime host and persist one started run."""

        if self._started:
            return
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        self._bus = BusStore(self.bus_db_path)
        self._chats = ChatStore(room.chats_db_path)
        self._execution = ExecutionStore(room.execution_db_path)
        self._channel_plugins = {
            name: create_channel_plugin(binding.plugin, config=binding.config)
            for name, binding in self.channels_config.channels.items()
        }
        self.execution.begin_run(
            agent=self.prepared.ref,
            run_id=self.run_id,
            run_kind="runtime",
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
        self._stop_event = threading.Event()
        self._started = True
        self._start_background_thread(
            name=f"toolang-heartbeat-{self.prepared.ref.name}",
            target=self._heartbeat_loop,
        )
        if "poll" in self.runtime_loops:
            for binding_name in sorted(self.channels_config.channels):
                self._start_background_thread(
                    name=f"toolang-poll-{self.prepared.ref.name}-{binding_name}",
                    target=self._poll_binding_loop,
                    args=(room, binding_name, self.channels_config.channels[binding_name]),
                )
        if "pulse" in self.runtime_loops:
            self._start_background_thread(
                name=f"toolang-pulse-{self.prepared.ref.name}",
                target=self._pulse_loop,
                args=(room,),
            )

    def stop(self) -> None:
        """Stop the runtime host and persist one stopped run."""

        if not self._started:
            return
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()
        self.scheduler.close()
        try:
            self.execution.finish_run(
                run_id=self.run_id,
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
            self.chats.close()
            self.bus.close()
            self.execution.close()
            self._bus = None
            self._chats = None
            self._execution = None
            self._channel_plugins = {}
            self._pulse_pending = set()
            self._stop_event = None
            self._started = False

    def touch(self) -> None:
        """Refresh the current-running summary for this host."""

        touch_running_agent(
            self.prepared,
            agents_db_path=self.agents_db_path,
            endpoint=self.endpoint,
        )

    def submit_chat(self, request: ChatRequest) -> ChatResult:
        """Submit one API chat turn through the runtime scheduler."""

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
        turn_request = TurnRequest(kind="chat", thread_id=thread_id, message=incoming)
        return self.scheduler.submit(
            turn_request,
            lambda: self._run_chat_turn(request=request, message=incoming),
        )

    def submit_run(self, request: RunRequest) -> InvokeResult:
        """Submit one API run turn through the runtime scheduler."""

        turn_request = TurnRequest(kind="invoke", thread_id=None)
        return self.scheduler.submit(
            turn_request,
            lambda: self._run_invoke_turn(request=request),
        )

    def submit_inbound(self, binding_name: str, delivery: InboundDelivery) -> ChatResult | InvokeResult:
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
            turn_request = TurnRequest(kind="chat", thread_id=bound_delivery.thread_id, message=message)
            return self.scheduler.submit(
                turn_request,
                lambda: self._run_inbound_chat(
                    binding_name=binding_name,
                    delivery=bound_delivery,
                    message=message,
                ),
            )
        if bound_delivery.origin == "invoke":
            turn_request = TurnRequest(kind="invoke", thread_id=bound_delivery.thread_id)
            return self.scheduler.submit(
                turn_request,
                lambda: self._run_inbound_invoke(
                    binding_name=binding_name,
                    delivery=bound_delivery,
                ),
            )
        raise ToolangError(f"Inbound channel delivery origin is not supported yet: {bound_delivery.origin}")

    def _run_self_turn(
        self,
        *,
        origin: TurnRequestKind,
        thread_id: str,
        text: str,
        thunk_name: str | None,
        model: str | None,
    ) -> InvokeResult:
        current = prepare_agent(self.prepared.ref, cap_scopes=self.prepared.cap_scopes)
        selected_thunk = _select_named_or_origin_thunk(current, thunk_name, origin)
        return invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=text,
            model=model,
            origin=origin,
            thread_id=thread_id,
            sender="self",
            sandbox=self.sandbox,
            execution_store=self.execution,
            process_run_id=self.run_id,
        )

    def _run_chat_turn(self, *, request: ChatRequest, message: Message) -> ChatResult:
        current = prepare_agent(self.prepared.ref, cap_scopes=self.prepared.cap_scopes)
        selected_thunk = _select_chat_thunk(current, request.thunk)
        return chat_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            chat_store=self.chats,
            message=message,
            model=request.model,
            sandbox=self.sandbox,
            execution_store=self.execution,
            process_run_id=self.run_id,
        )

    def _run_invoke_turn(self, *, request: RunRequest) -> InvokeResult:
        current = prepare_agent(self.prepared.ref, cap_scopes=self.prepared.cap_scopes)
        selected_thunk = current.program.get_thunk(request.thunk)
        return invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=request.input,
            model=request.model,
            sandbox=self.sandbox,
            execution_store=self.execution,
            process_run_id=self.run_id,
        )

    def _run_inbound_chat(
        self,
        *,
        binding_name: str,
        delivery: InboundDelivery,
        message: Message,
    ) -> ChatResult:
        current = prepare_agent(self.prepared.ref, cap_scopes=self.prepared.cap_scopes)
        thunk_name = _optional_text(delivery.meta.get("thunk"))
        selected_thunk = _select_chat_thunk(current, thunk_name)
        result = chat_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            chat_store=self.chats,
            message=message,
            model=_optional_text(delivery.meta.get("model")),
            sandbox=self.sandbox,
            execution_store=self.execution,
            process_run_id=self.run_id,
        )
        if delivery.reply_target is not None:
            self._deliver_reply(
                binding_name=binding_name,
                target=delivery.reply_target,
                turn_id=result.run_id,
                text=result.output,
            )
        return result

    def _run_inbound_invoke(
        self,
        *,
        binding_name: str,
        delivery: InboundDelivery,
    ) -> InvokeResult:
        current = prepare_agent(self.prepared.ref, cap_scopes=self.prepared.cap_scopes)
        thunk_name = _optional_text(delivery.meta.get("thunk"))
        selected_thunk = (
            current.program.get_thunk(thunk_name)
            if thunk_name is not None
            else current.program.default_thunk()
        )
        result = invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=delivery.text,
            model=_optional_text(delivery.meta.get("model")),
            origin="invoke",
            thread_id=delivery.thread_id,
            sandbox=self.sandbox,
            execution_store=self.execution,
            process_run_id=self.run_id,
        )
        if delivery.reply_target is not None:
            self._deliver_reply(
                binding_name=binding_name,
                target=delivery.reply_target,
                turn_id=result.run_id,
                text=result.output,
            )
        return result

    def _deliver_reply(
        self,
        *,
        binding_name: str,
        target: ReplyTarget,
        turn_id: str,
        text: str,
    ) -> None:
        plugin = self._channel_plugins.get(binding_name)
        if plugin is None:
            raise ToolangError(f"Unknown runtime channel binding: {binding_name}")
        try:
            result = plugin.deliver(target, OutboundMessage(text=text))
        except Exception as exc:
            self.execution.append_step(
                turn_id=turn_id,
                step_kind="delivery",
                status="failed",
                input_json={"channel": binding_name, "address": target.address, "text": text},
                output_json={},
                error=str(exc),
            )
            return
        self.execution.append_step(
            turn_id=turn_id,
            step_kind="delivery",
            status="finished" if result.ok else "failed",
            input_json={"channel": binding_name, "address": target.address, "text": text},
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
        self._threads.append(thread)

    def _heartbeat_loop(self) -> None:
        stop_event = self._require_stop_event()
        while not stop_event.wait(HEARTBEAT_INTERVAL_SEC):
            self.touch()

    def _poll_binding_loop(
        self,
        room,
        binding_name: str,
        binding: ChannelBinding,
    ) -> None:
        plugin = self._channel_plugins.get(binding_name)
        if plugin is None:
            return
        stop_event = self._require_stop_event()
        state_path = room.poll_state_path(binding_name)
        while not stop_event.is_set():
            persisted_state = PollState.load(state_path) if state_path.exists() else PollState()
            try:
                result = plugin.poll(
                    ChannelState(cursor=persisted_state.cursor, meta=dict(persisted_state.meta))
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
        stop_event = self._require_stop_event()
        state_path = room.pulse_state_path
        while not stop_event.is_set():
            try:
                persisted = PulseState.load(state_path) if state_path.exists() else PulseState()
                next_state, submissions = collect_pulse_submissions(
                    room,
                    self.prepared.ref,
                    persisted,
                    pending_keys=self._pulse_pending_keys(),
                )
                if next_state != persisted:
                    next_state.save(state_path)
                for submission in submissions:
                    self._submit_pulse(submission)
                stop_event.wait(PULSE_SCAN_INTERVAL_SEC)
            except Exception:
                traceback.print_exc()
                stop_event.wait(FAILED_POLL_SLEEP_SEC)

    def _require_stop_event(self) -> threading.Event:
        if self._stop_event is None:
            raise ToolangError("Runtime host has not been started.")
        return self._stop_event

    def _submit_pulse(self, submission: PulseSubmission) -> None:
        pending_key = f"{submission.kind}:{submission.key}"
        if not self._mark_pulse_pending(pending_key):
            return
        future = self.scheduler.submit_async(
            TurnRequest(kind=submission.kind, thread_id=submission.thread_id),
            lambda: self._run_self_turn(
                origin=submission.kind,
                thread_id=submission.thread_id,
                text=submission.text,
                thunk_name=submission.thunk,
                model=submission.model,
            ),
        )
        future.add_done_callback(lambda _future: self._clear_pulse_pending(pending_key))

    def _pulse_pending_keys(self) -> set[str]:
        with self._pulse_pending_lock:
            return set(self._pulse_pending)

    def _mark_pulse_pending(self, pending_key: str) -> bool:
        with self._pulse_pending_lock:
            if pending_key in self._pulse_pending:
                return False
            self._pulse_pending.add(pending_key)
            return True

    def _clear_pulse_pending(self, pending_key: str) -> None:
        with self._pulse_pending_lock:
            self._pulse_pending.discard(pending_key)


def _select_chat_thunk(prepared: PreparedAgent, thunk_name: str | None) -> Thunk:
    return _select_named_or_origin_thunk(prepared, thunk_name, "chat")


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
