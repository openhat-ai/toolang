"""Model-directed picks preserve provenance, visibility, and State-free replay."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_fixtures import project_run_start, project_run_end
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart, ToolResultPart, message_text
from toolang.base.types.policy import AgentCeiling
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.events import PartBegin, StepBegin, StepEnd
from toolang.execution.records import RecallControlPayload, StoredModelStepGiven
from toolang.execution.types import (
    FieldRef,
    Local,
    RunRef,
    SkillRecallTarget,
    ThreadPrefix,
    ToolStepGiven,
)
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import StateCap
from toolang.state.watcher import StateWatcher


SOURCE = """
agic chat() -> Text:
  context: none
  user: Complete the task.
"""
GUIDANCE = "Unique guidance <body>: test before publishing."


def _pick(identity="pick", *, kind="skill", ref=None):
    return ToolCall(
        identity,
        identity,
        "_toolang__pick",
        {
            "kind": kind,
            "ref": ref
            or (
                "home://skills/testing" if kind == "skill" else "home://services/github"
            ),
        },
    )


def _calls(*calls):
    return ModelCallResult(tool_calls=tuple(calls))


def _answer():
    return ModelCallResult(message=Message.assistant("done"))


def _harness(tmp_path, responses, *, source=SOURCE, content=GUIDANCE):
    layout = AgentLayout.resident(tmp_path, "alice")
    skill = layout.home / "skills/testing/SKILL.md"
    skill.parent.mkdir(parents=True)
    _write_guidance(skill, content)
    other = layout.home / "skills/private/SKILL.md"
    other.parent.mkdir(parents=True)
    _write_guidance(other, "Unselected guidance.")
    service = layout.home / "services/github.md"
    service.parent.mkdir(parents=True)
    _write_guidance(service, content)
    _write_guidance(service.with_name("private.md"), "Unselected service guidance.")
    layout.program.write_text(source, encoding="utf-8")
    watcher = StateWatcher(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=prepare_agent_state(layout),
        responses=responses,
        refresh_state=watcher.refresh_result,
    )
    return harness, skill


def _write_guidance(path, content):
    path.write_text(
        f"---\ndescription: Test guidance\n---\n{content}\n", encoding="utf-8"
    )


def _recalls(harness, run):
    return [
        c
        for c in harness.store.list_run_controls(run_id=run.id)
        if isinstance(c.payload, RecallControlPayload)
    ]


def _results(harness, run):
    return {
        s.given.call.tool_call_id: s.output.value
        for s in harness.store.list_steps(run_id=run.id)
        if isinstance(s.given, ToolStepGiven)
        and s.output is not None
        and isinstance(s.output.value, ToolResultPart)
    }


def _assert_replay_without_state(harness, tracer, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not read State")

    monkeypatch.setattr(StateCap, "read_content", forbidden)
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize("content", [GUIDANCE, ""])
def test_pick_reuses_pending_then_visible_guidance(
    tmp_path: Path, monkeypatch, kind, content
):
    harness, _ = _harness(
        tmp_path,
        [
            _calls(_pick("first", kind=kind), _pick("duplicate", kind=kind)),
            _calls(_pick("visible", kind=kind)),
            _answer(),
        ],
        content=content,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            (control,) = _recalls(harness, run)
            assert isinstance(control.payload, RecallControlPayload)
            assert control.payload.content == content
            assert (
                control.payload.revision == sha256(content.encode()).hexdigest() != "0"
            )
            results = _results(harness, run)
            assert all(result.error is None for result in results.values())
            assert (
                results["first"].output
                == results["duplicate"].output
                == {"controls": [str(control.ref)]}
            )
            assert results["visible"].output == {"controls": []}
            steps = harness.store.list_steps(run_id=run.id)
            assert control.triggered_by == steps[1].ref
            adopted = [s for s in steps if control.ref in s.preceded_by]
            assert len(adopted) == 1 and isinstance(
                adopted[0].given, StoredModelStepGiven
            )
            first = harness.adapter.invocations[0].call
            assert "_toolang__pick" in first.instructions
            if content:
                assert content not in first.instructions
            for invocation in harness.adapter.invocations[1:]:
                recalled = [
                    m
                    for m in invocation.call.messages
                    if m.role == "user"
                    and message_text(m.parts).startswith(f"<{kind} ref=")
                ]
                assert len(recalled) == 1
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    _assert_replay_without_state(harness, tracer, monkeypatch)


@pytest.mark.parametrize("recall", ["auto", "none"])
def test_only_selected_near_counts_in_the_next_run(tmp_path: Path, recall):
    harness, _ = _harness(
        tmp_path,
        [_calls(_pick()), _answer(), _calls(_pick()), _answer()],
        source=SOURCE.replace("context: none", f"recall = {recall}\n  context: none"),
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            runs = [
                await harness.executor.run(
                    harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
                )
                for _ in range(2)
            ]
            assert all(run.status == "succeeded" for run in runs)
            assert len(_recalls(harness, runs[0])) == 1
            assert len(_recalls(harness, runs[1])) == (0 if recall == "auto" else 1)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("ceiling", [False, True])
@pytest.mark.parametrize("kind, name", [("skill", "testing"), ("service", "github")])
def test_pick_matches_the_effective_catalog(tmp_path: Path, ceiling, kind, name):
    selected = f"home://{kind}s/{name}"
    excluded = f"home://{kind}s/private"
    source = (
        SOURCE
        if ceiling
        else SOURCE.replace(
            "context: none", f"{kind}s = {kind}/{name}\n  context: none"
        )
    )
    harness, _ = _harness(
        tmp_path,
        [
            _calls(
                _pick("allowed", kind=kind, ref=selected),
                _pick("name", kind=kind, ref=name),
                _pick("wildcard", kind=kind, ref=f"home://{kind}s/*"),
                _pick("private", kind=kind, ref=excluded),
                _pick(
                    "wrong-kind",
                    kind="service" if kind == "skill" else "skill",
                    ref=selected,
                ),
            ),
            _answer(),
        ],
        source=source,
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    ceilings=(
                        AgentCeiling(skills=("skill/testing",))
                        if kind == "skill"
                        else AgentCeiling(services=("service/github",)),
                    )
                    if ceiling
                    else (),
                )
            )
            assert run.status == "succeeded", run.error
            catalog = harness.adapter.invocations[0].call.instructions
            assert f'ref="{selected}"' in catalog
            assert f'ref="{excluded}"' not in catalog
            (control,) = _recalls(harness, run)
            assert control.payload.content == GUIDANCE
            results = _results(harness, run)
            assert results.pop("allowed").output == {"controls": [str(control.ref)]}
            assert all(
                result.error and "available catalog" in result.error
                for result in results.values()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "execute"])
def test_pick_uses_the_target_modules_effective_resources(tmp_path: Path, operation):
    home_ref, module_ref = "home://services/github", "inline://services/github"
    module_guidance = "Use the target module's guidance."
    layout = AgentLayout.resident(tmp_path, "alice")
    module = layout.home / "flows/research.too"
    module.parent.mkdir(parents=True)
    module.write_text(
        f"""service github:
  description = Module guidance.
  transport = http
  target = https://example.invalid/mcp

  {module_guidance}

agic worker() -> Text:
  context: none
  user: Complete the module task.

flow research() -> Text:
  run worker
""",
        encoding="utf-8",
    )
    directive = "hands" if operation == "run" else "handoffs"
    harness, _ = _harness(
        tmp_path,
        [
            _calls(
                _pick("caller", kind="service", ref=home_ref),
                _pick("foreign", kind="service", ref=module_ref),
            ),
            _calls(
                ToolCall(
                    "transfer",
                    "transfer",
                    f"_toolang__{operation}",
                    {"runnable": "research"},
                )
            ),
            _calls(
                _pick("target", kind="service", ref=module_ref),
                _pick("shadowed", kind="service", ref=home_ref),
            ),
            _answer(),
            *(
                [_calls(_pick("resumed", kind="service", ref=home_ref)), _answer()]
                if operation == "run"
                else []
            ),
        ],
        source=SOURCE.replace(
            "context: none", f"{directive} = research\n  context: none"
        ),
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            runs = harness.store.list_run_tree(root_run_id=root.id)
            results = {
                key: result
                for run in runs
                for key, result in _results(harness, run).items()
            }
            for identity in ("foreign", "shadowed"):
                assert (
                    results[identity].error
                    and "available catalog" in results[identity].error
                )
            assert results["caller"].error is None
            assert results["target"].error is None
            contents = [
                c.payload.content for run in runs for c in _recalls(harness, run)
            ]
            assert contents == [GUIDANCE, module_guidance]
            caller = harness.adapter.invocations[0].call.instructions
            target = harness.adapter.invocations[2].call.instructions
            assert f'ref="{home_ref}"' in caller and f'ref="{module_ref}"' not in caller
            assert f'ref="{module_ref}"' in target and f'ref="{home_ref}"' not in target
            if operation == "run":
                assert results["resumed"].output == {"controls": []}
                assert harness.adapter.invocations[4].call.instructions == caller
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_pick_uses_the_reloaded_resource_selection(tmp_path: Path):
    harness, _ = _harness(
        tmp_path,
        [
            _calls(_pick("before", ref="home://skills/private")),
            _calls(
                ToolCall("reload", "reload", "_toolang__reload", {}),
                _pick("denied", ref="home://skills/private"),
                _pick("allowed"),
            ),
            _answer(),
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepEnd)
                and event.output is not None
                and isinstance(event.output.value, ToolResultPart)
                and event.output.value.tool_call_id == "before"
            ):
                harness.setup.layout.program.write_text(
                    SOURCE.replace(
                        "context: none", "skills = skill/testing\n  context: none"
                    ),
                    encoding="utf-8",
                )

    tracer = Tracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            results = _results(harness, run)
            assert (
                results["denied"].error
                and "available catalog" in results["denied"].error
            )
            assert results["allowed"].error is None
            assert len(_recalls(harness, run)) == 2
            before = harness.adapter.invocations[0].call.instructions
            after = harness.adapter.invocations[-1].call.instructions
            assert 'ref="home://skills/private"' in before
            assert 'ref="home://skills/private"' not in after
            assert 'ref="home://skills/testing"' in after

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("same_batch", [False, True])
def test_revision_reversal_is_not_reused_across_newer_content(
    tmp_path: Path, same_batch
):
    a, b = "Revision A.", "Revision B."
    batches = [
        (_pick("a"),),
        (ToolCall("reload-b", "reload-b", "_toolang__reload", {}), _pick("b")),
        (ToolCall("reload-a", "reload-a", "_toolang__reload", {}), _pick("a-again")),
    ]
    harness, skill = _harness(
        tmp_path,
        [
            *(
                [_calls(*(call for batch in batches for call in batch))]
                if same_batch
                else [_calls(*batch) for batch in batches]
            ),
            _calls(_pick("visible")),
            _answer(),
        ],
        content=a,
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepEnd)
                and event.output is not None
                and isinstance(event.output.value, ToolResultPart)
            ):
                if event.output.value.tool_call_id == "a":
                    _write_guidance(skill, b)
                elif event.output.value.tool_call_id == "b":
                    _write_guidance(skill, a)

    tracer = Tracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            controls = _recalls(harness, run)
            assert [c.payload.content for c in controls] == [a, b, a]
            results = _results(harness, run)
            assert results["a"].output != results["a-again"].output
            assert results["visible"].output == {"controls": []}
            assert all(result.error is None for result in results.values())
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_canceled_delivery_keeps_recall_but_does_not_make_it_visible(tmp_path: Path):
    harness, _ = _harness(tmp_path, [_calls(_pick()), _calls(_pick()), _answer()])
    handle = None
    canceled = False

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal canceled
            await super().on_event(event)
            if (
                not canceled
                and isinstance(event, PartBegin)
                and event.part_type == "tool_result"
            ):
                canceled = True
                assert handle is not None
                handle.cancel(timing="immediate")
                await asyncio.sleep(0)

    tracer = Tracer()

    async def scenario():
        nonlocal handle
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            first = await handle
            assert first.status == "canceled"
            (pending,) = _recalls(harness, first)
            assert all(
                pending.ref not in s.preceded_by
                for s in harness.store.list_steps(run_id=first.id)
            )
            second = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            assert second.status == "succeeded", second.error
            assert len(_recalls(harness, second)) == 1
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_pick_sees_controls_added_at_the_tool_boundary(tmp_path: Path):
    harness, _ = _harness(tmp_path, [_calls(_pick("first"), _pick("again")), _answer()])

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepBegin)
                and isinstance(event.given, ToolStepGiven)
                and event.given.call.tool_call_id == "first"
            ):
                harness.store.accept_recall_control(
                    run_id=event.step.run_id,
                    triggered_by=event.step,
                    payload=RecallControlPayload(
                        SkillRecallTarget("home://skills/testing"),
                        "b" * 64,
                        "Earlier pending content.",
                    ),
                    created_at=event.started_at,
                )

    tracer = Tracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            assert len(_recalls(harness, run)) == 2
            results = _results(harness, run)
            assert results["first"].output == results["again"].output

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("at_boundary", [False, True])
def test_compaction_excludes_old_guidance_even_when_far_mentions_it(
    tmp_path: Path, at_boundary
):
    harness, _ = _harness(
        tmp_path,
        [
            _calls(_pick()),
            _answer(),
            _answer(),
            *([_calls(_pick("before"))] if at_boundary else []),
            _calls(_pick()),
            _answer(),
        ],
    )
    horizon = None

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepEnd)
                and event.output is not None
                and isinstance(event.output.value, ToolResultPart)
                and event.output.value.tool_call_id == "before"
            ):
                assert horizon is not None
                harness.store.accept_compact_control(
                    run_id=event.step.run_id,
                    horizon=horizon,
                    triggered_by=event.step,
                    created_at=event.finished_at,
                )

    tracer = Tracer()

    async def scenario():
        nonlocal horizon
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            retained = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            summary = project_run_start(
                harness.store,
                run_id=harness.ids.issue_run(),
                # Exercise explicit control adoption, not automatic discovery.
                thread_id=f"summary_{thread}",
                origin="test",
                input=Message.user("compact"),
            )
            project_run_end(
                harness.store,
                run_id=summary.id,
                output=Local(
                    {
                        "thread": thread,
                        "begin": None,
                        "end": retained.id,
                        "summary": f"Earlier guidance: {GUIDANCE}",
                    }
                ),
            )
            horizon = FieldRef.from_path(RunRef(summary.id), "output")
            current = await harness.executor.run(
                replace(
                    harness.run_spec(thread=thread, runnable="chat"),
                    horizon=None if at_boundary else horizon,
                ),
                tracer=tracer,
            )
            assert all(run.status == "succeeded" for run in (first, retained, current))
            assert len(_recalls(harness, current)) == 1
            if at_boundary:
                assert _results(harness, current)["before"].output == {"controls": []}
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_fork_visibility_survives_source_rewind(tmp_path: Path):
    harness, _ = _harness(tmp_path, [_calls(_pick()), _answer()] * 3)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat")
            )
            fork = harness.threads.fork(thread_id=thread)
            harness.threads.rewind(thread_id=thread, run_id=first.id)
            inherited = await harness.executor.run(
                harness.run_spec(thread=fork, runnable="chat")
            )
            fresh = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat")
            )
            assert all(run.status == "succeeded" for run in (first, inherited, fresh))
            assert _results(harness, inherited)["pick"].output == {"controls": []}
            assert len(_recalls(harness, fresh)) == 1

    asyncio.run(scenario())


def test_execute_starts_a_new_now_without_restoring_guidance(tmp_path: Path):
    source = (
        SOURCE.replace("context: none", "handoffs = agic:target\n  context: none")
        + """
agic target() -> Text:
  context: none
  user: Continue in the target.
"""
    )
    harness, _ = _harness(
        tmp_path,
        [
            _calls(_pick("before")),
            _calls(
                ToolCall(
                    "transfer", "transfer", "_toolang__execute", {"runnable": "target"}
                )
            ),
            _calls(_pick("after")),
            _answer(),
        ],
        source=source,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            assert len(_recalls(harness, run)) == 2
            assert (
                _results(harness, run)["before"].output
                != _results(harness, run)["after"].output
            )
            target_call = harness.adapter.invocations[2].call
            assert not any(
                message_text(m.parts).startswith("<skill ref=")
                for m in target_call.messages
            )
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_parallel_children_keep_pending_and_adopted_recalls_isolated(tmp_path: Path):
    gates = AsyncGate(), AsyncGate()
    source = (
        SOURCE
        + """
agic child(_: Part[]) -> Part[]:
  context: none
  user: Child task.

flow parent(_: Part[]) -> Part[][]:
  storm 2 using child in 2 lanes
"""
    )
    harness, _ = _harness(
        tmp_path,
        [
            *(ScriptedModelTurn(result=_calls(_pick()), gate=gate) for gate in gates),
            _answer(),
            _answer(),
            _calls(_pick()),
            _answer(),
        ],
        source=source,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", primary=(TextPart("seed"),)
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates)),
                timeout=2,
            )
            for gate in gates:
                gate.release()
            parent = await asyncio.wait_for(handle, timeout=2)
            assert parent.status == "succeeded", parent.error
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=parent.id)
                if r.parent is not None
            ]
            assert len(children) == 2
            assert all(len(_recalls(harness, child)) == 1 for child in children)
            assert not _recalls(harness, parent)
            next_root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            assert next_root.status == "succeeded", next_root.error
            assert len(_recalls(harness, next_root)) == 1
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_restart_recovers_visibility_from_saved_deltas(tmp_path: Path, monkeypatch):
    first, _ = _harness(tmp_path, [_calls(_pick()), _answer()])
    tracer = RecordingRunTracer()

    async def scenario():
        async with first:
            thread = first.threads.create(prefix=ThreadPrefix.TERM)
            run = await first.executor.run(
                first.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            assert run.status == "succeeded", run.error
        reopened = ExecutionHarness.create(
            tmp_path,
            source=SOURCE,
            state=first.state,
            responses=[_calls(_pick()), _answer()],
        )
        async with reopened:
            run = await reopened.executor.run(
                reopened.run_spec(thread=thread, runnable="chat"), tracer=tracer
            )
            assert run.status == "succeeded", run.error
            assert _results(reopened, run)["pick"].output == {"controls": []}
            assert not _recalls(reopened, run)

    asyncio.run(scenario())
    _assert_replay_without_state(first, tracer, monkeypatch)


def test_read_failure_is_not_a_removal(tmp_path: Path, monkeypatch):
    harness, _ = _harness(tmp_path, [_calls(_pick()), _answer()])

    def unreadable(cap):
        raise PermissionError("guidance cannot be read")

    monkeypatch.setattr(StateCap, "read_content", unreadable)

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            assert _results(harness, run)["pick"].error == "guidance cannot be read"
            assert not _recalls(harness, run)

    asyncio.run(scenario())
