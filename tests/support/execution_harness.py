"""Readable test harness for complete execution scenarios."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Part, TextPart
from toolang.base.types.model import (
    ModelInfo,
    ModelRequest,
    ModelTarget,
    Provider,
    ResolvedProvider,
)
from toolang.base.types.policy import (
    AgentCeiling,
    RunBindings,
    RunDefaults,
    RunLimits,
)
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
from toolang.execution.runnables import (
    parse_runnable_ref,
    resolve_state_runnable,
)
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.lang import Program
from toolang.lang.input import resolve_runnable_input
from toolang.state.state import AgentState, agent_state_revision
from toolang.state.watcher import StateRefresh
from toolang.plugin.models.resolution import build_model_collection
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentEnvironment, AgentSetup

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


def _missing_state_revision(revision: str) -> AgentState:
    raise ValueError(f"state revision not found: {revision}")


class ScriptedModelAdapter:
    """Return deterministic model results while recording normalized calls."""

    name = "scripted"
    description = "Deterministic execution-test model adapter."
    default_api = "https://example.invalid/v1"

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


class FakeModels:
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

    def catalog_provider(self) -> Provider:
        return Provider(
            id=self.name,
            name="Test",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="https://example.invalid/v1",
            models={},
            resolved=ResolvedProvider(
                adapter=ScriptedModelAdapter.name,
                api="https://example.invalid/v1",
                env=(),
                ready=True,
            ),
        )


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
        parameters: Mapping[str, Any] | None = None,
        gate: AsyncGate | None = None,
        error: Exception | None = None,
    ) -> None:
        if "__" not in name:
            raise ValueError("recording tool names must include their plugin prefix")
        self.name = name
        self.plugin_name = name.split("__", 1)[0]
        self.toolset = self.plugin_name
        self.output = dict(output)
        self.description = description
        self.parameters = dict(parameters or {"type": "object"})
        self.gate = gate
        self.error = error
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=dict(self.parameters),
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
        state: AgentState | None = None,
        refresh_state: Callable[[], Awaitable[StateRefresh]] | None = None,
    ) -> ExecutionHarness:
        """Build one isolated execution runtime from authored source."""

        home = root / "agents" / "alice"
        runtime = home / ".runtime"
        program = Program.from_source(source)
        root_revision = sha256(b"execution-test-root").hexdigest()
        home_revision = sha256(source.encode("utf-8")).hexdigest()
        state = state or AgentState(
            revision=agent_state_revision(root_revision, home_revision),
            root_revision=root_revision,
            home_revision=home_revision,
            root_config={},
            home_config={},
            config={},
            caps={},
            modules={"agent": program},
            module_sources={"agent": "agent.too"},
            module_digests={"agent": home_revision},
            module_caps={"agent": ()},
        )
        provider = FakeModels(streaming=streaming)
        adapter = ScriptedModelAdapter(responses)
        layout = AgentLayout.resident(root, "alice")
        providers = {provider.name: provider.catalog_provider()}
        setup = AgentSetup(
            layout=layout,
            providers=providers,
            adapters={adapter.name: adapter},
            models=build_model_collection(
                providers=providers,
                models=provider.list_models(environ={}),
                envs={},
            ),
            tools=ToolCollection.from_tools(tools or {}),
            envs={},
            environment=AgentEnvironment(
                sandbox="host",
                system="test",
                release="test",
                machine="test",
                container=False,
                root=root,
                home=home,
                working_directory=home,
            ),
            defaults=RunDefaults(model=ModelRequest(TEST_MODEL_REF)),
        )
        store = RunStore(runtime / "runs.db")
        ids = IdIssuer(runtime / "ids.json")
        return cls(
            setup=setup,
            state=state,
            store=store,
            ids=ids,
            executor=RunExecutor(
                store,
                ids,
                setup=lambda: setup,
                state=lambda: state,
                load_state=lambda revision: (
                    state
                    if revision == state.revision
                    else _missing_state_revision(revision)
                ),
                refresh_state=refresh_state,
                include=lambda _setup: lambda reference: TextPart(reference),
            ),
            threads=ThreadManager(store, ids),
            adapter=adapter,
        )

    def run_spec(
        self,
        *,
        thread: str,
        runnable: str,
        primary: tuple[Part, ...] | None = None,
        named: Mapping[str, object] | None = None,
        model: str | None = None,
        limits: RunLimits | None = None,
        ceilings: tuple[AgentCeiling, ...] = (),
    ) -> RunSpec:
        """Build a run spec while keeping scenario tests focused on behavior."""

        runnable_name, runnable_kind = parse_runnable_ref(runnable)
        module, declaration = resolve_state_runnable(
            self.state,
            runnable_name,
            kind=runnable_kind,
        )
        return RunSpec(
            setup=self.setup,
            state=self.state,
            thread=thread,
            bindings=RunBindings(
                model=(
                    model
                    if model is not None
                    else self.setup.defaults.model.ref
                    if self.setup.defaults.model is not None
                    else None
                ),
                runnable=runnable,
            ),
            limits=limits if limits is not None else self.setup.limits,
            ceilings=ceilings,
            input=resolve_runnable_input(
                declaration,
                primary=primary,
                named=named,
                structs={
                    item.name: item for item in self.state.modules[module].structs
                },
            ),
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

        await self.executor.stop()
        self.store.close()
