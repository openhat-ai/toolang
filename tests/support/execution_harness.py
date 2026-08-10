"""Readable test harness for complete execution scenarios."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Percept
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelPartUpdate,
    ModelStreamHandler,
)
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.lang import Program
from toolang.state.state import AgentState, agent_state_version
from toolang.setup import AgentSetup

TEST_MODEL_REF = "test/scripted"


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """One model target and normalized call observed by the fake adapter."""

    target: ModelTarget
    call: ModelCall


class AsyncGate:
    """Pause one fake invocation at an observable async checkpoint."""

    def __init__(self) -> None:
        self._entered = asyncio.Event()
        self._released = asyncio.Event()
        self._error: Exception | None = None

    async def wait(self) -> None:
        """Mark the model call entered and wait for the test to release it."""

        self._entered.set()
        await self._released.wait()
        if self._error is not None:
            raise self._error

    async def wait_until_entered(self) -> None:
        """Wait until the adapter reaches this model turn."""

        await self._entered.wait()

    @property
    def entered(self) -> bool:
        """Return whether the guarded invocation has started."""

        return self._entered.is_set()

    def release(self) -> None:
        """Allow the scripted model turn to return its configured result."""

        self._released.set()

    def fail(self, error: Exception) -> None:
        """Release the scripted model turn by raising an error."""

        self._error = error
        self._released.set()


@dataclass(frozen=True, slots=True)
class ScriptedModelTurn:
    """One final model result with optional streaming updates."""

    result: ModelCallResult
    updates: tuple[ModelPartUpdate, ...] = ()
    gate: AsyncGate | None = None
    after_updates_gate: AsyncGate | None = None
    error: Exception | None = None


ScriptedResponse = ModelCallResult | ScriptedModelTurn | Exception


class ScriptedModelAdapter:
    """Return deterministic model results while recording normalized calls."""

    name = "scripted"
    description = "Deterministic execution-test model adapter."

    def __init__(self, responses: Sequence[ScriptedResponse]) -> None:
        self._responses = deque(responses)
        self.invocations: list[ModelInvocation] = []

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        turn = self._take_turn(target, request)
        if turn.gate is not None:
            await turn.gate.wait()
        if turn.updates:
            raise AssertionError("streaming updates require a streaming test model")
        if turn.error is not None:
            raise turn.error
        return turn.result

    async def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        turn = self._take_turn(target, request)
        if turn.gate is not None:
            await turn.gate.wait()
        for update in turn.updates:
            await on_event(update)
        if turn.after_updates_gate is not None:
            await turn.after_updates_gate.wait()
        if turn.error is not None:
            raise turn.error
        return turn.result

    def _take_turn(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ScriptedModelTurn:
        self.invocations.append(ModelInvocation(target=target, call=request))
        if not self._responses:
            raise AssertionError("scripted model responses are exhausted")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        if isinstance(response, ScriptedModelTurn):
            return response
        return ScriptedModelTurn(result=response)

    @property
    def pending_responses(self) -> int:
        """Return the number of responses not consumed by execution."""

        return len(self._responses)


class FakeModelProvider:
    """Expose exactly one model for execution tests."""

    name = "test"
    description = "Execution-test model provider."

    def __init__(self, *, streaming: bool) -> None:
        self.streaming = streaming

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return None

    def default_api_key_env(self) -> str | None:
        return None

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        return (
            ModelInfo(
                ref=TEST_MODEL_REF,
                provider=self.name,
                name="scripted",
                model="scripted",
                selectors=(TEST_MODEL_REF, "scripted"),
                adapter=ScriptedModelAdapter.name,
                streaming=self.streaming,
            ),
        )

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


class RecordingRunTracer(RunTracer):
    """Collect the ordered events observed after durable projection."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def on_event(self, event: RunEvent) -> None:
        self.events.append(event)


class RecordingTool:
    """Return one fixed result and retain every tool invocation."""

    def __init__(
        self,
        name: str,
        *,
        output: Mapping[str, Any],
        description: str = "A deterministic execution-test tool.",
        gate: AsyncGate | None = None,
        error: Exception | None = None,
    ) -> None:
        if "__" not in name:
            raise ValueError("recording tool names must include their plugin prefix")
        self.name = name
        self.plugin_name = name.split("__", 1)[0]
        self.output = dict(output)
        self.description = description
        self.gate = gate
        self.error = error
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters={"type": "object"},
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        self.calls.append((dict(arguments), context))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return dict(self.output)


@dataclass(slots=True)
class ExecutionHarness:
    """Actual execution objects wired to deterministic model collaborators."""

    setup: AgentSetup
    state: AgentState
    store: RunStore
    ids: IdIssuer
    executor: RunExecutor
    threads: ThreadManager
    adapter: ScriptedModelAdapter

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        source: str,
        responses: Sequence[ScriptedResponse],
        tools: Mapping[str, AgentTool] | None = None,
        streaming: bool = False,
    ) -> ExecutionHarness:
        """Build one isolated execution runtime from authored source."""

        home = root / "agents" / "alice"
        runtime = home / ".runtime"
        program = Program.from_source(source)
        root_version = sha256(b"execution-test-root").digest()
        home_version = sha256(source.encode("utf-8")).digest()
        state = AgentState(
            version=agent_state_version(root_version, home_version),
            root_version=root_version,
            home_version=home_version,
            toolang_version="test",
            root_config={},
            home_config={},
            config={},
            program_source="agents/alice/agent.too",
            program=program,
            caps=(),
            loaded_at="2026-01-01T00:00:00Z",
        )
        provider = FakeModelProvider(streaming=streaming)
        adapter = ScriptedModelAdapter(responses)
        setup = AgentSetup(
            layout=AgentLayout.resident(root, "alice"),
            providers={provider.name: provider},
            adapters={adapter.name: adapter},
            models=provider.list_models(environ={}),
            tools=tools or {},
            envs={},
        )
        store = RunStore(runtime / "runs.db")
        ids = IdIssuer(runtime / "ids.json")
        return cls(
            setup=setup,
            state=state,
            store=store,
            ids=ids,
            executor=RunExecutor(store, ids),
            threads=ThreadManager(store, ids),
            adapter=adapter,
        )

    def run_spec(
        self,
        *,
        thread: str,
        runnable: str,
        primary: Percept = (),
        named: Mapping[str, object] | None = None,
        model: str | None = None,
        limits: RunLimits | None = None,
        ceilings: tuple[AgentCeiling, ...] = (),
    ) -> RunSpec:
        """Build a run spec while keeping scenario tests focused on behavior."""

        return RunSpec(
            setup=self.setup,
            state=self.state,
            thread=thread,
            bindings=RunBindings(model=model, runnable=runnable),
            limits=limits if limits is not None else self.setup.limits,
            ceilings=ceilings,
            primary=primary,
            named=named,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    async def close(self) -> None:
        """Stop owned tasks before closing the durable store."""

        await self.executor.shutdown()
        self.store.close()
