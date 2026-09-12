"""The compact command runs an ordinary script against local durable history."""

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from types import SimpleNamespace
from typing import cast

import pytest

from tests.support.execution_fixtures import project_run_end, project_run_start
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import compact
from toolang.execution.executor.compact import permit
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import RunControlPayload, StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.lang.input import CallInput, RunnableInput
from toolang.plugin.models.collections import ModelCollection
from toolang.setup import SetupWatcher


SOURCE = """agic chat(_: Part[]) -> Text:
  context: none
  user: {{_}}
"""


def reply(value):
    return ModelCallResult(message=Message.assistant(json.dumps(value)))


@pytest.fixture
def harness(tmp_path):
    h = ExecutionHarness.create(tmp_path, source=SOURCE, responses=[])
    (h.setup.layout.home / "agent.too").write_text(SOURCE, encoding="utf-8")
    h.setup = replace(
        h.setup,
        models=ModelCollection(
            tuple(
                replace(entry, target=replace(entry.target, structured_output=True))
                for entry in h.setup.models.entries
            )
        ),
    )
    for index in range(10):
        project_run_start(
            h.store,
            run_id=f"run_{index}",
            thread_id="term_a",
            origin="chat",
            input=Message.user(f"fact {index}"),
        )
        project_run_end(h.store, run_id=f"run_{index}")
    yield h
    asyncio.run(h.close())


def responses(h, *, begin=None, end="run_8", summary="Facts zero through seven."):
    h.adapter._responses.extend(
        [
            reply({"thread": "term_a", "begin": begin, "end": end, "summary": summary}),
        ]
    )


async def run(h, **kwargs):
    values = {"thread": "term_a", **kwargs}
    watcher = cast(SetupWatcher, SimpleNamespace(refresh=lambda: refresh(h)))
    return await compact._run(
        h.store,
        h.ids,
        watcher,
        RunnableInput(values),
        CallInput({k: str(v) for k, v in values.items()}),
        max_width=100,
    )


async def refresh(h):
    return h.setup


def prepare(h, **kwargs):
    return compact._prepare(
        h.store, h.setup, RunnableInput({"thread": "term_a", **kwargs}), CallInput()
    )


def horizon(history):
    output = history.get_compaction("term_a")
    assert output is not None
    return str(output.ref)


def test_cli_executes_eight_of_ten_and_next_run_adopts_it(harness, monkeypatch, capsys):
    h = harness
    responses(h)
    captured = {}

    class Watcher:
        def __init__(self, layout, **kwargs):
            assert layout == h.setup.layout
            captured.update(kwargs)

        async def refresh(self):
            return h.setup

    monkeypatch.setattr(compact, "SetupWatcher", Watcher)
    monkeypatch.setenv("TOOLANG_COMPACT_MODEL", "test/configured effort=low")
    result = cli.main(
        [
            "--root",
            str(h.setup.layout.root),
            "alice",
            "compact",
            "thread=term_a",
            "end=run_8",
            "--limit",
            "time=120",
        ]
    )
    assert result == 0
    captured_output = capsys.readouterr()
    result = json.loads(captured_output.out)
    assert result["output"] == {
        "thread": "term_a",
        "begin": None,
        "end": "run_8",
        "summary": "Facts zero through seven.",
    }
    assert result["horizon"] == f"{result['run']}/output"
    assert "∎" in captured_output.err and result["run"] in captured_output.err
    assert captured["sandbox"] == "host"
    assert captured["compact_override"].identity == "test/configured"
    assert captured["compact_override"].effort == "low"
    assert captured["limit_overrides"]
    control = h.store.get_run_control(run_id=result["run"], index=0)
    assert control is not None and isinstance(control.payload, RunControlPayload)
    assert control.payload.input == {"thread": "term_a", "end": "run_8", "bare": True}
    assert control.payload.authored_input == {"thread": "term_a", "end": "run_8"}

    async def next_run():
        h.adapter._responses.append(reply("remembered"))
        return await h.executor.run(
            h.run_spec(thread="term_a", runnable="chat", primary=(TextPart("recap"),))
        )

    record = asyncio.run(next_run())
    assert record.status == "succeeded", record.error
    control = h.store.get_run_control(run_id=record.id, index=0)
    assert control is not None and isinstance(control.payload, RunControlPayload)
    assert str(control.payload.horizon) == result["horizon"]
    history = RunHistory(h.store)
    steps = h.store.list_steps(run_id=record.id)
    model = next(s for s in steps if isinstance(s.given, StoredModelStepGiven))
    call = history.get_model_call(model.ref)
    assert "Facts zero through seven." in str(call.messages)
    with closing(RunStore(h.store.db_path, read_only=True)) as reopened:
        assert RunHistory(reopened).get_model_call(model.ref) == call
        assert horizon(RunHistory(reopened)) == result["horizon"]


@pytest.mark.parametrize(
    ("arguments", "expected_begin", "reuse"),
    [
        ({}, "run_5", True),
        ({"bare": True}, "run_5", False),
        ({"begin": "run_3"}, "run_3", False),
        ({"begin": "run_5"}, "run_5", True),
        ({"begin": "run_6"}, "run_6", False),
        ({"end": "run_3"}, None, False),
        ({"begin": "run_0"}, None, False),
    ],
)
def test_range_defaults_freeze_previous_reference(
    harness, arguments, expected_begin, reuse
):
    h = harness
    responses(h, end="run_5")
    previous = asyncio.run(run(h, end="run_5"))
    spec, prefix = prepare(h, **arguments)
    assert spec.input.get("begin") == expected_begin
    assert spec.input.get("previous") == (previous["horizon"] if reuse else None)
    assert spec.input["bare"] is not reuse
    assert spec.input["end"] == arguments.get("end", "run_9")
    assert prefix[-1] == spec.input["end"]


def test_incremental_and_bare_results_keep_last_usable_summary(harness):
    h = harness

    async def scenario():
        responses(h, end="run_5")
        first = await run(h, end="run_5")
        responses(h, begin="run_5", end="run_7")
        partial = await run(h, end="run_7", bare=True)
        assert partial["horizon"] is None
        assert horizon(RunHistory(h.store)) == first["horizon"]
        responses(h, end="run_9")
        latest = await run(h)
        assert latest["horizon"]
        assert horizon(RunHistory(h.store)) == latest["horizon"]
        control = h.store.get_run_control(run_id=latest["run"], index=0)
        assert control.payload.input["previous"] == first["horizon"]
        assert control.payload.input["begin"] == "run_5"
        with pytest.raises(ToolangError, match="nothing to compact"):
            await run(h)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("input", "output"),
    [
        ({"begin": "run_5"}, {"begin": None}),
        ({}, {"end": "run_7"}),
        ({}, {"summary": ""}),
    ],
)
def test_bad_output_does_not_hide_old_summary_or_rewrite_run_status(
    harness, input, output
):
    h = harness

    async def scenario():
        responses(h, end="run_3")
        first = await run(h, end="run_3")
        responses(h, **output)
        with pytest.raises(ToolangError, match="output must match"):
            await run(h, end="run_8", **input)
        history = RunHistory(h.store)
        assert (
            history.thread_view("compact_term_a", include_children=False)
            .roots[-1]
            .status
            == "succeeded"
        )
        assert horizon(history) == first["horizon"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "input",
    [
        {"thread": "compact_term_a"},
        {"begin": "run_missing"},
        {"end": "run_missing"},
        {"begin": "run_8", "end": "run_3"},
        {"begin": "run_8", "end": "run_8"},
    ],
)
def test_invalid_ranges_do_not_create_runs(harness, input):
    with pytest.raises(ToolangError):
        prepare(harness, **input)
    assert harness.store.get_thread(thread_id="compact_term_a") is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["end=run_8"],
        ["thread=term_a", "bare=maybe"],
        ["thread=term_a", "thread=term_a"],
        ["thread=term_a", "previous=run_0/output"],
    ],
)
def test_cli_validates_script_inputs_before_setup(
    harness, arguments, monkeypatch, capsys
):
    def unexpected_setup(*args, **kwargs):
        pytest.fail("invalid script input reached setup")

    monkeypatch.setattr(compact, "SetupWatcher", unexpected_setup)
    assert (
        cli.main(
            ["--root", str(harness.setup.layout.root), "alice", "compact", *arguments]
        )
        != 0
    )
    assert "Traceback" not in capsys.readouterr().err
    assert harness.store.get_thread(thread_id="compact_term_a") is None


@pytest.mark.parametrize("change", ["append", "rewind"])
def test_frozen_range_survives_append_but_rejects_rewind(harness, change):
    h = harness

    async def scenario():
        gate = AsyncGate()
        responses(h)
        h.adapter._responses[0] = ScriptedModelTurn(h.adapter._responses[0], gate=gate)
        task = asyncio.create_task(run(h, end="run_8"))
        await asyncio.wait_for(gate.wait_until_entered(), 2)
        if change == "append":
            # A different executor may still be producing the target's latest root.
            active = project_run_start(
                h.store,
                run_id="run_active",
                thread_id="term_a",
                origin="chat",
                input=Message.user("new request"),
            )
            controls = h.store.list_run_controls(run_id=active.id)
        else:
            h.threads.rewind(thread_id="term_a", run_id="run_8")
        gate.release()
        if change == "rewind":
            with pytest.raises(ToolangError, match="range changed"):
                await task
            assert RunHistory(h.store).get_compaction("term_a") is None
        else:
            result = await task
            assert result["horizon"]
            assert h.store.get_run(run_id=active.id) == active
            assert h.store.list_run_controls(run_id=active.id) == controls

    asyncio.run(scenario())


@pytest.mark.parametrize("after_admission", [False, True])
def test_cancel_waiter_or_script_releases_permit(harness, after_admission):
    h = harness

    async def scenario():
        path = h.store.db_path.with_name(f"{h.store.db_path.name}.term_a.compact.lock")
        if after_admission:
            gate = AsyncGate()
            h.adapter._responses.append(ScriptedModelTurn(reply({}), gate=gate))
            task = asyncio.create_task(run(h))
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            roots = (
                RunHistory(h.store)
                .thread_view("compact_term_a", include_children=False)
                .roots
            )
            assert len(roots) == 1 and roots[0].status == "canceled"
        else:
            async with permit(path):
                task = asyncio.create_task(run(h))
                await asyncio.sleep(0.1)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert h.store.get_thread(thread_id="compact_term_a") is None
        async with asyncio.timeout(2), permit(path):
            pass

    asyncio.run(scenario())
