"""State declarations reach model calls without discovery or eager rule reads."""

import asyncio
from dataclasses import replace
from html import escape

import pytest

from tests.integration.execution.test_honor_rules import (
    _answer,
    _calls,
    _harness,
    _spec,
    _workspace_state,
)
from tests.integration.execution.test_pick_guidance import (
    SOURCE,
    _harness as capability_harness,
    _pick,
    _write_guidance,
)
from tests.support.execution_assertions import assert_replayed
from tests.support.execution_harness import RecordingRunTracer
from tests.support.execution_fixtures import project_run_start, project_run_end
from toolang.base.types.message import Message, ToolResultPart, message_text
from toolang.base.types.run import ToolCall
from toolang.execution.records import RecallControlPayload, StoredModelStepGiven
from toolang.execution.types import (
    FieldRef,
    Local,
    Output,
    RunRef,
    ThreadPrefix,
    TypedRef,
    WorkspaceRecallTarget,
)
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateRefresh


def _workspace_messages(call):
    return [
        message_text(message.parts)
        for message in call.messages
        if message_text(message.parts).startswith("<toolang:workspace ")
    ]


def _declarations(harness, run, kind):
    return [
        control
        for control in harness.store.list_run_controls(run_id=run.id)
        if isinstance(control.payload, RecallControlPayload)
        and control.payload.target.kind == kind
    ]


@pytest.mark.parametrize("recall", ["auto", "none"])
def test_first_call_has_all_workspaces_without_discovery_or_rule_reads(
    tmp_path, monkeypatch, recall
):
    from toolang.execution.executor import rules, tool_runtime

    harness, repo, _ = _harness(
        tmp_path,
        [_answer(), _answer()],
        source=SOURCE.replace("context: none", f"recall = {recall}\n  context: none"),
    )
    # Assembly must not probe a root, even when it is temporarily unavailable.
    publication = _workspace_state(harness, {"z": repo.with_name("missing"), "a": repo})

    def forbidden(*args, **kwargs):
        pytest.fail("model assembly must not discover workspace rules")

    monkeypatch.setattr(rules, "load_rules", forbidden)
    monkeypatch.setattr(tool_runtime, "load_rules", forbidden)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            runs = [
                await harness.executor.run(
                    _spec(harness, publication, thread), tracer=tracer
                )
                for _ in range(2)
            ]
            assert all(run.status == "succeeded" for run in runs)
            for invocation in harness.adapter.invocations:
                call = invocation.call
                assert _workspace_messages(call) == [
                    '<toolang:workspace ref="a"/>',
                    '<toolang:workspace ref="z"/>',
                ]
                text = call.instructions + "".join(
                    message_text(m.parts) for m in call.messages
                )
                assert str(repo) not in text
                assert "Root rules." not in text
            assert len(_declarations(harness, runs[0], "workspace")) == 2
            assert len(_declarations(harness, runs[1], "workspace")) == (
                0 if recall == "auto" else 2
            )
            for run in runs:
                for step in harness.store.list_steps(run_id=run.id):
                    assert isinstance(step.given, StoredModelStepGiven)
                    for template in step.given.call.delta.messages:
                        if template.recall is not None:
                            assert not any(
                                isinstance(s, TypedRef) for s in template.segments
                            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_compaction_reintroduces_workspaces_even_if_far_mentions_them(tmp_path):
    harness, _repo, publication = _harness(tmp_path, [_answer(), _answer(), _answer()])
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(
                _spec(harness, publication, thread), tracer=tracer
            )
            retained = await harness.executor.run(
                _spec(harness, publication, thread), tracer=tracer
            )
            assert not _declarations(harness, retained, "workspace")
            summary = project_run_start(
                harness.store,
                run_id=harness.ids.issue_run(),
                thread_id=f"summary_{thread}",
                origin="test",
                input=Message.user("compact"),
            )
            project_run_end(
                harness.store,
                run_id=summary.id,
                output=Output(
                    Local(
                        {
                            "thread": thread,
                            "begin": None,
                            "end": retained.id,
                            "summary": 'Earlier: <toolang:workspace ref="repo"/>',
                        }
                    ),
                    None,
                ),
            )
            current = await harness.executor.run(
                replace(
                    _spec(harness, publication, thread),
                    horizon=FieldRef.from_path(RunRef(summary.id), "output"),
                ),
                tracer=tracer,
            )
            assert all(r.status == "succeeded" for r in (first, retained, current))
            assert len(_declarations(harness, current, "workspace")) == 1
            assert _workspace_messages(harness.adapter.invocations[-1].call) == [
                '<toolang:workspace ref="repo"/>',
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_remap_with_identical_rules_still_requires_model_delivery(tmp_path):
    changed = None

    async def refresh():
        assert changed is not None
        return StateRefresh(changed)

    def write(identity):
        return ToolCall(
            identity,
            identity,
            "fs__write",
            {"path": "workspace://repo/src/result", "text": "done"},
        )

    harness, repo, initial = _harness(
        tmp_path,
        [
            _calls(write("first")),
            _calls(
                ToolCall("reload", "reload", "_toolang__reload", {}), write("changed")
            ),
            _calls(write("retry")),
            _answer(),
        ],
        refresh_state=refresh,
    )
    other = repo.with_name("other")
    (other / "src").mkdir(parents=True)
    (other / "AGENTS.md").write_text("Root rules.")
    (other / "src/AGENTS.md").write_text("Scoped rules.")
    changed = _workspace_state(harness, {"repo": other})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            results = {
                part.tool_call_id: part
                for message in harness.adapter.invocations[-1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            }
            assert "not executed" in results["changed"].error
            assert results["retry"].error is None
            assert not (repo / "src/result").exists()
            assert (other / "src/result").read_text() == "done"
            rules = _declarations(harness, run, "rules")
            assert [c.payload.content for c in rules] == [
                "Root rules.",
                "Scoped rules.",
                "",
                "",
                "Root rules.",
                "Scoped rules.",
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_reload_withdraws_psyches_and_runnable_authority(tmp_path):
    source = "agic helper:\n  Help.\n" + SOURCE.replace(
        "context: none", "hands = helper\n  context: none"
    )
    harness, _ = capability_harness(
        tmp_path,
        [_calls(ToolCall("reload", "reload", "_toolang__reload", {})), _answer()],
        source=source,
        psyche="Resident advice.",
    )
    harness.setup.layout.program.write_text(
        source.replace("hands = helper", "psyches = none")
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
            first, last = (i.call for i in harness.adapter.invocations)
            assert "Resident advice." in first.instructions
            assert '<toolang:runnable-info ref="agic:helper"' in first.instructions
            assert "Resident advice." not in last.instructions
            assert "<toolang:runnable-info " not in last.instructions
            for kind in ("psyche", "runnable-info"):
                (control,) = _declarations(harness, run, kind)
                assert control.payload.revision == "0"
                assert any(
                    f'<toolang:{kind} ref="' in message_text(m.parts)
                    and 'removed="true"' in message_text(m.parts)
                    for m in last.messages
                )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_workspace_add_remove_remap_and_restore_are_presented_once(tmp_path):
    states = []

    async def refresh():
        return StateRefresh(states.pop(0))

    def reload(index):
        return _calls(ToolCall(str(index), str(index), "_toolang__reload", {}))

    harness, repo, initial = _harness(
        tmp_path, [*(reload(i) for i in range(5)), _answer()], refresh_state=refresh
    )
    states.extend(
        [
            _workspace_state(harness, {"repo": repo, "added": repo.with_name("extra")}),
            _workspace_state(
                harness,
                {"repo": repo.with_name("remapped"), "added": repo.with_name("extra")},
            ),
            _workspace_state(harness, {"added": repo.with_name("extra")}),
            _workspace_state(harness, {"repo": repo, "added": repo.with_name("extra")}),
            _workspace_state(harness, {"repo": repo, "added": repo.with_name("extra")}),
        ]
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            controls = _declarations(harness, run, "workspace")
            assert [c.payload.target for c in controls] == [
                WorkspaceRecallTarget("repo"),
                WorkspaceRecallTarget("added"),
                WorkspaceRecallTarget("repo"),
                WorkspaceRecallTarget("repo"),
                WorkspaceRecallTarget("repo"),
            ]
            assert controls[0].payload.revision == controls[-1].payload.revision
            assert controls[2].payload.revision != controls[0].payload.revision
            assert controls[3].payload.revision == "0"
            assert all(c.triggered_by is None for c in controls)
            messages = [
                _workspace_messages(i.call) for i in harness.adapter.invocations
            ]
            assert [len(m) for m in messages] == [1, 2, 3, 4, 5, 5]
            assert messages[-1][-2:] == [
                '<toolang:workspace ref="repo" removed="true"/>',
                '<toolang:workspace ref="repo"/>',
            ]
            assert all("revision=" not in text for group in messages for text in group)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("kind,name", [("skill", "testing"), ("service", "github")])
def test_definition_changes_withdraw_guidance_until_explicit_pick(tmp_path, kind, name):
    ref = f"home://{kind}s/{name}"
    source = SOURCE.replace(
        "context: none", f"{kind}s = {kind}/{name}\n  context: none"
    )

    def reload(index):
        return _calls(ToolCall(str(index), str(index), "_toolang__reload", {}))

    harness, _ = capability_harness(
        tmp_path,
        [
            _calls(_pick(kind=kind)),
            reload(1),
            _calls(_pick("again", kind=kind)),
            reload(2),
            reload(3),
            _answer(),
        ],
        source=source,
        content="Original guidance.",
    )
    path = harness.setup.layout.home / (
        "skills/testing/SKILL.md" if kind == "skill" else "services/github.md"
    )
    _write_guidance(path, "Changed <guidance>.")
    changed = prepare_agent_state(harness.setup.layout)
    harness.setup.layout.program.write_text(
        source.replace(f"{kind}s = {kind}/{name}", f"{kind}s = none")
    )
    removed = prepare_agent_state(harness.setup.layout)
    harness.setup.layout.program.write_text(source)
    restored = prepare_agent_state(harness.setup.layout)
    states = [changed, removed, restored]

    async def refresh():
        return StateRefresh(states.pop(0))

    harness.executor._refresh_state = refresh
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
            calls = [i.call for i in harness.adapter.invocations]
            guidance = _declarations(harness, run, kind)
            assert [c.payload.content for c in guidance] == [
                "Original guidance.",
                "",
                "Changed <guidance>.",
                "",
            ]
            triggers = _declarations(harness, run, f"{kind}-trigger")
            assert [c.payload.revision == "0" for c in triggers] == [False, True, False]
            assert triggers[0].payload.revision == triggers[-1].payload.revision

            def text(call):
                return "\n".join(message_text(m.parts) for m in call.messages)

            assert f'<toolang:{kind}-guidance ref="{ref}" removed="true"/>' in text(
                calls[2]
            )
            assert escape("Changed <guidance>.", quote=False) not in text(calls[2])
            assert escape("Changed <guidance>.", quote=False) in text(calls[3])
            # Restoring availability must not silently restore the old guidance.
            assert guidance[-1].payload.revision == "0"

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
