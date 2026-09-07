"""Workspace URI calls follow Tool Step States and durable honor results."""

import asyncio
from dataclasses import replace

import pytest

from tests.integration.execution.test_honor_rules import (
    SOURCE,
    _answer,
    _call,
    _calls,
    _harness,
    _recalls,
    _spec,
    _tool_steps,
    _workspace_state,
)
from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import TextPart, message_text
from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolPreparation
from toolang.execution.types import RulesRecallTarget, ThreadPrefix
from toolang.plugin.toolsets.filesystem import _FilesystemTool
from toolang.state.watcher import StateRefresh


def _results(harness, run):
    return {
        s.output.value.tool_call_id: s.output.value for s in _tool_steps(harness, run)
    }


def test_external_workspace_rules_and_protocol_survive_instruct_none(tmp_path):
    uri = "workspace://external/file"
    harness, _repo, _pub = _harness(
        tmp_path,
        [
            _calls(_call("first", path=uri, text="done")),
            _calls(_call("retry", path=uri, text="done")),
            _answer(),
        ],
        source=SOURCE.replace("context: none", "instruct: none\n  context: none"),
    )
    external = tmp_path / "external"
    external.mkdir()
    (external / "AGENTS.md").write_text("Log changes in this workspace.")
    publication = _workspace_state(harness, {"external": external})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            results = _results(harness, run)
            assert results["first"].error == "operation not executed; retry required"
            assert results["retry"].error is None
            assert results["retry"].output["path"] == uri
            controls = _recalls(harness, run)
            assert len(controls) == 1
            assert controls[0].payload.target == RulesRecallTarget("external", "/")
            assert "<filesystem>" in harness.adapter.invocations[0].call.instructions
            assert str(external) not in harness.adapter.invocations[0].call.instructions
            assert any(
                '<rules workspace="external"' in message_text(message.parts)
                for message in harness.adapter.invocations[1].call.messages
            )
            assert (external / "file").read_text() == "done"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    (external / "AGENTS.md").unlink()
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("external", [False, True])
def test_rule_symlinks_cannot_escape_the_workspace(tmp_path, external):
    harness, _repo, _pub = _harness(
        tmp_path,
        [_calls(_call("write", path="workspace://repo/file", text="bad")), _answer()],
    )
    root = (tmp_path if external else harness.setup.layout.home) / "workspace"
    root.mkdir()
    secret = harness.setup.layout.home / "secret"
    secret.write_text("Outside rules.")
    (root / "AGENTS.md").symlink_to(secret)

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                _spec(harness, _workspace_state(harness, {"repo": root}))
            )
            assert "rules recall failed" in _results(harness, run)["write"].error
            assert not _recalls(harness, run)
            assert not (root / "file").exists()

    asyncio.run(scenario())


def test_explicit_workspace_cannot_create_an_unavailable_root_in_home(tmp_path):
    harness, _repo, _pub = _harness(
        tmp_path,
        [
            _calls(_call("write", workspace="missing", path="file", text="done")),
            _answer(),
        ],
    )
    missing = harness.setup.layout.home / "missing"

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                _spec(harness, _workspace_state(harness, {"missing": missing}))
            )
            assert "not available" in _results(harness, run)["write"].error
            assert not missing.exists()

    asyncio.run(scenario())


def test_remove_symlink_honors_rules_then_unlinks_without_removing_the_target(tmp_path):
    arguments = {"path": "workspace://repo/alias", "recursive": True}
    harness, repo, publication = _harness(
        tmp_path,
        [
            _calls(_call("first", "fs__remove", **arguments)),
            _calls(_call("retry", "fs__remove", **arguments)),
            _answer(),
        ],
    )
    link = repo / "alias"
    link.symlink_to(repo / "src", target_is_directory=True)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication), tracer=tracer)
            assert run.status == "succeeded", run.error
            results = _results(harness, run)
            assert results["first"].error == "operation not executed; retry required"
            assert results["retry"].error is None
            assert results["retry"].output == {
                "path": arguments["path"],
                "removed": True,
            }
            assert [c.payload.target for c in _recalls(harness, run)] == [
                RulesRecallTarget("repo", "/"),
                RulesRecallTarget("repo", "/alias"),
            ]
            assert not link.is_symlink()
            assert (repo / "src/AGENTS.md").read_text() == "Scoped rules."
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_fs_protocol_follows_effective_tools(tmp_path):
    harness, _repo, publication = _harness(
        tmp_path,
        [_answer()],
        source=SOURCE.replace("context: none", "tools = shell/*\n  context: none"),
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, publication))
            assert run.status == "succeeded", run.error
            call = harness.adapter.invocations[0].call
            assert all(not tool.name.startswith("fs__") for tool in call.tools)
            assert "<filesystem>" not in call.instructions

    asyncio.run(scenario())


def test_reload_updates_revision_listing_grants_and_mapping_in_one_run(tmp_path):
    changed = None

    async def refresh():
        assert changed is not None
        return StateRefresh(changed)

    harness, _repo, _pub = _harness(
        tmp_path,
        [
            _calls(
                _call("before-list", "fs__list", path="workspace://"),
                _call("before-write", path="workspace://moving/file", text="before"),
            ),
            _calls(
                ToolCall("reload", "reload", "_toolang__reload", {}),
                _call("after-list", "fs__list", path="workspace://"),
                _call("removed", path="workspace://removed/file", text="bad"),
                _call("moved", path="workspace://moving/file", text="after"),
                _call("added", path="workspace://added/file", text="new"),
            ),
            _answer(),
        ],
        refresh_state=refresh,
    )
    roots = {name: tmp_path / name for name in ("old", "new", "removed", "added")}
    for root in roots.values():
        root.mkdir()
    initial = _workspace_state(
        harness, {"moving": roots["old"], "removed": roots["removed"]}
    )
    changed = _workspace_state(
        harness, {"moving": roots["new"], "added": roots["added"]}
    )
    assert initial.revision != changed.revision
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            results = _results(harness, run)
            assert results["reload"].error is None
            assert [e["name"] for e in results["before-list"].output["entries"]] == [
                "moving",
                "removed",
            ]
            assert [e["name"] for e in results["after-list"].output["entries"]] == [
                "added",
                "moving",
            ]
            assert "not available" in results["removed"].error
            assert results["moved"].error is results["added"].error is None
            assert (roots["old"] / "file").read_text() == "before"
            assert (roots["new"] / "file").read_text() == "after"
            assert (roots["added"] / "file").read_text() == "new"
            assert not (roots["removed"] / "file").exists()
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("change", ["remove", "remap", "remap-without-rules"])
def test_honor_retry_resolves_the_new_workspace_state(tmp_path, change):
    changed = None

    async def refresh():
        assert changed is not None
        return StateRefresh(changed)

    uri = "workspace://repo/file"
    harness, _repo, _pub = _harness(
        tmp_path,
        [
            _calls(_call("first", path=uri, text="first")),
            _calls(
                ToolCall("reload", "reload", "_toolang__reload", {}),
                _call("changed", path=uri, text="changed"),
            ),
            _calls(_call("retry", path=uri, text="done")),
            _answer(),
        ],
        refresh_state=refresh,
    )
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "AGENTS.md").write_text("Old rules.")
    if change == "remap":
        (new / "AGENTS.md").write_text("New rules.")
    initial = _workspace_state(harness, {"repo": old})
    changed = _workspace_state(harness, {} if change == "remove" else {"repo": new})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(_spec(harness, initial), tracer=tracer)
            assert run.status == "succeeded", run.error
            results = _results(harness, run)
            assert results["reload"].error is None
            controls = _recalls(harness, run)
            assert controls[0].payload.content == "Old rules."
            assert not (old / "file").exists()
            if change == "remove":
                assert "not available" in results["changed"].error
                assert "not available" in results["retry"].error
                assert not (new / "file").exists()
                assert len(controls) == 1
            else:
                assert (
                    results["changed"].error == "operation not executed; retry required"
                )
                assert results["retry"].error is None
                assert len(controls) == 2
                if change == "remap":
                    assert controls[-1].payload.content == "New rules."
                else:
                    assert controls[-1].payload.revision == "0"
                assert (new / "file").read_text() == "done"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_reload_during_a_tool_keeps_its_path_and_updates_the_next_step(
    tmp_path, monkeypatch
):
    gate = AsyncGate()
    original_prepare = _FilesystemTool.prepare

    def prepare(self, arguments, context):
        prepared = original_prepare(self, arguments, context)
        if self.name != "write" or gate.entered:
            return prepared

        async def invoke():
            await gate.wait()
            return await prepared.invoke()

        return ToolPreparation(prepared.paths, invoke)

    monkeypatch.setattr(_FilesystemTool, "prepare", prepare)
    uri = "workspace://repo/file"
    harness, _repo, _pub = _harness(
        tmp_path,
        [
            _calls(_call("before", path=uri, text="old")),
            _calls(_call("after", path=uri, text="new")),
            _answer(),
        ],
    )
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    initial = _workspace_state(harness, {"repo": old})
    changed = _workspace_state(harness, {"repo": new})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(_spec(harness, initial), tracer=tracer)
            try:
                await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
                control = handle.reload(changed)
                await harness.executor._apply_reload_controls(
                    harness.executor._active[handle.run_id]
                )
            finally:
                gate.release()
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "succeeded", run.error
            assert (old / "file").read_text() == "old"
            assert (new / "file").read_text() == "new"
            before, after = _tool_steps(harness, run)
            assert before.state != control.ref
            assert after.state == control.ref
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_parallel_children_recheck_workspace_rules_after_root_reload(tmp_path):
    initial_gates = (AsyncGate(), AsyncGate())
    gates = (AsyncGate(), AsyncGate())
    uri = "workspace://repo/file"
    source = """
agic chat(_: Part[]) -> Text:
  recall = none
  context: none
  user: Complete the task.

flow parent(_: Part[]) -> Text[]:
  storm 2 using chat in 2 lanes
"""
    harness, _repo, _pub = _harness(
        tmp_path,
        [
            *(
                ScriptedModelTurn(
                    result=_calls(_call(f"first-{i}", path=uri, text="old")),
                    gate=gate,
                )
                for i, gate in enumerate(initial_gates)
            ),
            *(
                ScriptedModelTurn(
                    result=_calls(_call(f"retry-{i}", path=uri, text="new")), gate=gate
                )
                for i, gate in enumerate(gates)
            ),
            _answer(),
            _answer(),
        ],
        source=source,
    )
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "AGENTS.md").write_text("Old rules.")
    (new / "AGENTS.md").write_text("New rules.")
    initial = _workspace_state(harness, {"repo": old})
    changed = _workspace_state(harness, {"repo": new})
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            spec = replace(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    primary=(TextPart("start"),),
                ),
                state=initial,
            )
            handle = harness.executor.run(spec, tracer=tracer)
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(gate.wait_until_entered() for gate in initial_gates)
                    ),
                    timeout=2,
                )
                for gate in initial_gates:
                    gate.release()
                await asyncio.wait_for(
                    asyncio.gather(*(gate.wait_until_entered() for gate in gates)),
                    timeout=2,
                )
                handle.reload(changed)
                await harness.executor._apply_reload_controls(
                    harness.executor._active[handle.run_id]
                )
            finally:
                for gate in (*initial_gates, *gates):
                    gate.release()
            parent = await asyncio.wait_for(handle, timeout=2)
            assert parent.status == "succeeded", parent.error
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=parent.id)
                if r.parent is not None
            ]
            assert len(children) == 2
            assert not _recalls(harness, parent)
            for child in children:
                assert [c.payload.content for c in _recalls(harness, child)] == [
                    "Old rules.",
                    "New rules.",
                ]
            assert not (old / "file").exists()
            assert not (new / "file").exists()
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
