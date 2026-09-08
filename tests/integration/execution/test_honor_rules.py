"""Workspace rules gate real tools through durable runtime Steps and recalls."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    AsyncGate,
    ScriptedModelTurn,
)
from toolang.base.types.message import (
    Message,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.events import PartBegin, StepBegin, StepEnd
from toolang.execution.records import RecallControlPayload
from toolang.execution.store import RunStore
from toolang.execution.types import (
    RulesRecallTarget,
    ThreadPrefix,
    ToolStepGiven,
    TypedRef,
    ControlRef,
)
from toolang.plugin.toolsets.loading import load_tools
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateRefresh


SOURCE = """
agic chat() -> Text:
  context: none
  user: Complete the task.
"""

RETRY_MESSAGE = (
    "Workspace rules were just loaded. This operation was not executed; "
    "please retry if it complies with them."
)


def _results(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolResultPart)]


def _call(identity, name="fs__write", **arguments):
    return ToolCall(
        identity,
        identity,
        name,
        arguments or {"workspace": "repo", "path": "/src/result", "text": "done"},
    )


def _calls(*calls):
    return ModelCallResult(tool_calls=calls)


def _answer():
    return ModelCallResult(message=Message.assistant("done"))


def _harness(tmp_path, responses, *, source=SOURCE, refresh_state=None):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(source)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=responses,
        tools=load_tools(queries=("fs/*", "shell/*")),
        refresh_state=refresh_state,
        state=prepare_agent_state(layout),
    )
    repo = harness.setup.layout.home / "repo"
    (repo / "src/unused").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Root rules.")
    (repo / "src/AGENTS.md").write_text("Scoped rules.")
    (repo / "src/unused/AGENTS.md").write_text("Unused rules.")
    publication = _workspace_state(harness, {"repo": repo})
    return harness, repo, publication


def _workspace_state(harness, workspaces):
    import tomlkit

    harness.setup.layout.config.write_text(
        tomlkit.dumps(
            {"workspaces": {name: str(path) for name, path in workspaces.items()}}
        )
    )
    return prepare_agent_state(harness.setup.layout)


def _spec(harness, publication, thread=None):
    return replace(
        harness.run_spec(
            thread=thread or harness.threads.create(prefix=ThreadPrefix.TERM),
            runnable="chat",
        ),
        state=publication,
    )


def _recalls(harness, run):
    return [
        c
        for c in harness.store.list_run_controls(run_id=run.id)
        if isinstance(c.payload, RecallControlPayload)
    ]


def _tool_steps(harness, run):
    return [
        s
        for s in harness.store.list_steps(run_id=run.id)
        if isinstance(s.given, ToolStepGiven)
    ]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("fs__write", {"path": "/src/result", "text": "done"}),
        ("fs__read", {"path": "/src/AGENTS.md"}),
        ("shell__execute", {"cwd": "/src", "command": "printf done > result"}),
    ],
)
def test_honor_precedes_blocked_batch_and_retry_executes_once(
    tmp_path, name, arguments
):
    def call(identity):
        return replace(
            _call(identity, name, workspace="repo", **arguments),
            call_id=f"provider-{identity}",
        )

    harness, repo, publication = _harness(
        tmp_path,
        [_calls(call("first"), call("duplicate")), _calls(call("retry")), _answer()],
    )
    early_effects = []

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.step.index < 4:
                early_effects.append((repo / "src/result").exists())
                if event.kind == "tool":
                    for summary in event.output.value.output["controls"]:
                        ref = ControlRef.parse(summary["ref"])
                        assert (
                            harness.store.get_run_control(
                                run_id=event.step.run_id, index=ref.index
                            )
                            is not None
                        )

    tracer = Tracer()

    async def scenario():
        async with harness:
            spec = replace(
                _spec(harness, publication),
                limits=replace(harness.setup.limits, agic_tool_calls=3),
            )
            run = await harness.executor.run(spec, tracer=tracer)
            assert run.status == "succeeded", run.error
            assert early_effects and not any(early_effects)
            controls = _recalls(harness, run)
            assert [c.payload.target for c in controls] == [
                RulesRecallTarget("repo", "/"),
                RulesRecallTarget("repo", "/src"),
            ]
            steps = _tool_steps(harness, run)
            assert [s.given.trigger for s in steps] == [
                "runtime",
                "runtime",
                "model",
            ]
            first, duplicate = _results(harness.adapter.invocations[1].call.messages)
            assert first.error == duplicate.error == RETRY_MESSAGE
            assert (first.call_id, duplicate.call_id) == (
                "provider-first",
                "provider-duplicate",
            )
            assert first.output == duplicate.output == {}
            assert (
                steps[0].output.value.output
                == steps[1].output.value.output
                == {
                    "controls": [
                        {
                            "ref": str(c.ref),
                            "target": {
                                "kind": "rules",
                                "workspace": "repo",
                                "path": path,
                            },
                            "revision": c.payload.revision,
                        }
                        for c, path in zip(
                            controls, ("/AGENTS.md", "/src/AGENTS.md"), strict=True
                        )
                    ]
                }
            )
            for step, identity in zip(steps, ("first", "duplicate")):
                original = harness.store.resolve_value(TypedRef(step.input[0], "Part"))
                assert isinstance(original, ToolCallPart)
                assert original.tool_call_id == identity
            assert {s.given.call.tool_call_id for s in steps}.isdisjoint(
                {"first", "duplicate"}
            )
            assert all(c.triggered_by == steps[0].ref for c in controls)
            assert steps[-1].output.value.error is None
            if name != "fs__read":
                assert (repo / "src/result").read_text() == "done"
            else:
                assert steps[-1].output.value.output["text"] == "Scoped rules."
            call = harness.adapter.invocations[1].call
            assert "_toolang__honor" not in {t.name for t in call.tools}
            messages = call.messages
            tool_positions = [i for i, m in enumerate(messages) if m.role == "tool"]
            rule_positions = [
                i for i, m in enumerate(messages) if "<rules " in message_text(m.parts)
            ]
            assert len(rule_positions) == 2 and min(rule_positions) > max(
                tool_positions
            )
            results = [
                p for m in messages for p in m.parts if isinstance(p, ToolResultPart)
            ]
            assert {p.tool_call_id for p in results} == {"first", "duplicate"}
            assert all("Unused rules." not in message_text(m.parts) for m in messages)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    # Replay consumes records, not the rule files or State publication.
    (repo / "AGENTS.md").unlink()
    (repo / "src/AGENTS.md").unlink()
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("change", ["update", "empty", "remove", "restore"])
def test_honor_rechecks_changes_including_empty_and_removed_rules(tmp_path, change):
    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(_call("first")),
            _calls(_call("changed")),
            _calls(_call("retry")),
            _answer(),
        ],
    )
    file = repo / "src/AGENTS.md"
    if change == "restore":
        # First present an actual empty file; then replace it with nonempty text.
        file.write_text("")

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepBegin)
                and event.kind == "model"
                and event.step.index == 2
            ):
                if change == "remove":
                    file.unlink()
                else:
                    file.write_text("" if change == "empty" else "Changed rules.")

    tracer = Tracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            controls = _recalls(harness, run)
            assert len(controls) == 3
            last = controls[-1].payload
            assert last.target == RulesRecallTarget("repo", "/src")
            assert last.content == (
                "" if change in {"empty", "remove"} else "Changed rules."
            )
            assert last.revision == (
                "0" if change == "remove" else sha256(last.content.encode()).hexdigest()
            )
            assert _tool_steps(harness, run)[-1].output.value.error is None
            assert (repo / "src/result").read_text() == "done"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("failure", ["symlink", "invalid_utf8", "directory"])
def test_failed_rule_reads_block_the_operation_without_retraction(tmp_path, failure):
    harness, repo, publication = _harness(
        tmp_path, [_calls(_call("blocked")), _answer()]
    )
    file = repo / "src/AGENTS.md"
    if failure == "invalid_utf8":
        file.write_bytes(b"\xff")
    else:
        file.unlink()
        if failure == "directory":
            file.mkdir()
        else:
            secret = tmp_path / "outside"
            secret.write_text("Never disclose this.")
            file.symlink_to(secret)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            (honor,) = _tool_steps(harness, run)
            assert honor.status == "failed"
            (original,) = _results(harness.adapter.invocations[-1].call.messages)
            assert "operation not executed" in original.error
            assert "were just loaded" not in original.error
            assert honor.given.summary == "Reloading workspace rules..."
            assert honor.noted.summary == "Failed to reload workspace rules"
            assert not _recalls(harness, run)
            assert not (repo / "src/result").exists()
            assert all(
                "Never disclose" not in message_text(m.parts)
                for m in harness.adapter.invocations[-1].call.messages
            )
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("event_type", [StepBegin, PartBegin, StepEnd])
@pytest.mark.parametrize("interrupt", ["cancel", "steer"])
def test_interrupted_honor_closes_every_announced_tool_call(
    tmp_path, event_type, interrupt
):
    harness, repo, publication = _harness(
        tmp_path, [_calls(_call("first"), _call("second")), _answer()]
    )
    handle = None
    interrupted = False

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal interrupted
            await super().on_event(event)
            if (
                not interrupted
                and isinstance(event, event_type)
                and event.step.index == 1
            ):
                interrupted = True
                assert handle is not None
                if interrupt == "cancel":
                    handle.cancel(timing="immediate")
                else:
                    handle.steer(
                        Message.user("Stop the operation."), timing="immediate"
                    )
                await asyncio.sleep(0)

    tracer = Tracer()

    async def scenario():
        nonlocal handle
        async with harness:
            handle = harness.executor.run(_spec(harness, publication), tracer=tracer)
            run = await handle
            assert run.status == (
                "canceled" if interrupt == "cancel" else "succeeded"
            ), run.error
            steps = _tool_steps(harness, run)
            assert [s.given.trigger for s in steps] == ["runtime", "model"]
            assert steps[1].output.value.tool_call_id == "second"
            assert steps[0].status == (
                "succeeded" if event_type is StepEnd else "canceled"
            )
            assert steps[1].status == "canceled"
            assert steps[1].noted.summary == "Canceled: Writing [repo] src/result"
            if event_type is StepEnd:
                assert "[repo] src/AGENTS.md" in steps[0].noted.summary
            else:
                assert steps[0].noted.summary == "Canceled: Reloading workspace rules"
            messages = harness.store.recent_conversation_messages(
                thread_id=run.thread.id
            )
            replies = _results(messages)
            assert [p.tool_call_id for p in replies] == ["first", "second"]
            if event_type is StepEnd:
                assert replies[0].error == RETRY_MESSAGE
            else:
                assert "could not be loaded" in replies[0].error
            if interrupt == "steer":
                assert (
                    _results(harness.adapter.invocations[-1].call.messages) == replies
                )
            assert replies[0].output == {}
            assert not (repo / "src/result").exists()
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_model_cannot_call_honor_and_unscoped_fs_is_rejected(tmp_path):
    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(
                _call(
                    "spoof",
                    "_toolang__honor",
                    paths=[{"workspace": "repo", "path": "/"}],
                ),
                _call("unscoped", path="notes", text="okay"),
            ),
            _answer(),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication))
            assert run.status == "succeeded", run.error
            steps = _tool_steps(harness, run)
            assert len(steps) == 2 and all(s.given.trigger == "model" for s in steps)
            assert "unknown tool call" in steps[0].output.value.error
            assert "agent home is not accessible" in steps[1].output.value.error
            assert not (repo.parent / "notes").exists()
            assert not _recalls(harness, run)

    asyncio.run(scenario())


def test_mixed_batch_and_changed_retry_leave_the_original_operation_unexecuted(
    tmp_path,
):
    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(_call("list", "fs__list", path="workspace://"), _call("blocked")),
            _calls(_call("changed", path="workspace://repo/src/other", text="changed")),
            _answer(),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            assert not (repo / "src/result").exists()
            assert (repo / "src/other").read_text() == "changed"
            results = _results(harness.adapter.invocations[1].call.messages)
            assert [part.tool_call_id for part in results] == ["list", "blocked"]
            assert results[0].error is None and results[1].error == RETRY_MESSAGE
            assert [step.given.call.name for step in _tool_steps(harness, run)] == [
                "fs__list",
                "_toolang__honor",
                "fs__write",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_parallel_children_keep_honor_dependencies_and_replies_local(tmp_path):
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
    harness, repo, publication = _harness(
        tmp_path,
        [
            *(
                ScriptedModelTurn(result=_calls(_call("blocked")), gate=gate)
                for gate in gates
            ),
            _answer(),
            _answer(),
            _answer(),
        ],
        source=source,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                replace(
                    harness.run_spec(
                        thread=thread,
                        runnable="parent",
                        primary=Message.user("start").parts,
                    ),
                    state=publication,
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates)),
                timeout=2,
            )
            for gate in gates:
                gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            children = [
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent is not None
            ]
            assert len(children) == 2
            for child in children:
                (honor,) = _tool_steps(harness, child)
                assert honor.given.trigger == "runtime"
                assert honor.input[0].record.run_id == child.id
                assert len(_recalls(harness, child)) == 2
                models = [
                    step
                    for step in harness.store.list_steps(run_id=child.id)
                    if step.kind == "model"
                ]
                (reply,) = _results(
                    harness.store.rebuild_model_call(models[-1]).messages
                )
                assert reply.tool_call_id == "blocked" and reply.error == RETRY_MESSAGE
            assert not (repo / "src/result").exists()
            next_run = await harness.executor.run(
                _spec(harness, publication, thread), tracer=tracer
            )
            assert next_run.status == "succeeded", next_run.error
            assert not _results(harness.adapter.invocations[-1].call.messages)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("saved_delta", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_restart_recovers_intercepted_reply_once_without_rule_files(
    tmp_path, saved_delta, failure
):
    harness, repo, publication = _harness(
        tmp_path, [_calls(_call("blocked")), _answer()]
    )
    if failure:
        (repo / "src/AGENTS.md").write_bytes(b"\xff")
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            request = _spec(harness, publication, thread)
            if not saved_delta:
                request = replace(
                    request, limits=replace(request.limits, agic_model_calls=1)
                )
            run = await harness.executor.run(request, tracer=tracer)
            assert run.status == ("succeeded" if saved_delta else "failed"), run.error
            assert len(_tool_steps(harness, run)) == 1
            assert not (repo / "src/result").exists()
            expected = _results(
                harness.store.recent_conversation_messages(thread_id=thread)
            )
            assert len(expected) == 1
            assert expected[0].tool_call_id == "blocked"
            assert expected[0].output == {}
            if failure:
                assert "could not be loaded" in expected[0].error
            else:
                assert expected[0].error == RETRY_MESSAGE
        (repo / "AGENTS.md").unlink()
        (repo / "src/AGENTS.md").unlink()
        store = RunStore(harness.store.db_path, read_only=True)
        try:
            assert (
                _results(store.recent_conversation_messages(thread_id=thread))
                == expected
            )
        finally:
            store.close()
        reopened = ExecutionHarness.create(
            tmp_path, source=SOURCE, state=publication, responses=[_answer(), _answer()]
        )
        async with reopened:
            for _ in range(2):
                next_run = await reopened.executor.run(
                    reopened.run_spec(thread=thread, runnable="chat"), tracer=tracer
                )
                assert next_run.status == "succeeded", next_run.error
                assert (
                    _results(reopened.adapter.invocations[-1].call.messages) == expected
                )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_overlapping_anchors_remain_independent_in_honor(tmp_path):
    repo_call = _call("repo")
    sdk_call = _call("sdk", workspace="sdk", path="/result", text="done")
    bare_call = _call("ambiguous", path="repo/src/result", text="bad")
    harness, repo, publication = _harness(
        tmp_path, [_calls(repo_call, sdk_call, bare_call), _answer()]
    )
    # Exercise rules identity independently of configured-root overlap validation.
    publication = replace(
        publication,
        home_config={"workspaces": {"repo": str(repo), "sdk": str(repo / "src")}},
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication))
            assert run.status == "succeeded", run.error
            controls = _recalls(harness, run)
            assert [c.payload.target for c in controls] == [
                RulesRecallTarget("repo", "/"),
                RulesRecallTarget("repo", "/src"),
                RulesRecallTarget("sdk", "/"),
            ]
            assert controls[1].payload.content == controls[2].payload.content
            assert (
                "specify workspace" in _tool_steps(harness, run)[-1].output.value.error
            )
            assert not (repo / "src/result").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("recall", ["auto", "none"])
def test_selected_near_determines_whether_next_run_needs_honor(tmp_path, recall):
    harness, repo, publication = _harness(
        tmp_path,
        [_calls(_call("first")), _answer(), _calls(_call("next")), _answer()],
        source=SOURCE.replace("context: none", f"recall = {recall}\n  context: none"),
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.run(_spec(harness, publication, thread))
            second = await harness.executor.run(_spec(harness, publication, thread))
            assert first.status == second.status == "succeeded"
            assert len(_recalls(harness, first)) == 2
            assert len(_recalls(harness, second)) == (0 if recall == "auto" else 2)
            assert (repo / "src/result").exists() == (recall == "auto")

    asyncio.run(scenario())


def test_pending_revisions_follow_a_b_a_order_and_deleted_rules_can_return(tmp_path):
    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(_call("a"), _call("b"), _call("a-again")),
            _calls(_call("removed")),
            _calls(_call("restored")),
            _calls(_call("retry")),
            _answer(),
        ],
    )
    file = repo / "src/AGENTS.md"

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool":
                changes = {1: "B", 2: "Scoped rules.", 5: "Restored"}
                if event.step.index in changes:
                    file.write_text(changes[event.step.index])
                elif event.step.index == 3:
                    file.unlink()

    tracer = Tracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            scoped = [
                c.payload
                for c in _recalls(harness, run)
                if c.payload.target == RulesRecallTarget("repo", "/src")
            ]
            assert [p.content for p in scoped] == [
                "Scoped rules.",
                "B",
                "Scoped rules.",
                "",
                "Restored",
            ]
            assert scoped[3].revision == "0"
            assert _tool_steps(harness, run)[-1].output.value.error is None
            assert (repo / "src/result").read_text() == "done"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_reload_changes_the_workspace_at_the_tool_boundary(tmp_path):
    next_publication = None

    async def refresh():
        assert next_publication is not None
        return StateRefresh(next_publication)

    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(_call("first")),
            _calls(
                ToolCall("reload", "reload", "_toolang__reload", {}), _call("changed")
            ),
            _calls(_call("retry")),
            _answer(),
        ],
        refresh_state=refresh,
    )
    new_repo = repo.with_name("new-repo")
    (new_repo / "src").mkdir(parents=True)
    (new_repo / "AGENTS.md").write_text("New root rules.")
    next_publication = _workspace_state(harness, {"repo": new_repo})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            assert not (repo / "src/result").exists()
            assert (new_repo / "src/result").read_text() == "done"
            payloads = [c.payload for c in _recalls(harness, run)]
            assert [p.content for p in payloads] == [
                "Root rules.",
                "Scoped rules.",
                "New root rules.",
                "",
            ]
            assert payloads[-1].revision == "0"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_honor_and_invocation_agree_after_symlink_parent_traversal(tmp_path):
    def call(identity):
        return _call(identity, workspace="repo", path="/link/../result", text="done")

    harness, repo, publication = _harness(
        tmp_path, [_calls(call("first")), _calls(call("retry")), _answer()]
    )
    (repo / "src/nested").mkdir()
    (repo / "link").symlink_to(repo / "src/nested", target_is_directory=True)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            assert [c.payload.target for c in _recalls(harness, run)] == [
                RulesRecallTarget("repo", "/"),
                RulesRecallTarget("repo", "/src"),
            ]
            assert (repo / "src/result").read_text() == "done"
            assert not (repo / "result").exists()
            honor, retried = _tool_steps(harness, run)
            assert honor.given.call.input == {
                "paths": [{"workspace": "repo", "path": "/src/result"}]
            }
            assert (
                _results(harness.adapter.invocations[1].call.messages)[0].error
                == RETRY_MESSAGE
            )
            assert retried.output.value.error is None
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_honor_preserves_a_prepared_directory_name_with_trailing_space(tmp_path):
    def call(identity):
        return _call(identity, "fs__list", path="workspace://repo/link")

    harness, repo, publication = _harness(
        tmp_path, [_calls(call("first")), _calls(call("retry")), _answer()]
    )
    directory = repo / "trailing "
    directory.mkdir()
    (directory / "AGENTS.md").write_text("Rules for the actual directory.")
    (repo / "link").symlink_to(directory, target_is_directory=True)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            assert [c.payload.target for c in _recalls(harness, run)] == [
                RulesRecallTarget("repo", "/"),
                RulesRecallTarget("repo", "/link"),
            ]
            honor, retried = _tool_steps(harness, run)
            assert honor.given.call.input == {
                "paths": [{"workspace": "repo", "path": "/link"}]
            }
            assert (
                _results(harness.adapter.invocations[1].call.messages)[0].error
                == RETRY_MESSAGE
            )
            assert retried.output.value.error is None
            assert retried.output.value.output["path"] == "workspace://repo/link"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
