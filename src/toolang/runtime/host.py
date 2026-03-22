"""Long-lived runtime host for one active agent process."""

from __future__ import annotations

import uuid
from pathlib import Path

from toolang.agent.prepared import PreparedAgent, prepare_agent
from toolang.bus.db import BusStore
from toolang.concepts.execution import Message, RuntimeLoop
from toolang.concepts.layout import AgentHome
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
from .requests import TurnRequest
from .scheduler import RuntimeScheduler
from .server.state import (
    activate_running_agent,
    deactivate_running_agent,
    has_running_state,
    touch_running_agent,
)


class RuntimeHost:
    """One long-lived runtime activation with shared stores and scheduler."""

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
    ) -> None:
        self.prepared = prepared
        self.agents_db_path = agents_db_path
        self.bus_db_path = bus_db_path
        self.host = host
        self.port = port
        self.public_host = public_host or host
        self.sandbox = SandboxSpec.parse(sandbox).spec
        self.runtime_loops = runtime_loops
        self.endpoint = f"http://{self.public_host}:{self.port}"
        self.scheduler = RuntimeScheduler()
        self.activation_id = uuid.uuid4().hex
        self._bus: BusStore | None = None
        self._chats: ChatStore | None = None
        self._execution: ExecutionStore | None = None
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
        """Start the runtime host and persist one running activation."""

        if self._started:
            return
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        self._bus = BusStore(self.bus_db_path)
        self._chats = ChatStore(room.chats_db_path)
        self._execution = ExecutionStore(room.execution_db_path)
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
        self._started = True

    def stop(self) -> None:
        """Stop the runtime host and persist a stopped activation."""

        if not self._started:
            return
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
            self.chats.close()
            self.bus.close()
            self.execution.close()
            self._bus = None
            self._chats = None
            self._execution = None
            self._started = False

    def touch(self) -> None:
        """Refresh the current-running summary for this host."""

        touch_running_agent(
            self.prepared,
            agents_db_path=self.agents_db_path,
            endpoint=self.endpoint,
        )

    def submit_chat(self, request: ChatRequest) -> ChatResult:
        """Submit one chat turn through the runtime scheduler."""

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
        """Submit one non-chat run through the runtime scheduler."""

        turn_request = TurnRequest(kind="invoke", thread_id=None)
        return self.scheduler.submit(
            turn_request,
            lambda: self._run_invoke_turn(request=request),
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
            activation_id=self.activation_id,
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
            activation_id=self.activation_id,
        )


def _select_chat_thunk(prepared: PreparedAgent, thunk_name: str | None) -> Thunk:
    if thunk_name is not None:
        return prepared.program.get_thunk(thunk_name)
    try:
        return prepared.program.get_thunk("chat")
    except ToolangError:
        return prepared.program.default_thunk()
