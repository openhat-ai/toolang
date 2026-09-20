"""Opt-in semantic regression: compact eight real turns and query retained facts.

uv run pytest -s tests/integration/cli/test_compact_live.py \
    --live-model 'deepseek/deepseek-v4-flash effort=low'
"""

import asyncio
from contextlib import closing
import json
from typing import cast

import pytest

from toolang.base.model_settings import parse_model_body
from toolang.base.types.policy import RunBindings
from toolang.cli.toolang.commands import compact
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import RunControlPayload, StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import CallInput, RunnableInput
from toolang.setup import SetupWatcher
from toolang.state.builtin import prepare_builtin_state

pytestmark = pytest.mark.live_provider

_SOURCE = """agic note(_: Text) -> Text:
  tools = none
  context: none
  instruct: Acknowledge the fictional project update in one short sentence.
  user: {{_}}

agic check(_: Text) -> Json:
  tools = none
  context: none
  instruct: Use the supplied history only. Later corrections supersede old facts. Never invent missing facts.
  user: {{_}}
"""

_FACTS = (
    "Project name: Harbor Finch. Project code: HF42. Client: Kestrel Museum.",
    "Provisional launch: 2030-04-12 at 09:30 UTC.",
    "Initial budget: USD 18500, including USD 2500 reserve.",
    "Use Python 3.11 and SQLite with a single writer, offline with no cloud dependency.",
    "Decision owner Mara Chen; backup Luis Ortega; escalation label FINCH7.",
    "No telemetry. Visitor data must stay at the museum. Audit exports must be CSV.",
    "Correction: launch 2030-04-19 at 09:30 UTC; budget USD 16200; reserve USD 2000. Earlier values are obsolete.",
    "CSV importer complete; visitor-data schema approval pending; deliver tar.gz, not Docker.",
    "Recent canary: amber-kite-731. Preserve it literally.",
    "Recent checksum: Q9:violet/27|NORTH. Preserve punctuation and case.",
)

_EXPECTED = {
    "project": "Harbor Finch",
    "code": "HF42",
    "client": "Kestrel Museum",
    "launch_date": "2030-04-19",
    "budget_usd": 16200,
    "reserve_usd": 2000,
    "writer_limit": 1,
    "cloud_allowed": False,
    "telemetry_allowed": False,
    "data_may_leave_museum": False,
    "owner": "Mara Chen",
    "backup": "Luis Ortega",
    "escalation": "FINCH7",
    "canary": "amber-kite-731",
    "checksum": "Q9:violet/27|NORTH",
    "coffee_order": None,
}


@pytest.mark.parametrize("algorithm", ["DEFAULT", "file"])
def test_live_compact_preserves_constraints_across_unrelated_updates(
    tmp_path, request, algorithm
):
    model = request.config.getoption("--live-model")
    if not model:
        pytest.skip("pass --live-model with a concrete tool-capable model")

    if algorithm == "file":
        from toolang.execution.assembly import prompts

        source_path = tmp_path / "custom-compact.too"
        source_path.write_text(prompts.load("defaults/compact.too"), encoding="utf-8")
        algorithm = str(source_path)

    async def scenario():
        layout = AgentLayout.resident(tmp_path, "probe")
        watcher = SetupWatcher(
            layout,
            default_overrides={"model": model},
            compact_override=parse_model_body(model),
            limit_overrides={"time": 240, "tokens": 500000, "cost": 0.5},
        )
        setup = await watcher.refresh()
        selected = setup.defaults.model
        assert selected is not None
        state = prepare_builtin_state(_SOURCE)
        with closing(RunStore(layout.run_store)) as store:
            ids = IdIssuer(layout.id_state)
            executor = RunExecutor(store, ids)
            thread = ThreadManager(store, ids).create(prefix=ThreadPrefix.TERM)

            async def run(name, text):
                print(f"\nUSER > {text}", flush=True)
                record = await executor.run(
                    RunSpec(
                        setup=setup,
                        state=state,
                        thread=thread,
                        bindings=RunBindings(
                            runnable=f"agic:{name}", model=selected.ref
                        ),
                        model_request=selected,
                        limits=setup.limits,
                        input=RunnableInput({"_": text}),
                    )
                )
                assert record.status == "succeeded", record.error
                return record

            try:
                roots = [await run("note", text) for text in _FACTS]
                inputs = {"thread": thread, "before": roots[8].id}
                result = await compact._run(
                    store,
                    ids,
                    watcher,
                    RunnableInput(inputs),
                    CallInput(inputs),
                    max_width=100,
                    algorithm=algorithm,
                )
                assert result["horizon"]
                output = cast(dict[str, object], result["output"])
                summary = output["summary"]
                assert isinstance(summary, str)
                assert "amber-kite-731" not in summary
                assert "Q9:violet/27|NORTH" not in summary
                assert not any(root.id in summary for root in roots)
                checked = await run(
                    "check",
                    (
                        "Return a JSON object for these facts: "
                        + ", ".join(_EXPECTED)
                        + ". Use numbers for amounts and writer_limit, booleans for permissions, "
                        "launch_date as YYYY-MM-DD (date only), and null for unknown facts."
                    ),
                )
                answer = json.loads(store.run_output_text(run_id=checked.id))
                # A descriptive "Project" label is not a change to the name.
                if isinstance(answer.get("project"), str):
                    answer["project"] = answer["project"].removeprefix("Project ")
                assert answer == _EXPECTED, {"answer": answer, "summary": summary}
                control = store.get_run_control(run_id=checked.id, index=0)
                assert control is not None and isinstance(
                    control.payload, RunControlPayload
                )
                assert str(control.payload.horizon) == result["horizon"]
                step = next(
                    s
                    for s in store.list_steps(run_id=checked.id)
                    if isinstance(s.given, StoredModelStepGiven)
                )
                call = RunHistory(store).get_model_call(step.ref)
                with closing(RunStore(layout.run_store, read_only=True)) as reopened:
                    assert RunHistory(reopened).get_model_call(step.ref) == call
                incremental = await compact._run(
                    store,
                    ids,
                    watcher,
                    RunnableInput({"thread": thread, "before": roots[9].id}),
                    CallInput({"thread": thread, "before": roots[9].id}),
                    max_width=100,
                    algorithm=algorithm,
                )
                merged = cast(dict[str, object], incremental["output"])
                assert merged["begin"] == output["begin"]
                assert merged["end"] == roots[9].id
                assert "HF42" in str(merged["summary"])
                assert "amber-kite-731" in str(merged["summary"])
                assert "Q9:violet/27|NORTH" not in str(merged["summary"])
                producer = store.get_run_control(
                    run_id=cast(str, incremental["run"]), index=0
                )
                assert producer is not None and isinstance(
                    producer.payload, RunControlPayload
                )
                assert producer.payload.input["begin"] == roots[8].id
                assert producer.payload.input["summary"] == summary
            finally:
                await executor.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["auto", "FORGET"])
def test_live_compaction_owns_terminal_replies_by_root(tmp_path, request, mode):
    from dataclasses import replace

    from toolang.base.types.message import Message
    from toolang.execution.records import CompactControlPayload
    from toolang.plugin.models.collections import ModelCollection

    model = request.config.getoption("--live-model")
    if not model:
        pytest.skip("pass --live-model with a concrete tool-capable model")

    async def scenario():
        layout = AgentLayout.resident(tmp_path, "boundary")
        watcher = SetupWatcher(
            layout,
            default_overrides={"model": model},
            compact_override=replace(parse_model_body(model), max_output=4096),
            limit_overrides={"time": 240, "tokens": 500000, "cost": 0.5},
        )
        setup = await watcher.refresh()
        selected = setup.defaults.model
        assert selected is not None
        catalog_model = setup.models.resolve(selected.ref)
        maximum = catalog_model.limit.get("output", 0)
        if maximum < 32000:
            pytest.skip("live boundary probe needs a large inclusive output allowance")
        state = prepare_builtin_state("""agic note(_: Text) -> Text:
  tools = none
  context: none
  instruct: Reply with exactly the acknowledgment requested in the current message.
  {{_}}

agic check(_: Text) -> Json:
  tools = none
  context: none
  instruct: Use the supplied history. Return null for unknown values.
  {{_}}
""")
        with closing(RunStore(layout.run_store)) as store:
            ids = IdIssuer(layout.id_state)
            executor = RunExecutor(store, ids)
            thread = ThreadManager(store, ids).create(prefix=ThreadPrefix.TERM)

            async def run(name, text):
                record = await executor.run(
                    RunSpec(
                        setup=setup,
                        state=state,
                        thread=thread,
                        bindings=RunBindings(
                            runnable=f"agic:{name}", model=selected.ref
                        ),
                        model_request=selected,
                        limits=setup.limits,
                        input=RunnableInput({"_": text}),
                    )
                )
                assert record.status == "succeeded", (
                    store.resolve_error(record.error) if record.error else record.status
                )
                return record

            try:
                old = await run(
                    "note",
                    "Project code is HF42. The following routine logs are irrelevant. "
                    + "Routine archived status; no new decisions. " * 3000
                    + " Reply exactly: COVERED_TERMINAL_731.",
                )
                retained = await run(
                    "note",
                    "Recent marker is RED9. Reply exactly: RETAINED_TERMINAL_927.",
                )
                old_reply = store.run_output_text(run_id=old.id)
                retained_reply = store.run_output_text(run_id=retained.id)
                assert "COVERED_TERMINAL_731" in old_reply
                assert "RETAINED_TERMINAL_927" in retained_reply
                if mode == "FORGET":
                    inputs = {"thread": thread, "before": retained.id}
                    await compact._run(
                        store,
                        ids,
                        watcher,
                        RunnableInput(inputs),
                        CallInput(inputs),
                        max_width=100,
                        algorithm="FORGET",
                    )
                else:
                    # Reserve the normal model's output, leaving about 16k input.
                    # The compactor reserves only 4096 and can read the full range.
                    context = (maximum + 16000) * 100 // 95
                    assert context < catalog_model.limit["context"]
                    setup = replace(
                        setup,
                        models=ModelCollection(
                            tuple(
                                replace(m, limit={**m.limit, "context": context})
                                if m.ref == selected.ref
                                else m
                                for m in setup.models.entries
                            )
                        ),
                    )
                current = await run(
                    "check", "Return JSON with project_code and recent_marker."
                )
                answer = json.loads(store.run_output_text(run_id=current.id))
                assert answer == {
                    "project_code": "HF42" if mode == "auto" else None,
                    "recent_marker": "RED9",
                }
                history = RunHistory(store)
                result = history.get_compaction(thread)
                assert result is not None and result.result.end == retained.id
                models = [
                    s
                    for s in store.list_steps(run_id=current.id)
                    if isinstance(s.given, StoredModelStepGiven)
                ]
                assert len(models) == 1
                call = history.get_model_call(models[0].ref)
                assert len(call.messages) == 4
                assert call.messages[0] == Message.user(result.result.summary)
                assert Message.assistant(old_reply) not in call.messages
                assert call.messages[2] == Message.assistant(retained_reply)
                controls = [
                    c
                    for c in store.list_run_controls(run_id=current.id)
                    if isinstance(c.payload, CompactControlPayload)
                ]
                assert len(controls) == (1 if mode == "auto" else 0)
                if controls:
                    assert controls[0].ref in models[0].preceded_by
                with closing(RunStore(layout.run_store, read_only=True)) as reopened:
                    assert RunHistory(reopened).get_model_call(models[0].ref) == call
            finally:
                await executor.stop()

    asyncio.run(scenario())
