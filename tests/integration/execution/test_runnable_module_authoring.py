"""Model-issued home edits publish discoverable, callable runnable modules."""

import asyncio
from hashlib import sha256

import pytest

from tests.support.execution_assertions import execution_snapshot
from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, ToolResultPart, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix
from toolang.plugin.toolsets.loading import load_tools


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("named", [False, True])
def test_model_creates_discovers_calls_edits_and_deletes_module(tmp_path, kind, named):
    key = "runnables/research.too"
    source = "agic manage():\n  tools = state/*\n  context = none\n  Manage modules.\n"
    module = (
        "## Research version one.\n"
        f"{kind}{' research' if named else ''}(_: Text) -> Text:\n"
        + ("  Version one: {{_}}\n" if kind == "agic" else "  run helper\n")
        + "agic helper(_: Text) -> Text:\n  Version one: {{_}}\n"
    )
    updated = module.replace("version one", "version two").replace(
        "Version one", "Version two"
    )
    broken = "agic ("
    original_digest = sha256(module.encode()).hexdigest()
    updated_digest = sha256(updated.encode()).hexdigest()
    responses = []
    expected_ids = []

    def call(id, tool, **arguments):
        expected_ids.append(id)
        responses.append(
            ModelCallResult(tool_calls=(ToolCall(id, id, tool, arguments),))
        )

    def invoke(id, ref, answer):
        call(id, "runtime__run", runnable=ref, input={"_": "input"})
        responses.append(ModelCallResult(message=Message.assistant(answer)))

    call("create", "state__create", key=key, content=module)
    call("list_created", "state__list")
    call("get_created", "state__get", key=key)
    call("unpublished", "runtime__runnables")
    call("publish", "state__sync")
    call("discover", "runtime__runnables")
    invoke("first", "research", "first result")
    call("update", "state__update", key=key, content=updated, if_digest=original_digest)
    call("stale", "state__update", key=key, content=module, if_digest=original_digest)
    call("list_updated", "state__list")
    call("get_updated", "state__get", key=key)
    call("before_sync", "runtime__runnables", name="research")
    invoke("before_sync_run", "research", "still first version")
    call("republish", "state__sync")
    call("rediscover", "runtime__runnables", name=f"{kind}:research")
    invoke("second", f"{kind}:research", "second result")
    call("break", "state__update", key=key, content=broken, if_digest=updated_digest)
    call("reject", "state__sync")
    call("get_broken", "state__get", key=key)
    call("after_reject", "runtime__runnables", name="research")
    call("after_reject_run", "runtime__run", runnable="research", input={"_": "input"})
    call(
        "repair",
        "state__update",
        key=key,
        content=updated,
        if_digest=sha256(broken.encode()).hexdigest(),
    )
    call("recover", "state__sync")
    call(
        "private_discovery",
        "runtime__runnables",
        name="runnables::research::agic:helper",
    )
    call(
        "private_run",
        "runtime__run",
        runnable="runnables::research::agic:helper",
        input={"_": "input"},
    )
    call("delete", "state__delete", key=key, if_digest=updated_digest)
    call("publish_delete", "state__sync")
    call("list_deleted", "state__list")
    call("discover_deleted", "runtime__runnables")
    call("get_deleted", "state__get", key=key)
    call("run_deleted", "runtime__run", runnable="research", input={"_": "input"})
    responses.append(ModelCallResult(message=Message.assistant("done")))
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        tools=load_tools(queries=("state/*",)),
        responses=responses,
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="manage",
                )
            )
            assert root.status == "succeeded", root.error
            results = {
                part.tool_call_id: part
                for step in harness.store.list_steps(run_id=root.id)
                if step.output is not None
                and isinstance(part := step.output.value, ToolResultPart)
            }
            assert list(results) == expected_ids
            expected_errors = {
                "stale",
                "after_reject",
                "after_reject_run",
                "private_discovery",
                "private_run",
                "get_deleted",
                "run_deleted",
            }
            assert {id for id, part in results.items() if part.error} == expected_errors
            values = {id: part.output for id, part in results.items()}

            for suffix, content, digest in (
                ("created", module, original_digest),
                ("updated", updated, updated_digest),
            ):
                assert values[f"get_{suffix}"] == {
                    "key": key,
                    "digest": digest,
                    "content": content,
                    "encoding": "utf-8",
                    "bytes": len(content.encode()),
                }
                assert values[f"list_{suffix}"]["files"] == [
                    {
                        "key": "agent.too",
                        "digest": sha256(source.encode()).hexdigest(),
                        "bytes": len(source.encode()),
                    },
                    {"key": key, "digest": digest, "bytes": len(content.encode())},
                ]
            assert values["stale"]["error"] == "digest_mismatch"
            rejected_file = next(
                file for file in values["reject"]["files"] if file["key"] == key
            )
            assert rejected_file["accepted_digest"] is None
            assert rejected_file["code"] == "invalid-program"
            assert values["get_broken"]["content"] == broken
            assert values["get_deleted"]["error"] == "not_found"

            initial_revision = harness.state.revision
            first_revision = values["publish"]["revision"]
            second_revision = values["republish"]["revision"]
            assert len({initial_revision, first_revision, second_revision}) == 3
            assert values["reject"]["revision"] != second_revision
            assert values["recover"]["revision"] == second_revision
            assert values["publish_delete"]["revision"] == initial_revision
            assert {
                "scope": "home",
                "key": key,
                "observed_digest": original_digest,
                "accepted_digest": original_digest,
            } in (values["publish"]["files"])
            assert {
                "scope": "home",
                "key": key,
                "observed_digest": updated_digest,
                "accepted_digest": updated_digest,
            } in (values["republish"]["files"])
            assert [item["key"] for item in values["list_deleted"]["files"]] == [
                "agent.too"
            ]
            assert not (harness.setup.layout.home / key).exists()

            for id in ("unpublished", "discover_deleted"):
                assert [e["ref"] for e in values[id]["runnables"]] == ["agic:manage"]
            assert {e["ref"] for e in values["discover"]["runnables"]} == {
                "agic:manage",
                f"{kind}:research",
            }
            for id, revision, doc in (
                ("discover", first_revision, "Research version one."),
                ("before_sync", first_revision, "Research version one."),
                ("rediscover", second_revision, "Research version two."),
            ):
                entry = next(
                    e for e in values[id]["runnables"] if e["ref"] == f"{kind}:research"
                )
                assert entry["revision"] == revision
                assert entry["doc"] == doc

            children = [
                invocation.call
                for invocation in harness.adapter.invocations
                if execution_snapshot(invocation.call)["runnable"]
                != "agent::agic:manage"
            ]
            assert len(children) == 3
            for call, version in zip(children, ("one", "one", "two")):
                text = " ".join(message_text(m.parts) for m in call.messages)
                assert f"Version {version}: input" in text
            for id, output in (
                ("first", "first result"),
                ("before_sync_run", "still first version"),
                ("second", "second result"),
            ):
                assert values[id] == {"type": "Text", "value": output}
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == (4 if kind == "agic" else 7)
            assert all(run.status == "succeeded" for run in runs)
            assert harness.store.resolve_state_revision(root.state) == initial_revision
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())
