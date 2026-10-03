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
from tests.support.execution_assertions import assert_replayed, route_snapshots
from tests.support.execution_harness import (
    PublicationTracer,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import ToolResultPart, message_text
from toolang.base.types.run import ToolCall
from toolang.execution.records import RecallControlPayload, StoredModelStepGiven
from toolang.execution.types import (
    ThreadPrefix,
    TypedRef,
)
from toolang.state.prepare import prepare_agent_state


def _workspace_messages(call):
    return [
        message_text(message.parts)
        for message in call.messages
        if message.tag == "workspace"
    ]


def _declarations(harness, run, kind):
    return [
        control
        for control in harness.store.list_run_controls(run_id=run.id)
        if isinstance(control.payload, RecallControlPayload)
        and control.payload.target.kind == kind
    ]


@pytest.mark.parametrize("recall", ["default", "none"])
def test_first_call_has_all_workspaces_without_discovery_or_rule_reads(
    tmp_path, monkeypatch, recall
):
    from toolang.execution.executor import rules, tool_runtime

    harness, repo, _ = _harness(
        tmp_path,
        [_answer(), _answer()],
        source=SOURCE.replace("context = none", f"recall = {recall}\n  context = none"),
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
                assert _workspace_messages(call)[-1:] == [
                    '<toolang:workspace list="lab,a"/>'
                ]
                text = call.instructions + "".join(
                    message_text(m.parts) for m in call.messages
                )
                assert str(repo) not in text
                assert "Root rules." not in text
            assert not _declarations(harness, runs[0], "workspace")
            assert not _declarations(harness, runs[1], "workspace")
            for run in runs:
                for step in harness.store.list_steps(run_id=run.id):
                    assert isinstance(step.given, StoredModelStepGiven)
                    for template in step.given.call.messages.delta:
                        if template.recall is not None:
                            assert not any(
                                isinstance(s, TypedRef) for s in template.content
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
            from tests.support.execution_fixtures import project_compaction

            horizon = project_compaction(
                harness.store,
                thread=thread,
                begin=first.id,
                end=retained.id,
                summary='Earlier: <toolang:workspace list="tmp,repo"/>',
            )
            current = await harness.executor.run(
                replace(
                    _spec(harness, publication, thread),
                    horizon=horizon,
                ),
                tracer=tracer,
            )
            assert all(r.status == "succeeded" for r in (first, retained, current))
            assert not _declarations(harness, current, "workspace")
            assert _workspace_messages(harness.adapter.invocations[-1].call)[-1:] == [
                '<toolang:workspace list="lab,repo"/>',
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_published_remap_keeps_active_rule_bindings(tmp_path):
    def write(identity):
        return ToolCall(
            identity,
            identity,
            "fs__write",
            {"path": "repo://src/result", "text": "done"},
        )

    harness, repo, initial = _harness(
        tmp_path,
        [
            _calls(write("first")),
            _calls(
                ToolCall("publication", "publication", "test__checkpoint", {}),
                write("changed"),
            ),
            _calls(write("retry")),
            _answer(),
        ],
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )
    other = repo.with_name("other")
    (other / "src").mkdir(parents=True)
    (other / "AGENTS.md").write_text("Root rules.")
    (other / "src/AGENTS.md").write_text("Scoped rules.")
    changed = _workspace_state(harness, {"repo": other})
    tracer = PublicationTracer(harness, {"publication": changed})

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
            assert results["changed"].error is None
            assert results["retry"].error is None
            assert (repo / "src/result").read_text() == "done"
            assert not (other / "src/result").exists()
            rules = _declarations(harness, run, "rules")
            assert [c.payload.content for c in rules] == [
                "Root rules.",
                "Scoped rules.",
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_publication_preserves_bound_psyches_and_runnable_authority(tmp_path):
    source = "agic helper:\n  Help.\n" + SOURCE.replace(
        "context = none", "hands = helper\n  context = none"
    )
    harness, _ = capability_harness(
        tmp_path,
        [
            _calls(ToolCall("publication", "publication", "test__checkpoint", {})),
            _answer(),
        ],
        source=source,
        psyche="Resident advice.",
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )
    harness.setup.layout.program.write_text(
        source.replace("hands = helper", "psyches = none")
    )
    tracer = PublicationTracer(
        harness, {"publication": prepare_agent_state(harness.setup.layout)}
    )

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
            assert [item["ref"] for item in route_snapshots(first)["hands"]] == [
                "agic:helper"
            ]
            assert "Resident advice." in last.instructions
            assert route_snapshots(last) == route_snapshots(first)
            assert not _declarations(harness, run, "psyche")

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("context", ["none", "custom"])
def test_publication_cannot_expand_bound_route_authority_and_replays(tmp_path, context):
    states = []

    def source(hands, handoffs, type_name="Text"):
        directives = ("  hands = helper\n" if hands else "  hands = none\n") + (
            "  handoffs = helper\n" if handoffs else "  handoffs = none\n"
        )
        return (
            "context custom: User context.\n"
            f"agic helper(_: {type_name}) -> Text:\n  Help.\n"
            f"agic chat() -> Text:\n{directives}"
            f"  context = {context}\n  user: Complete the task.\n"
        )

    versions = [
        (False, False, "Text"),
        (True, False, "Text"),
        (True, True, "Text"),
        (False, True, "Text"),
        (False, False, "Text"),
        (True, True, "Number"),
        (True, True, "Number"),
    ]
    harness, _, initial = _harness(
        tmp_path,
        [
            *(
                _calls(ToolCall(str(i), str(i), "test__checkpoint", {}))
                for i in range(len(versions) - 1)
            ),
            _answer(),
        ],
        source=source(*versions[0]),
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )
    for version in versions[1:]:
        harness.setup.layout.program.write_text(source(*version))
        states.append(prepare_agent_state(harness.setup.layout))
    tracer = PublicationTracer(
        harness, {str(i): value for i, value in enumerate(states)}
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            calls = [item.call for item in harness.adapter.invocations]
            assert len(calls) == len(versions)
            assert all(call.instructions == calls[0].instructions for call in calls)
            for call in calls:
                hands, handoffs, type_name = versions[0]
                snapshots = route_snapshots(call)
                for tag, enabled in (("hands", hands), ("handoffs", handoffs)):
                    assert [item["ref"] for item in snapshots[tag]] == (
                        ["agic:helper"] if enabled else []
                    )
                    if enabled:
                        assert snapshots[tag][0]["input"] == {
                            "documentation": "",
                            "type": type_name,
                            "optional": False,
                        }
                text = next(
                    message_text(m.parts)
                    for m in reversed(call.messages)
                    if message_text(m.parts).startswith("<toolang:hands ")
                )
                if context == "custom":
                    assert (
                        text.index("<toolang:hands ")
                        < text.index("<toolang:handoffs ")
                        < text.index("<toolang:context>")
                    )
                    assert "User context." in text
                else:
                    assert "<toolang:context>" not in text
            assert route_snapshots(calls[0]) == {"hands": [], "handoffs": []}
            controls = [
                c
                for c in harness.store.list_run_controls(run_id=run.id)
                if isinstance(c.payload, RecallControlPayload)
            ]
            assert not controls
            for step in harness.store.list_steps(run_id=run.id):
                if isinstance(step.given, StoredModelStepGiven):
                    assert all(
                        m.recall is None or m.tag == "workspace"
                        for m in step.given.call.messages.delta
                    )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_route_budget_failure_does_not_publish_partial_snapshots(tmp_path):
    def source(description):
        return "\n\n".join(
            f"## {description}\nagic action_{i:02d}:\n  Act." for i in range(64)
        ) + (
            "\n\nagic chat() -> Text:\n  hands = "
            + ", ".join(f"agic:action_{i:02d}" for i in range(64))
            + "\n  context = none\n  Complete the task.\n"
        )

    harness, _ = capability_harness(
        tmp_path,
        [
            _calls(ToolCall("publication", "publication", "test__checkpoint", {})),
            _answer(),
        ],
        source=source("Short description."),
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
    )
    harness.setup.layout.program.write_text(source("界" * 512))
    tracer = PublicationTracer(
        harness, {"publication": prepare_agent_state(harness.setup.layout)}
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "failed"
            assert len(harness.adapter.invocations) == 1
            assert (
                len(route_snapshots(harness.adapter.invocations[0].call)["hands"]) == 64
            )
            assert not any(
                isinstance(c.payload, RecallControlPayload)
                for c in harness.store.list_run_controls(run_id=run.id)
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_workspace_publications_preserve_active_workspace_listing(tmp_path):
    states = []

    def publication(index):
        return _calls(ToolCall(str(index), str(index), "test__checkpoint", {}))

    harness, repo, initial = _harness(
        tmp_path,
        [*(publication(i) for i in range(5)), _answer()],
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
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
    tracer = PublicationTracer(
        harness, {str(i): value for i, value in enumerate(states)}
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            assert not _declarations(harness, run, "workspace")
            messages = [
                _workspace_messages(i.call) for i in harness.adapter.invocations
            ]
            assert [group[-1] for group in messages] == [
                '<toolang:workspace list="lab,repo"/>'
            ] * len(messages)
            assert all("revision=" not in text for group in messages for text in group)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("kind,name", [("skill", "testing"), ("service", "github")])
def test_definition_changes_invalidate_and_refresh_guidance(tmp_path, kind, name):
    ref = f"{kind}/{name}"
    source = SOURCE.replace(
        "context = none", f"{kind}s = {kind}/{name}\n  context = none"
    )

    def publication(index):
        return _calls(ToolCall(str(index), str(index), "test__checkpoint", {}))

    harness, _ = capability_harness(
        tmp_path,
        [
            _calls(_pick(kind=kind)),
            publication(1),
            _calls(_pick("again", kind=kind)),
            publication(2),
            publication(3),
            _answer(),
        ],
        source=source,
        content="Original guidance.",
        tools={"test__checkpoint": RecordingTool("test__checkpoint", output={})},
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

    tracer = PublicationTracer(
        harness, {str(i + 1): value for i, value in enumerate(states)}
    )

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
            ]
            triggers = _declarations(harness, run, f"{kind}-trigger")
            assert len(triggers) == 1
            for index, call in enumerate(calls[1:], 1):
                text = "\n".join(message_text(m.parts) for m in call.messages)
                assert "Original guidance." in text
                assert (escape("Changed <guidance>.", quote=False) in text) == (
                    index >= 3
                )
                assert (
                    f'<toolang:{kind}-guidance ref="{ref}" removed="true"/>' in text
                ) == (index >= 2)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
