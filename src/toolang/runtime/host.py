"""Long-lived runtime host for one active agent process."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from toolang.agent.prepared import PreparedAgent, prepare_agent
from toolang.bus.db import BusStore
from toolang.channels import ChannelPlugin, ChannelState, create_channel_plugin
from toolang.caps import load_prepared_caps
from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget
from toolang.concepts.execution import Message, RuntimeLoop
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted import (
    ChannelBinding,
    ChannelsConfig,
    HooksConfig,
    PollState,
    PulseItemState,
    PulseState,
)
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ToolangError
from toolang.program.ast import Thunk
from toolang.tools import create_tool_runtime

from .api_models import ChatRequest, RunRequest
from .execution_store import ExecutionStore
from .invoke import (
    ChatResult,
    InvokeResult,
    chat_prepared_agent,
    invoke_prepared_agent,
)
from .messages import chat_message
from .model_exec import ModelExecutionEventHandler
from .pulse import PulseSubmission, collect_pulse_submissions
from .requests import RunSubmission, RunSubmissionKind
from .scheduler import RuntimeScheduler
from .work import materialize_task_mirror_output
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
        self.activation_id = uuid.uuid4().hex
        self._bus: BusStore | None = None
        self._execution: ExecutionStore | None = None
        self._room = None
        self._channel_plugins: dict[str, ChannelPlugin] = {}
        self._pulse_pending: set[str] = set()
        self._pulse_pending_lock = threading.Lock()
        self._pulse_state_lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._threads: list[threading.Thread] = []
        self._started = False

    def current_prepared(self) -> PreparedAgent:
        """Return the latest prepared snapshot, refreshing from source when possible."""

        try:
            current = prepare_agent(
                self.prepared.ref, cap_scopes=self.prepared.cap_scopes
            )
        except FileNotFoundError:
            return self.prepared
        self.prepared = current
        return current

    @property
    def bus(self) -> BusStore:
        if self._bus is None:
            raise ToolangError("Runtime host has not been started.")
        return self._bus

    @property
    def execution(self) -> ExecutionStore:
        if self._execution is None:
            raise ToolangError("Runtime host has not been started.")
        return self._execution

    def start(self) -> None:
        """Start the runtime host and persist one started activation."""

        if self._started:
            return
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        self._room = room
        self._bus = BusStore(self.bus_db_path)
        self._execution = ExecutionStore(room.execution_db_path)
        self._channel_plugins = {
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
        """Stop the runtime host and persist one stopped activation."""

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
            self._bus = None
            self._execution = None
            self._channel_plugins = {}
            self._pulse_pending = set()
            self._room = None
            self._stop_event = None
            self._started = False

    def touch(self) -> None:
        """Refresh the current-running summary for this host."""

        touch_running_agent(
            self.prepared,
            agents_db_path=self.agents_db_path,
            endpoint=self.endpoint,
        )

    def diagnostics_snapshot(self) -> dict[str, object]:
        """Return one operational diagnostics snapshot for the active runtime."""

        room = self._require_room()
        home = AgentHome.resolve(self.prepared.ref.home)
        hooks_config = (
            HooksConfig.load(home.hooks_config_path)
            if home.hooks_config_path.exists()
            else HooksConfig()
        )

        channels: list[dict[str, object]] = []
        for name, binding in sorted(self.channels_config.channels.items()):
            plugin = self._channel_plugins.get(name)
            health_ok: bool | None = None
            health_detail: str | None = None
            health_meta: dict[str, object] = {}
            if plugin is not None:
                try:
                    health = plugin.health()
                except Exception as exc:
                    health_ok = False
                    health_detail = str(exc)
                else:
                    health_ok = health.ok
                    health_detail = health.detail
                    health_meta = dict(health.meta)
            poll_state_path = room.poll_state_path(name)
            poll_state = (
                PollState.load(poll_state_path) if poll_state_path.exists() else None
            )
            channels.append(
                {
                    "name": name,
                    "plugin": binding.plugin,
                    "ok": health_ok,
                    "detail": health_detail,
                    "meta": health_meta,
                    "poll_state_path": str(poll_state_path),
                    "poll_cursor": poll_state.cursor
                    if poll_state is not None
                    else None,
                    "poll_meta": dict(poll_state.meta)
                    if poll_state is not None
                    else {},
                }
            )

        hooks = [
            {
                "name": name,
                "path": binding.path,
                "method": binding.method,
                "plugin": binding.plugin,
            }
            for name, binding in sorted(hooks_config.hooks.items())
        ]
        pulse = None
        if (
            "pulse" in self.runtime_loops
            or room.pulse_state_path.exists()
            or self._pulse_pending_keys()
        ):
            pulse = {
                "state_path": str(room.pulse_state_path),
                "pending": sorted(self._pulse_pending_keys()),
            }
        return {
            "runtime_loops": list(self.runtime_loops),
            "hook_loop_enabled": "hook" in self.runtime_loops,
            "security": self.security_snapshot(),
            "scheduler": self.scheduler.snapshot(),
            "channels": channels,
            "hooks": hooks,
            "pulse": pulse,
        }

    def security_snapshot(
        self, *, prepared: PreparedAgent | None = None
    ) -> dict[str, object]:
        """Return one structured security snapshot for the active runtime."""

        current = prepared or self.prepared
        room = self._require_room()
        spec = SandboxSpec.parse(self.sandbox)
        visible_caps = load_prepared_caps(current)
        tool_runtime = create_tool_runtime(
            current.ref,
            sandbox=self.sandbox,
            working_directory=current.ref.home,
            visible_services=[item.service_catalog_item() for item in visible_caps.services],
        )
        enabled_families = set(tool_runtime.enabled_families())
        pulse_enabled = "pulse" in self.runtime_loops
        caps_mutable = current.ref.kind != "visiting"
        return {
            "sandbox": _sandbox_security_snapshot(
                spec=spec,
                prepared=current,
                room=room,
            ),
            "tools": {
                "filesystem": "filesystem" in enabled_families,
                "shell": "shell" in enabled_families,
                "browser_use": "browser_use" in enabled_families,
                "computer_use": "computer_use" in enabled_families,
                "service_use": "service_use" in enabled_families,
                "web_search": "web_search" in enabled_families,
                "mem_search": "memory_search" in enabled_families,
                "file_search": False,
            },
            "autonomy": {
                "chores_enabled": pulse_enabled,
                "tasks_enabled": pulse_enabled,
                "will_enabled": pulse_enabled,
                "will_path_exists": room.will_path.exists(),
            },
            "self_modification": {
                "can_add_caps": caps_mutable,
                "can_edit_will": True,
                "can_write_source": False,
                "can_persist_changes": True,
            },
        }

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
        self, binding_name: str, delivery: InboundDelivery
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
                kind="chat", thread_id=bound_delivery.thread_id, message=message
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
                kind=bound_delivery.origin, thread_id=bound_delivery.thread_id
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

    def _run_self_run(
        self,
        *,
        origin: RunSubmissionKind,
        thread_id: str,
        text: str,
    ) -> InvokeResult:
        current = self.current_prepared()
        selected_thunk = _select_named_or_origin_thunk(current, None, origin)
        return invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=text,
            origin=origin,
            thread_id=thread_id,
            sender="self",
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
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
        return chat_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            message=message,
            model=request.model,
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
            run_id=run_id,
            stream_event=stream_event,
        )

    def _run_invoke(self, *, request: RunRequest) -> InvokeResult:
        current = self.current_prepared()
        selected_thunk = current.program.get_thunk(request.thunk)
        return invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=request.input,
            model=request.model,
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
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
        result = chat_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            message=message,
            model=_optional_text(delivery.meta.get("model")),
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
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
            current, thunk_name, delivery.origin
        )
        result = invoke_prepared_agent(
            current,
            selected_thunk,
            bus_db_path=self.bus_db_path,
            user_input=delivery.text,
            model=_optional_text(delivery.meta.get("model")),
            origin=delivery.origin,
            thread_id=delivery.thread_id,
            sender=delivery.sender,
            sandbox=self.sandbox,
            execution_store=self.execution,
            activation_id=self.activation_id,
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
        plugin = self._channel_plugins.get(binding_name)
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
            persisted_state = (
                PollState.load(state_path) if state_path.exists() else PollState()
            )
            try:
                result = plugin.poll(
                    ChannelState(
                        cursor=persisted_state.cursor, meta=dict(persisted_state.meta)
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
        stop_event = self._require_stop_event()
        state_path = room.pulse_state_path
        while not stop_event.is_set():
            try:
                persisted = self._load_pulse_state(state_path)
                next_state, submissions = collect_pulse_submissions(
                    room,
                    self.prepared.ref,
                    persisted,
                    pending_keys=self._pulse_pending_keys(),
                )
                if next_state != persisted:
                    self._save_pulse_state(state_path, next_state)
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
                pending_key, submission, completed
            )
        )

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

    def _finish_pulse_submission(
        self, pending_key: str, submission: PulseSubmission, future
    ) -> None:
        try:
            exc = future.exception()
            if exc is None:
                result = future.result()
                if submission.kind == "chore":
                    materialize_task_mirror_output(self._require_room(), result.output)
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
            self._clear_pulse_pending(pending_key)

    def _update_pulse_item(self, kind: RunSubmissionKind, key: str, **changes) -> None:
        room = self._require_room()
        state_path = room.pulse_state_path
        state = self._load_pulse_state(state_path)
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
        self._save_pulse_state(state_path, state)

    def _load_pulse_state(self, state_path: Path) -> PulseState:
        with self._pulse_state_lock:
            if state_path.exists():
                return PulseState.load(state_path)
            return PulseState()

    def _save_pulse_state(self, state_path: Path, state: PulseState) -> None:
        with self._pulse_state_lock:
            state.save(state_path)

    def _require_room(self):
        if self._room is None:
            raise ToolangError("Runtime host has not been started.")
        return self._room


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


def _sandbox_security_snapshot(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    room,
) -> dict[str, object]:
    if spec.kind != "docker":
        return {
            "image": None,
            "volumes": [],
            "network_mode": "host",
            "bridge": None,
            "dns": [],
            "host_reachability": True,
        }

    root = ToolangRoot.resolve(prepared.ref.root)
    stage_dir = root.sandbox_dir(_sandbox_key(prepared.ref.name, prepared.ref.id))
    volumes = [f"{root.path}:{root.path}"]
    if not _path_is_within(prepared.ref.home, root.path):
        volumes.append(f"{prepared.ref.home}:{prepared.ref.home}")
    volumes.append(f"{stage_dir}:{room.sandbox_dir}")
    return {
        "image": spec.image,
        "volumes": volumes,
        "network_mode": "bridge",
        "bridge": "default",
        "dns": [],
        "host_reachability": False,
    }


def _sandbox_key(agent_name: str, agent_id: str) -> str:
    return f"{agent_name}-{agent_id[:12]}"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
