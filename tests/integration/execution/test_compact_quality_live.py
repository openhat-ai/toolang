"""Opt-in semantic checks for cumulative Step compaction with a real provider.

uv run pytest -s tests/integration/execution/test_compact_quality_live.py \
    --live-model 'deepseek/deepseek-v4-flash effort=low'
"""

import asyncio
from dataclasses import replace
import json

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
