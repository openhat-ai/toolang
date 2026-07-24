from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, cast

from pydantic import TypeAdapter

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, message_text
from toolang.base.types.model import ModelAlias, ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.ids import IdIssuer
from toolang.execution.events import RunEvent, RunTracer, StepBegin
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.executor.common import BoundRun
from toolang.execution.executor.persist import PersistSink
from toolang.execution.executor.prepare import prepare_agic
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.schemas import RunDetail
from toolang.execution.store import RunStore
from toolang.lang.ast import AgicDecl, Message as AstMessage, Parameter, Program, Span
from toolang.up.setup import AgentSetup


class _Provider:
    name = "test"
    description = None

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ) -> str | None:
        return None

    def default_api_key_env(self) -> str | None:
        return None

    def list_models(self, *, environ) -> tuple[ModelInfo, ...]:
        return ()

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


class _Adapter:
    name = "test"
    description = None

    def __init__(self) -> None:
        self.requests: list[ModelCall] = []

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        self.requests.append(request)
        return ModelCallResult(message=Message.assistant("done"))

    async def stream(self, target: ModelTarget, request: ModelCall, *, on_event):
        return await self.invoke(target, request)


class _Tool(AgentTool):
    name = "shell__execute"
    plugin_name = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Run a command.")

    async def invoke(self, arguments, context: ToolContext) -> dict[str, object]:
        return {}


class _History:
    def recent_conversation_messages(self, **kwargs) -> list[Message]:
        return [Message.user("previous")]


class _Tracer(RunTracer):
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def on_event(self, event: RunEvent) -> None:
        self.events.append(event)


def test_prepare_agic_builds_one_complete_model_input(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    provider = _Provider()
    adapter = _Adapter()
    tool = _Tool()
    setup = AgentSetup(
        home=home,
        name="alice",
        tools={tool.name: tool},
        model_providers={provider.name: provider},
        model_adapters={adapter.name: adapter},
        model_environ={},
        model_selectors=("default",),
        model_cache_dir=root / ".runtime" / "model-cache",
    )
    agic = AgicDecl(
        name="chat",
        input=Parameter(name="_", type_name="Text", span=Span(1)),
        params=(Parameter(name="focus", type_name="Text", span=Span(1)),),
        messages=(
            AstMessage(
                role="user",
                content="Answer: {{_}}; focus={{focus}}",
                explicit=True,
                span=Span(1),
            ),
        ),
        span=Span(1),
    )
    program = Program(agics=(agic,), span=Span(1))
    state = cast(
        Any,
        SimpleNamespace(
            program=program,
            program_source="agents/alice/agent.too",
            caps=(),
            fingerprint="state-1",
        ),
    )
    run = BoundRun(
        run_id="run_1",
        root_run_id="run_1",
        thread="term_1",
        input=Message.user("hello"),
        args={"focus": "events"},
        model=None,
        state=state,
        setup=setup,
        created_at="2026-01-01T00:00:00Z",
    )
    context = cast(
        Any,
        SimpleNamespace(
            setup=setup,
            home=home,
            store=_History(),
            model_providers=setup.model_providers,
            model_aliases={
                "default": ModelAlias(
                    name="default",
                    ref="test/model",
                    provider="test",
                    model="model",
                    adapter="test",
                )
            },
            default_models=("default",),
            model_environ=setup.model_environ,
            model_cache_dir=root / ".runtime" / "model-cache",
        ),
    )

    prepared = prepare_agic(context, run, agic)

    assert prepared.run is run
    assert prepared.agic is agic
    assert prepared.model.ref == "test/model"
    assert prepared.adapter is adapter
    assert tuple(prepared.tools) == ("shell__execute",)
    assert prepared.services == ()
    assert "You are the alice Toolang agent." in prepared.instructions
    assert "agent_name: alice" in prepared.prompt_context
    assert [message_text(message.parts) for message in prepared.messages] == [
        "previous",
        prepared.prompt_context + "\n\nAnswer: hello; focus=events",
    ]


def test_run_executor_uses_prepared_model_input_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    provider = _Provider()
    adapter = _Adapter()
    tool = _Tool()
    setup = AgentSetup(
        home=home,
        name="alice",
        tools={tool.name: tool},
        model_providers={provider.name: provider},
        model_adapters={adapter.name: adapter},
        model_environ={},
        model_selectors=("default",),
        model_cache_dir=root / ".runtime" / "model-cache",
    )
    agic = AgicDecl(
        name="chat",
        input=Parameter(name="_", type_name="Text", span=Span(1)),
        params=(Parameter(name="focus", type_name="Text", span=Span(1)),),
        messages=(
            AstMessage(
                role="user",
                content="Answer: {{_}}; focus={{focus}}",
                explicit=True,
                span=Span(1),
            ),
        ),
        span=Span(1),
    )
    state = cast(
        Any,
        SimpleNamespace(
            program=Program(agics=(agic,), span=Span(1)),
            program_source="agents/alice/agent.too",
            caps=(),
            root_config={},
            home_config={
                "models": {
                    "default": "default",
                    "aliases": {
                        "default": {
                            "ref": "test/model",
                            "provider": "test",
                            "model": "model",
                            "adapter": "test",
                            "endpoint": "https://models.example/v1",
                            "headers": {"X-Secret": "do-not-store"},
                        }
                    },
                }
            },
            fingerprint="state-1",
        ),
    )
    store = RunStore(home / ".runtime" / "runs.db")
    store.create_thread(thread_id="term_1")
    executor = RunExecutor(store, IdIssuer(home / ".runtime" / "ids.json"))
    tracer = _Tracer()
    try:
        async def execute() -> Any:
            return await executor.start(
                RunSpec(
                    setup=setup,
                    state=state,
                    thread="term_1",
                    runnable="chat",
                    input=Message.user("hello").parts,
                    args={"focus": "events"},
                ),
                tracer=tracer,
            )

        record = asyncio.run(execute())
        assert record.status == "finished"
        steps = store.list_steps(run_id=record.id)
        assert [step.kind for step in steps] == ["model"]
        assert len(adapter.requests) == 1
        assert message_text(adapter.requests[0].messages[-1].parts).endswith(
            "Answer: hello; focus=events"
        )
        assert store.rebuild_model_call(steps[0]) == adapter.requests[0]
        begin = next(event for event in tracer.events if isinstance(event, StepBegin))
        assert begin.given["call"] == adapter.requests[0].to_data()
        assert steps[0].given["call"] != begin.given["call"]
        assert steps[0].given["model"] == {
            "ref": "test/model",
            "provider": "test",
            "name": "model",
            "model": "model",
            "adapter": "test",
            "base_url": "https://models.example/v1",
            "scope": "remote",
            "tags": [],
            "options": {},
            "tools": True,
            "streaming": True,
        }
        assert "headers" not in steps[0].given["model"]
        assert "api_key" not in steps[0].given["model"]
        assert "adapter_request" not in steps[0].noted
        detail = ExecutionInspection(store).run_detail(record.id)
        assert detail is not None
        assert detail.steps[0].given["call"] == adapter.requests[0].to_data()
        payload = TypeAdapter(RunDetail).dump_python(detail, mode="json")
        serialized_call = cast(
            dict[str, Any],
            payload["steps"][0]["given"]["call"],
        )
        assert ModelCall.from_data(serialized_call) == adapter.requests[0]
        PersistSink(store).on_event(begin)
        connection = sqlite3.connect(store.db_path)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM model_texts"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM model_messages"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM model_toolsets"
            ).fetchone() == (1,)
        finally:
            connection.close()
    finally:
        asyncio.run(executor.shutdown())
        store.close()
