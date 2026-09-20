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
from toolang.execution.assembly import prompts
from toolang.execution.compaction import permit
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
                replace(model, structured_output=True)
                for model in h.setup.models.entries
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
            ModelCallResult(message=Message.assistant(summary)),
        ]
    )


async def run(h, *, algorithm="DEFAULT", **kwargs):
    values = {"thread": "term_a", **kwargs}
    watcher = cast(SetupWatcher, SimpleNamespace(refresh=lambda: refresh(h)))
    return await compact._run(
        h.store,
        h.ids,
        watcher,
        RunnableInput(values),
        CallInput({k: str(v) for k, v in values.items()}),
        max_width=100,
        algorithm=algorithm,
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
            "before=run_8",
            "--limit",
            "time=120",
        ]
    )
    assert result == 0
    captured_output = capsys.readouterr()
    result = json.loads(captured_output.out)
    assert result["output"] == {
        "thread": "term_a",
        "begin": "run_0",
        "end": "run_8",
        "summary": "Facts zero through seven.",
    }
    assert result["horizon"] == result["run"]
    assert "∎" in captured_output.err
    assert captured["sandbox"] == "host"
    assert captured["compact_override"].identity == "test/configured"
    assert captured["compact_override"].effort == "low"
    assert captured["limit_overrides"]
    control = h.store.get_run_control(run_id=result["run"], index=0)
    assert control is not None and isinstance(control.payload, RunControlPayload)
    assert dict(control.payload.input) == {
        "thread": "term_a",
        "begin": "run_0",
        "start": "run_0",
        "end": "run_8",
        "summary": "",
    }
    assert control.payload.authored_input == {"thread": "term_a", "before": "run_8"}
    assert result["run"] in captured_output.err
    producer = RunHistory(h.store).get_output(result["run"])
    assert producer is not None
    assert producer.local.value == "Facts zero through seven."

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
        ({"before": "run_3"}, "run_0", False),
    ],
)
def test_range_defaults_freeze_previous_reference(
    harness, arguments, expected_begin, reuse
):
    h = harness
    responses(h, end="run_5")
    previous = asyncio.run(run(h, before="run_5"))
    spec, prefix = prepare(h, **arguments)
    assert spec.input.get("begin") == expected_begin
    assert spec.input["summary"] == (previous["output"]["summary"] if reuse else "")
    assert spec.input["end"] == arguments.get("before", "run_9")
    assert prefix[-1] == spec.input["end"]


def test_incremental_results_keep_summary(harness):
    h = harness

    async def scenario():
        responses(h, end="run_5")
        first = await run(h, before="run_5")
        responses(h, end="run_9")
        latest = await run(h)
        assert horizon(RunHistory(h.store)) == latest["horizon"]
        control = h.store.get_run_control(run_id=latest["run"], index=0)
        assert control.payload.input["summary"] == first["output"]["summary"]
        assert control.payload.input["begin"] == "run_5"
        with pytest.raises(ToolangError, match="nothing to compact"):
            await run(h)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "input,output", [({}, {"summary": ""}), ({}, {"summary": " \n"})]
)
def test_bad_output_does_not_hide_old_summary_or_rewrite_run_status(
    harness, input, output
):
    h = harness

    async def scenario():
        responses(h, end="run_3")
        first = await run(h, before="run_3")
        responses(h, **output)
        with pytest.raises(ToolangError, match="invalid summary"):
            await run(h, before="run_8", **input)
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
        {"before": "run_missing"},
        {"before": "run_0"},
        {"bare": True},
    ],
)
def test_invalid_ranges_do_not_create_runs(harness, input):
    with pytest.raises(ToolangError):
        prepare(harness, **input)
    assert harness.store.get_thread(thread_id="compact_term_a") is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["before=run_8"],
        ["thread=term_a", "bare=true"],
        ["thread=term_a", "begin=run_0"],
        ["thread=term_a", "end=run_8"],
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
        task = asyncio.create_task(run(h, before="run_8"))
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
                with pytest.raises(ToolangError, match="already running"):
                    await run(h)
                assert h.store.get_thread(thread_id="compact_term_a") is None
        async with asyncio.timeout(2), permit(path):
            pass

    asyncio.run(scenario())


def test_run_output_is_not_a_published_compaction(harness):
    from tests.support.execution_fixtures import accept_run
    from toolang.execution.types import Local, Output, RunRef, ThreadRef

    h = harness
    responses(h, end="run_3")
    first = asyncio.run(run(h, before="run_3"))
    request = {"thread": "term_a", "begin": "run_0", "end": "run_8"}
    output = {**request, "summary": "Unpublished complete result"}
    accept_run(
        h.store,
        run_id="run_forged",
        parent=None,
        thread="compact_term_a",
        input=RunnableInput(request),
        context={},
        request_id=None,
        created_at="2026-09-20T00:00:00Z",
    )
    project_run_end(h.store, run_id="run_forged", output=Output(Local(output), None))
    history = RunHistory(h.store)
    assert horizon(history) == first["horizon"]
    with pytest.raises((ValueError, KeyError)):
        history.read_compaction(
            RunRef("run_forged"),
            ThreadRef("term_a"),
            tuple(RunRef(f"run_{i}") for i in range(10)),
        )
    with closing(RunStore(h.store.db_path, read_only=True)) as reopened:
        assert horizon(RunHistory(reopened)) == first["horizon"]


def test_active_exclusive_end_can_retain_a_later_terminal_root(harness):
    h = harness
    project_run_start(
        h.store,
        run_id="run_active",
        thread_id="term_a",
        origin="chat",
        input=Message.user("still running"),
    )
    project_run_start(
        h.store,
        run_id="run_tail",
        thread_id="term_a",
        origin="chat",
        input=Message.user("finished later"),
    )
    project_run_end(h.store, run_id="run_tail")
    responses(h, end="run_active")
    result = asyncio.run(run(h, before="run_active"))
    assert result["horizon"] == horizon(RunHistory(h.store))
    assert result["output"]["end"] == "run_active"
    assert h.store.get_run(run_id="run_active").status == "running"


@pytest.mark.parametrize("algorithm", ["DEFAULT", "file"])
def test_algorithm_executes_selected_source(harness, tmp_path, algorithm):
    h = harness
    if algorithm == "file":
        path = tmp_path / "custom.too"
        source = prompts.load("defaults/compact.too").replace(
            "Read the Runs and Steps",
            "CUSTOM ALGORITHM: Read the Runs and Steps",
        )
        path.write_text(source)
        algorithm = str(path)
    responses(h)
    result = asyncio.run(run(h, algorithm=algorithm, before="run_8"))
    assert result["horizon"] == horizon(RunHistory(h.store))
    request = h.adapter.invocations[-1].call
    assert request.output_schema is None
    assert ("CUSTOM ALGORITHM" in str(request.messages)) == (algorithm != "DEFAULT")


def test_forget_without_models_replaces_summary_and_survives_restart(harness):
    from toolang.base.types.policy import RunDefaults
    from tests.support.execution_assertions import assert_replayed
    from tests.support.execution_harness import RecordingRunTracer

    h = harness
    responses(h)
    first = asyncio.run(run(h, before="run_8"))
    calls = len(h.adapter.invocations)
    setup = h.setup
    h.setup = replace(
        setup, models=ModelCollection(()), defaults=RunDefaults(), compact_model=None
    )
    forgotten = asyncio.run(run(h, algorithm="FORGET", before="run_8"))
    assert len(h.adapter.invocations) == calls
    assert forgotten["output"] == {
        "thread": "term_a",
        "begin": "run_0",
        "end": "run_8",
        "summary": "Earlier history was intentionally forgotten.",
    }
    assert forgotten["run"] == forgotten["horizon"]
    assert len(RunHistory(h.store).thread_view("compact_term_a").roots) == 2
    assert forgotten["horizon"] != first["horizon"]
    with closing(RunStore(h.store.db_path, read_only=True)) as reopened:
        assert horizon(RunHistory(reopened)) == forgotten["horizon"]
        assert (
            len(
                RunHistory(reopened).thread_view("term_a", include_children=False).roots
            )
            == 10
        )
    h.setup = setup
    tracer = RecordingRunTracer()

    async def scenario():
        h.adapter._responses.append(reply("done"))
        record = await h.executor.run(
            h.run_spec(thread="term_a", runnable="chat", primary=(TextPart("recap"),)),
            tracer=tracer,
        )
        assert record.status == "succeeded", record.error
        request = h.adapter.invocations[-1].call
        text = str(request.messages)
        assert "intentionally forgotten" in text
        assert "Facts zero through seven" not in text
        assert "fact 0" not in text and "fact 7" not in text
        assert "fact 8" in text and "fact 9" in text
        responses(h, end="run_9", summary="Retained recent facts.")
        incremental = await run(h, before="run_9")
        control = h.store.get_run_control(run_id=incremental["run"], index=0)
        assert control.payload.input["begin"] == "run_8"
        assert control.payload.input["summary"] == forgotten["output"]["summary"]

    asyncio.run(scenario())
    assert_replayed(h.store.db_path, tracer.events)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--algorithm", "FORGET", "thread=term_a"],
        [
            "--algorithm",
            "FORGET",
            "--model",
            "test/model",
            "thread=term_a",
            "before=run_8",
        ],
        ["--algorithm", "missing.too", "thread=term_a"],
    ],
)
def test_invalid_algorithm_options_fail_before_setup(
    harness, arguments, monkeypatch, capsys
):
    monkeypatch.setattr(
        compact, "SetupWatcher", lambda *a, **kw: pytest.fail("reached setup")
    )
    assert (
        cli.main(
            ["--root", str(harness.setup.layout.root), "alice", "compact", *arguments]
        )
        != 0
    )
    assert "Traceback" not in capsys.readouterr().err
    assert harness.store.get_thread(thread_id="compact_term_a") is None


@pytest.mark.parametrize(
    "source",
    [
        "agic other() -> Text:\n  user: hello\n",
        "agic compact(_: Text, thread: Text, summary: Text, start: Text, begin: Text, end: Text) -> Text:\n  Return text.\n",
        "agic compact(thread: Text) -> Text:\n  user: {{thread}}\n",
        "agic compact(thread: Text, summary: Text, start: Text, begin: Text, end: Text) -> Json:\n  Return an object.\n",
    ],
)
def test_external_signature_rejected_before_creating_run(harness, tmp_path, source):
    path = tmp_path / "invalid.too"
    path.write_text(source)
    with pytest.raises(ToolangError, match="requires agic compact"):
        asyncio.run(run(harness, algorithm=str(path)))
    assert harness.store.get_thread(thread_id="compact_term_a") is None


def test_compact_help_exposes_only_framework_inputs(harness, capsys):
    assert (
        cli.main(
            ["--root", str(harness.setup.layout.root), "alice", "compact", "--help"]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "before" in text and "--algorithm" in text
    assert "begin" not in text and "bare" not in text and "previous" not in text


def test_forget_cli_uses_real_setup_without_provider_calls(
    harness, capsys, monkeypatch
):
    monkeypatch.setenv("TOOLANG_COMPACT_MODEL", "not-a-valid-config effort=unknown")
    h = harness
    assert (
        cli.main(
            [
                "--root",
                str(h.setup.layout.root),
                "alice",
                "compact",
                "--algorithm",
                "FORGET",
                "thread=term_a",
                "before=run_8",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["output"]["end"] == "run_8"
    assert result["output"]["summary"] == "Earlier history was intentionally forgotten."
    assert not h.adapter.invocations
    assert horizon(RunHistory(h.store)) == result["horizon"]


def test_custom_algorithm_invalid_result_does_not_publish_horizon(harness, tmp_path):
    path = tmp_path / "custom.too"
    path.write_text(prompts.load("defaults/compact.too"))
    responses(harness, summary="")
    with pytest.raises(ToolangError, match="invalid summary"):
        asyncio.run(run(harness, algorithm=str(path), before="run_8"))
    assert RunHistory(harness.store).get_compaction("term_a") is None


def test_forget_rejects_active_covered_root(harness):
    h = harness
    project_run_start(
        h.store,
        run_id="run_active",
        thread_id="term_a",
        origin="chat",
        input=Message.user("active"),
    )
    project_run_start(
        h.store,
        run_id="run_tail",
        thread_id="term_a",
        origin="chat",
        input=Message.user("tail"),
    )
    project_run_end(h.store, run_id="run_tail")
    with pytest.raises(ToolangError, match="exclude active"):
        asyncio.run(run(h, algorithm="FORGET", before="run_tail"))
    assert h.store.get_thread(thread_id="compact_term_a") is None


@pytest.mark.parametrize("existing_summary", [False, True])
def test_waiting_compact_rejects_a_summary_replaced_by_forget(
    harness, monkeypatch, existing_summary
):
    from contextlib import asynccontextmanager

    h = harness

    async def scenario():
        if existing_summary:
            responses(h, end="run_5", summary="Old private detail.")
            await run(h, before="run_5")
        entered = asyncio.Event()
        release = asyncio.Event()
        original = compact.permit
        first_wait = True

        @asynccontextmanager
        async def delayed(path, *, wait=True):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                entered.set()
                await release.wait()
            async with original(path, wait=wait):
                yield

        monkeypatch.setattr(compact, "permit", delayed)
        responses(h, end="run_9", summary="Old private detail plus recent facts.")
        pending = asyncio.create_task(run(h, before="run_9"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            forgotten = await run(h, algorithm="FORGET", before="run_8")
        finally:
            release.set()
        calls = len(h.adapter.invocations)
        runs = len(RunHistory(h.store).thread_view("compact_term_a").roots)
        with pytest.raises(ToolangError, match="summary changed"):
            await pending
        assert len(h.adapter.invocations) == calls
        assert len(RunHistory(h.store).thread_view("compact_term_a").roots) == runs
        assert horizon(RunHistory(h.store)) == forgotten["horizon"]
        # A fresh request resolves the new marker and starts after forgotten roots.
        spec, _ = prepare(h, before="run_9")
        assert spec.input["summary"] == forgotten["output"]["summary"]
        assert spec.input["begin"] == "run_8"

    asyncio.run(scenario())


def test_text_algorithm_does_not_require_native_structured_output(harness):
    h = harness
    h.setup = replace(
        h.setup,
        models=ModelCollection(
            tuple(
                replace(model, structured_output=False)
                for model in h.setup.models.entries
            )
        ),
    )
    responses(h)
    result = asyncio.run(run(h, before="run_8"))
    assert result["output"]["summary"] == "Facts zero through seven."
    assert all(call.call.output_schema is None for call in h.adapter.invocations)


def test_unpublished_text_producer_cannot_replace_a_durable_result(
    harness, monkeypatch
):
    h = harness
    responses(h, end="run_5", summary="Existing summary.")
    first = asyncio.run(run(h, before="run_5"))

    def fail_publication(*args, **kwargs):
        raise ToolangError("result publication failed")

    monkeypatch.setattr(h.store, "publish_compaction", fail_publication)
    responses(h, summary="Unpublished new summary.")
    with pytest.raises(ToolangError, match="publication failed"):
        asyncio.run(run(h, before="run_8"))
    with closing(RunStore(h.store.db_path, read_only=True)) as reopened:
        history = RunHistory(reopened)
        assert horizon(history) == first["horizon"]
        producer = history.thread_view("compact_term_a").roots[-1]
        output = history.get_output(producer.id)
        assert producer.status == "succeeded"
        assert output is not None and output.local.value == "Unpublished new summary."


def test_first_forget_publishes_one_model_free_summary_run(harness):
    result = asyncio.run(run(harness, algorithm="FORGET", before="run_8"))
    assert result["run"] == result["horizon"]
    assert not harness.adapter.invocations
    history = RunHistory(harness.store)
    roots = history.thread_view("compact_term_a").roots
    assert len(roots) == 1 and roots[0].id == result["run"]
    output = history.get_output(result["run"])
    assert output is not None and output.local.value == result["output"]["summary"]
    assert str(harness.store.get_thread(thread_id="term_a").horizon) == result["run"]
    assert all(
        c.kind != "compact"
        for i in range(10)
        for c in harness.store.list_run_controls(run_id=f"run_{i}")
    )


@pytest.mark.parametrize(
    "bounds",
    [
        ("run_8", "run_3"),
        ("run_missing", "run_8"),
        ("run_0", "run_missing"),
        ("run_3", "run_8"),
    ],
)
def test_invalid_publication_is_atomic(harness, bounds):
    from tests.support.execution_fixtures import project_compaction
    from toolang.execution.types import RunRef

    first = asyncio.run(run(harness, algorithm="FORGET", before="run_3"))
    ref = project_compaction(
        harness.store,
        thread="term_a",
        begin=bounds[0],
        end=bounds[1],
        summary="Invalid coverage",
    )
    before = tuple(harness.store._conn.iterdump())
    with pytest.raises(ValueError):
        harness.store.publish_compaction(
            ref,
            roots=tuple(RunRef(f"run_{i}") for i in range(10)),
        )
    assert tuple(harness.store._conn.iterdump()) == before
    assert horizon(RunHistory(harness.store)) == first["horizon"]


@pytest.mark.parametrize("algorithm", ["DEFAULT", "FORGET"])
def test_cli_rejects_unfinished_compact_run_after_lock_is_released(harness, algorithm):
    h = harness
    project_run_start(
        h.store,
        run_id="run_stranded",
        thread_id="compact_term_a",
        origin="script",
        input=Message.user("unfinished producer"),
    )
    with pytest.raises(ValueError, match="compaction already running: run_stranded"):
        asyncio.run(run(h, algorithm=algorithm, before="run_8"))
    assert len(RunHistory(h.store).thread_view("compact_term_a").roots) == 1
    assert not h.adapter.invocations
    assert h.store.get_thread(thread_id="term_a").horizon is None
