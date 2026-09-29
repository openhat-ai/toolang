"""Opt-in semantic checks for cumulative Step compaction with a real provider.

uv run pytest -s tests/integration/execution/test_compact_quality_live.py \
    --live-model 'deepseek/deepseek-v4-flash effort=low'

Cross-model execution (synthetic history, real compact and caller requests):
uv run pytest -s tests/integration/execution/test_compact_quality_live.py -k distinct_models \
    --live-model 'vercel/openai/gpt-6-luna-fast effort=low max_output=8192' \
    --live-compact-model 'deepseek/deepseek-v4-flash effort=low max_output=8192'
"""

import asyncio
from dataclasses import replace
import json
import time

import pytest

from toolang.base.types.message import Message, message_text
from toolang.base.errors import ModelResponseError
from toolang.base.types.model import env_names
from toolang.base.types.run import ModelCall
from toolang.common.layout import AgentLayout
from toolang.execution.compaction import Compaction, HistoryUnit
from toolang.execution.types import RunRef, StepRef
from toolang.plugin.models.query import resolve_model
from toolang.setup import SetupWatcher

pytestmark = pytest.mark.live_provider


FACTS = (
    "Project Harbor Finch, code HF42, client Kestrel Museum. Launch was provisionally 2030-04-12.",
    "Budget initially USD 18500. Keep all visitor data at the museum. No telemetry or cloud services.",
    "Decision owner Mara Chen, backup Luis Ortega. Use Python 3.11 and SQLite, one writer.",
    "Correction: launch is 2030-04-19; budget is USD 16200. The previous date and budget are obsolete.",
    "CSV importer implementation and tests completed at run_finch.4. Schema approval is still pending; do not infer approval.",
    "Deliver tar.gz. Escalation code FINCH7; preserve canary amber-kite-731 and checksum Q9:violet/27|NORTH exactly.",
)
EXPECTED = {
    "launch_date": "2030-04-19",
    "budget_usd": 16200,
    "owner": "Mara Chen",
    "cloud_allowed": False,
    "telemetry_allowed": False,
    "schema_approved": False,
    "importer_completed": True,
    "completed_step": "run_finch.4",
    "artifact": "tar.gz",
    "canary": "amber-kite-731",
    "checksum": "Q9:violet/27|NORTH",
    "coffee_order": None,
}


def test_live_cumulative_compaction_preserves_corrections_and_provenance(
    tmp_path, request
):
    selector = request.config.getoption("--live-model")
    if not selector:
        pytest.skip("pass --live-model")

    async def scenario():
        setup = await SetupWatcher(
            AgentLayout.resident(tmp_path, "compact-quality"),
            default_overrides={"model": selector},
        ).refresh()
        selected = setup.defaults.model
        assert selected is not None
        original = resolve_model(setup.models_effective(), selected.ref)
        route = original._toolang.route
        assert route.adapter is not None and route.env is not None
        adapter = setup.adapters()[route.adapter]
        environ = {k: setup.envs[k] for k in env_names(route.env) if k in setup.envs}

        async def invoke(target, call):
            for attempt in range(3):
                try:
                    return await asyncio.wait_for(
                        adapter.invoke(target, call, environ=environ), 180
                    )
                except ModelResponseError as error:
                    if error.kind != "transport_error" or attempt == 2:
                        raise
                    await asyncio.sleep(1)
            raise AssertionError("unreachable")

        # A small local window forces multiple batches without changing the route.
        model = replace(original, limit={"context": 16000, "output": 8192})
        root = RunRef("run_finch")
        units = tuple(
            HistoryUnit(
                root,
                "succeeded",
                (Message.user(fact + "\nIrrelevant repeated log: " + "ping " * 2600),),
                step_id=StepRef.from_local(str(root), (i,)),
            )
            for i, fact in enumerate(FACTS)
        )
        by_ref = {unit.ref: unit for unit in units}
        reducer = Compaction(
            tuple(by_ref),
            by_ref.__getitem__,
            model,
            size=768,
            max_output_tokens=8192,
            reasoning=selected.reasoning,
        )
        calls = 0
        while (call := reducer.next_call()) is not None:
            response = await invoke(model, call)
            reducer.accept(response)
            calls += 1
            print(f"Accepted summary batch {calls}", flush=True)
        assert calls >= 3
        result = await asyncio.wait_for(
            adapter.invoke(
                original,
                ModelCall(
                    instructions="Answer solely from the history summary. Later corrections supersede old facts. Return one JSON object, without Markdown. Unknown facts are null.",
                    messages=[
                        Message.user(reducer.summary),
                        Message.user(
                            "Return these keys: "
                            + ", ".join(EXPECTED)
                            + ". Use booleans for permissions and completion/approval, an integer for budget, YYYY-MM-DD for date, and exact strings for other known facts."
                        ),
                    ],
                    max_output_tokens=8192,
                    reasoning=selected.reasoning,
                ),
                environ=environ,
            ),
            180,
        )
        assert result.message is not None
        answer = json.loads(message_text(result.message.parts))
        assert answer == EXPECTED, {"answer": answer, "summary": reducer.summary}

    asyncio.run(scenario())


def test_live_distinct_models_compact_and_resume_the_caller(tmp_path, request):
    """Exercise ordinary preflight, batching, publication and the resumed model."""
    from toolang.base.model_settings import parse_model_body
    from toolang.base.types.message import TextPart
    from toolang.base.types.run import ModelCallResult
    from toolang.execution.inspection.history import RunHistory
    from toolang.execution.tokens import TokenCounter
    from toolang.execution.types import ThreadPrefix
    from toolang.plugin.models.budget import input_budget
    from tests.support.execution_harness import ExecutionHarness
    from tests.support.setup import replace_materialized_setup

    caller_selector = request.config.getoption("--live-model")
    compact_selector = request.config.getoption("--live-compact-model")
    if not caller_selector or not compact_selector:
        pytest.skip("pass --live-model and --live-compact-model")

    async def scenario():
        h = ExecutionHarness.create(
            tmp_path,
            source="""agic chat(_: Part[]) -> Text:
  context = none
  instruct = none
  user: {{_}}
""",
            responses=[
                ModelCallResult(
                    message=Message.assistant(
                        fact + "\nIrrelevant log: " + "ping " * 4000
                    )
                )
                for fact in FACTS
            ],
        )
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            for index in range(len(FACTS)):
                seeded = await h.executor.run(
                    h.run_spec(
                        thread=thread,
                        runnable="chat",
                        primary=(
                            TextPart(f"Record project update {index}: {FACTS[index]}"),
                        ),
                    )
                )
                assert seeded.status == "succeeded", seeded.error
            live = await SetupWatcher(
                h.setup.layout, default_overrides={"model": caller_selector}
            ).refresh()
            caller_request = live.defaults.model
            assert caller_request is not None
            compact_request = parse_model_body(compact_selector)
            caller = resolve_model(live.models_effective(), caller_request.ref)
            assert compact_request.identity is not None
            compact = resolve_model(live.models_effective(), compact_request.identity)
            assert caller.ref != compact.ref
            caller = replace(caller, limit={"context": 32000, "output": 8192})
            compact = replace(compact, limit={"context": 16000, "output": 8192})
            calls = []
            intervals = []
            callback_seconds = 0.0
            observer_seconds = 0.0

            class RecordingAdapter:
                def __init__(self, delegate):
                    self.delegate = delegate

                def record(self, model, call, result):
                    nonlocal observer_seconds
                    started = time.perf_counter()
                    assert result.usage is not None
                    capacity = input_budget(model.limit, call.max_output_tokens)
                    assert capacity is not None
                    estimated = TokenCounter(model.ref).base(call)
                    print(
                        json.dumps(
                            {
                                "model": model.ref,
                                "estimate": estimated,
                                "actual_input": result.usage.input_tokens,
                                "input_budget": capacity,
                            }
                        ),
                        flush=True,
                    )
                    assert result.usage.input_tokens <= capacity
                    calls.append((model.ref, call, result))
                    observer_seconds += time.perf_counter() - started
                    return result

                async def invoke(self, model, call, *, environ):
                    started = time.perf_counter()
                    result = await asyncio.wait_for(
                        self.delegate.invoke(model, call, environ=environ), 180
                    )
                    intervals.append((model.ref, started, time.perf_counter()))
                    return self.record(model, call, result)

                async def stream(self, model, call, *, environ, on_event):
                    async def observed_event(event):
                        nonlocal callback_seconds
                        started = time.perf_counter()
                        try:
                            await on_event(event)
                        finally:
                            callback_seconds += time.perf_counter() - started

                    started = time.perf_counter()
                    result = await asyncio.wait_for(
                        self.delegate.stream(
                            model, call, environ=environ, on_event=observed_event
                        ),
                        180,
                    )
                    intervals.append((model.ref, started, time.perf_counter()))
                    return self.record(model, call, result)

            h.setup = replace_materialized_setup(
                live,
                models=(caller, compact),
                adapters={
                    key: RecordingAdapter(adapter)
                    for key, adapter in live.adapters().items()
                },
                compact_model=compact_request,
            )
            h.setup = replace(
                h.setup,
                compact=replace(
                    h.setup.compact, trigger=16000, recent=1500, summary=768
                ),
            )
            question = (
                "Use both the generated history summary and the retained recent messages as project history. Latest corrections supersede previous values. Return one JSON object, without Markdown, with keys "
                + ", ".join(EXPECTED)
                + ". Booleans for permissions/completion/approval, integer for budget, YYYY-MM-DD for date, exact strings otherwise; unknown facts null."
            )
            started = time.perf_counter()
            run = await asyncio.wait_for(
                h.executor.run(
                    h.run_spec(
                        thread=thread, runnable="chat", primary=(TextPart(question),)
                    )
                ),
                420,
            )
            finished = time.perf_counter()
            assert run.status == "succeeded", (
                h.store.resolve_error(run.error) if run.error else None
            )
            assert intervals
            adapter_seconds = sum(end - start for _, start, end in intervals)
            gaps = (
                [intervals[0][1] - started]
                + [
                    following[1] - previous[2]
                    for previous, following in zip(intervals, intervals[1:])
                ]
                + [finished - intervals[-1][2]]
            )
            # Stream callbacks persist/render local events while the adapter is
            # active. Count them as runtime work, and exclude test-only recounts.
            timing = {
                "end_to_end_ms": round((finished - started) * 1000, 2),
                "adapter_ms": round(adapter_seconds * 1000, 2),
                "stream_callback_ms": round(callback_seconds * 1000, 2),
                "observer_ms": round(observer_seconds * 1000, 2),
                "non_model_ms": round(
                    (
                        finished
                        - started
                        - adapter_seconds
                        + callback_seconds
                        - observer_seconds
                    )
                    * 1000,
                    2,
                ),
                "before_first_ms": round(gaps[0] * 1000, 2),
                "max_between_ms": round(max(gaps[1:-1], default=0) * 1000, 2),
                "after_last_ms": round(gaps[-1] * 1000, 2),
                "calls": [
                    {"model": ref, "ms": round((end - start) * 1000, 2)}
                    for ref, start, end in intervals
                ],
            }
            (tmp_path / "cross-model-timing.json").write_text(
                json.dumps(timing, indent=2)
            )
            print(json.dumps({"timing": timing}), flush=True)
            output = RunHistory(h.store).get_compaction(thread)
            assert output is not None
            assert sum(ref == compact.ref for ref, _, _ in calls) >= 2
            assert calls[-1][0] == caller.ref
            assert any(
                output.result.summary in message_text(message.parts)
                for message in calls[-1][1].messages
            )
            final = h.store.run_output_text(run_id=run.id)
            assert json.loads(final) == EXPECTED, final
            print(
                f"Cross-model run {run.id}: {len(calls) - 1} compact calls, summary adopted, caller facts verified",
                flush=True,
            )

    asyncio.run(scenario())
