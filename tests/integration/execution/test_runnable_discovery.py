"""Model-callable discovery keeps bound identities, full docs, and state revisions."""

from __future__ import annotations

import asyncio
from xml.etree import ElementTree

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    last_tool_result,
    route_scopes,
    runtime_message,
    without_runtime_snapshots,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.types import ThreadPrefix, ToolStepGiven
from toolang.state.prepare import prepare_agent_state


def query(arguments=None, *, id="query"):
    return ModelCallResult(
        tool_calls=(ToolCall(id, id, "_toolang__runnables", arguments or {}),)
    )


def answer():
    return ModelCallResult(message=Message.assistant("done"))


@pytest.mark.parametrize(
    "name", [None, "target", "agic:target", "agent::agic:target", "chat"]
)
def test_named_and_all_discovery_return_full_docs_even_with_no_routes(tmp_path, name):
    doc = 'A long description & "literal" text. ' * 30
    source = f"""
## {doc}
struct Node:
  ## {doc}
  text: Text
  next?: Node

## {doc}
## @param _ {doc}
## @param count {doc}
agic target(_: Node, count?: Number) -> Node:
  Do not return this source body.

flow empty():
  pass

agic chat() -> Text:
  hands = none
  handoffs = none
  user: Inspect available parameters.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[query({"name": name} if name else {}), answer()],
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
            first, following = [i.call for i in harness.adapter.invocations]
            result = last_tool_result(following)
            assert result.error is None
            value = result.output
            assert value["current"] == "agic:chat" and value["ancestors"] == []
            entries = value["runnables"]
            assert [e["ref"] for e in entries] == (
                ["agic:chat", "agic:target", "flow:empty"]
                if name is None
                else ["agic:chat" if name == "chat" else "agic:target"]
            )
            assert {e["revision"] for e in entries} == {harness.state.revision}
            if name != "chat":
                entry = next(e for e in entries if e["ref"] == "agic:target")
                assert entry["doc"] == doc.strip()
                signature = entry["signature"]
                assert signature["input"] == {
                    "type": "Node",
                    "optional": False,
                    "documentation": doc.strip(),
                }
                assert signature["parameters"] == [
                    {
                        "name": "count",
                        "type": "Number",
                        "optional": True,
                        "documentation": doc.strip(),
                    }
                ]
                assert signature["output"] == "Node"
                assert signature["structs"] == [
                    {
                        "name": "Node",
                        "documentation": doc.strip(),
                        "fields": [
                            {
                                "name": "text",
                                "type": "Text",
                                "optional": False,
                                "documentation": doc.strip(),
                            },
                            {
                                "name": "next",
                                "type": "Node",
                                "optional": True,
                                "documentation": "",
                            },
                        ],
                    }
                ]
                assert "Do not return this source body." not in str(value)
            if name is None:
                empty = entries[-1]["signature"]
                assert empty["input"] is None and empty["parameters"] == []
            assert route_scopes(first) == dict.fromkeys(
                ("hands", "handoffs", "spawns"), "NONE"
            )
            assert without_runtime_snapshots(first.messages) == [
                Message.user("Inspect available parameters.")
            ]
            for index, call in enumerate((first, following), start=1):
                batches = [m for m in call.messages if m.tag == "workspace"]
                assert (
                    len(batches) == index
                )  # One new batch each call; older calls remain replayable.
                batch = runtime_message(call)
                root = ElementTree.fromstring(
                    '<root xmlns:toolang="urn:test">'
                    + message_text(batch.parts)
                    + "</root>"
                )
                assert [n.tag.removeprefix("{urn:test}") for n in root] == [
                    "workspace",
                    "workdir",
                    "routes",
                    "context",
                    "execution",
                ]
                assert all(n.text is None for n in root)
                assert set(root[3].attrib) == {
                    "date",
                    "timezone",
                    "model_provider",
                    "model_name",
                }
                assert doc not in message_text(batch.parts)
            assert len(harness.store.list_run_tree(root_run_id=run.id)) == 1

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": ""},
        {"name": " "},
        {"name": None},
        {"name": "missing"},
        {"name": "*"},
        {"name": "ALL"},
        {"name": "chat "},
        {"names": ["chat"]},
        {"name": "flows::hidden::agic:private"},
    ],
)
def test_invalid_or_inaccessible_queries_fail_without_broadening(tmp_path, arguments):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat():\n  Inspect.\n",
        responses=[query(arguments), answer()],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert result.tool_call_id == "query" and result.error
            assert result.output == {}

    asyncio.run(scenario())


def test_current_and_ancestors_follow_exec_without_repeating_leaf(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow outer():
  run inner
flow inner():
  run worker
agic worker():
  Inspect then hand off.
agic target():
  Inspect current identity.
""",
        responses=[
            query(),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "transfer", "transfer", "_toolang__exec", {"runnable": "target"}
                    ),
                )
            ),
            query(id="target-query"),
            answer(),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="outer",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            calls = [i.call for i in harness.adapter.invocations]
            for call, current in ((calls[1], "agic:worker"), (calls[3], "agic:target")):
                value = last_tool_result(call).output
                assert value["current"] == current
                assert value["ancestors"] == ["flow:outer", "flow:inner"]
                assert {"flow:outer", "flow:inner", current} <= {
                    e["ref"] for e in value["runnables"]
                }

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("removed", [False, True])
def test_current_uses_bound_declaration_and_others_use_tool_step_state(
    tmp_path, removed
):
    source = "## Bound current doc.\nagic chat():\n  Inspect.\n## Old target doc.\nflow target():\n  pass\n"
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(query(), gate=gate),
            query({"name": "chat"}, id="current"),
            answer(),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            pending = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 5)
            updated = "## New target doc.\nflow target():\n  pass\n"
            if not removed:
                updated += "## New current doc.\nagic chat(_: Number):\n  New body.\n"
            harness.setup.layout.program.write_text(updated)
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            run = await pending
            assert run.status == "succeeded", run.error
            calls = [i.call for i in harness.adapter.invocations]
            entries = {
                e["ref"]: e for e in last_tool_result(calls[1]).output["runnables"]
            }
            assert set(entries) == {"agic:chat", "flow:target"}
            assert entries["agic:chat"]["doc"] == "Bound current doc."
            assert entries["agic:chat"]["revision"] == harness.state.revision
            assert entries["agic:chat"]["signature"]["input"] is None
            assert entries["flow:target"]["doc"] == "New target doc."
            assert entries["flow:target"]["revision"] == harness.published.revision
            assert last_tool_result(calls[2]).output["runnables"] == [
                entries["agic:chat"]
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_discovery_state_read_error_is_a_correlated_tool_failure(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat():\n  Inspect.\n",
        responses=[query(), answer()],
    )
    fail_next_read = False

    def latest():
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise ToolangError("Published State temporarily unavailable")
        return harness.state

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal fail_next_read
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "model":
                fail_next_read = True

    harness.executor._state = latest
    tracer = Tracer()
    harness.intercept_events(tracer)

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
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert result.tool_call_id == "query"
            assert result.error == "Published State temporarily unavailable"
            assert result.output == {}

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_discovery_keeps_the_state_captured_before_tool_step_delivery(tmp_path):
    source = "agic chat():\n  Inspect.\n## Before.\nflow target():\n  pass\n"
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            query({"name": "target"}),
            query({"name": "target"}, id="later"),
            answer(),
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepBegin)
                and isinstance(event.given, ToolStepGiven)
                and event.given.call.name == "_toolang__runnables"
                and harness.published is None
            ):
                harness.setup.layout.program.write_text(
                    source.replace("Before.", "After.")
                )
                harness.published = prepare_agent_state(harness.setup.layout)

    tracer = Tracer()
    harness.intercept_events(tracer)

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
            assert harness.published is not None
            entries = [
                last_tool_result(i.call).output["runnables"][0]
                for i in harness.adapter.invocations[1:]
            ]
            assert [entry["doc"] for entry in entries] == ["Before.", "After."]
            assert [entry["revision"] for entry in entries] == [
                harness.state.revision,
                harness.published.revision,
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_private_module_discovery_retains_aliases_and_hides_other_private_docs(
    tmp_path,
):
    home = tmp_path / "agents/alice"
    (home / "flows").mkdir(parents=True)
    source = "flow main():\n  run research\nagic worker():\n  Public helper.\n"
    (home / "agent.too").write_text(source)
    (home / "flows/research.too").write_text(
        "flow():\n  run worker\n## Local helper.\nagic worker():\n  Inspect.\n"
    )
    (home / "flows/other.too").write_text(
        "flow():\n  run worker\n## Private secret description.\nagic worker():\n  Help.\n"
    )
    state = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=state,
        program=state.modules["agent"],
        responses=[
            query(),
            query({"name": "flows::research::agic:worker"}, id="local"),
            query({"name": "flow:research"}, id="alias"),
            answer(),
        ],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                )
            )
            assert run.status == "succeeded", run.error
            values = [
                last_tool_result(i.call).output for i in harness.adapter.invocations[1:]
            ]
            assert values[0]["current"] == "flows::research::agic:worker"
            assert values[0]["ancestors"] == ["flow:main", "flow:research"]
            assert [e["ref"] for e in values[0]["runnables"]] == [
                "flow:research",
                "flows::research::agic:worker",
            ]
            assert "Private secret description." not in str(values)
            assert values[1]["runnables"] == [values[0]["runnables"][1]]
            assert values[2]["runnables"] == [values[0]["runnables"][0]]

    asyncio.run(scenario())


@pytest.mark.parametrize("current", ["worker", "research"])
def test_discovery_keeps_export_and_same_named_private_agic(tmp_path, current):
    home = tmp_path / "agents/alice"
    (home / "flows").mkdir(parents=True)
    source = "flow main():\n  run research\n"
    (home / "agent.too").write_text(source)
    (home / "flows/research.too").write_text(
        "## Exported flow.\nflow():\n  exec worker\n"
        "## Private namesake.\nagic research():\n  Help.\n"
        "agic worker():\n  Inspect.\n"
    )
    state = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=state,
        program=state.modules["agent"],
        responses=[
            *(
                [
                    ModelCallResult(
                        tool_calls=(
                            ToolCall(
                                "transfer",
                                "transfer",
                                "_toolang__exec",
                                {"runnable": "agic:research"},
                            ),
                        )
                    )
                ]
                if current == "research"
                else []
            ),
            query(),
            query({"name": "flow:research"}, id="export"),
            query({"name": "flows::research::agic:research"}, id="private"),
            answer(),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            results = [
                last_tool_result(i.call) for i in harness.adapter.invocations[-3:]
            ]
            assert all(result.error is None for result in results)
            assert all(
                result.output["current"] == f"flows::research::agic:{current}"
                for result in results
            )
            entries = {e["ref"]: e for e in results[0].output["runnables"]}
            assert set(entries) == {
                "flow:research",
                "flows::research::agic:research",
                "flows::research::agic:worker",
            }
            for result, ref, doc in (
                (results[1], "flow:research", "Exported flow."),
                (results[2], "flows::research::agic:research", "Private namesake."),
            ):
                assert result.output["runnables"] == [entries[ref]]
                assert entries[ref]["doc"] == doc

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_exec_removes_old_restrictions_without_rewriting_prior_batches(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat():
  hands = none
  handoffs = target
  context = none
  Hand off.
agic target():
  context = none
  Inspect.
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "transfer", "transfer", "_toolang__exec", {"runnable": "target"}
                    ),
                )
            ),
            query(),
            answer(),
        ],
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
            first, target, _ = [i.call for i in harness.adapter.invocations]
            assert route_scopes(first) == {
                "hands": "NONE",
                "handoffs": "agic:target",
                "spawns": "NONE",
            }
            assert route_scopes(target) == dict.fromkeys(
                ("hands", "handoffs", "spawns"), "ALL"
            )
            assert "<toolang:routes" not in message_text(runtime_message(target).parts)
            # Exec replaces the current window; persisted calls retain the old batch.
            assert 'hands="NONE"' in message_text(runtime_message(first).parts)
            assert (
                last_tool_result(harness.adapter.invocations[2].call).output["current"]
                == "agic:target"
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_all_discovery_has_no_64_target_limit(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat():\n  Inspect.\n"
        + "\n".join(
            f"## Description {i}.\nflow action_{i:03d}():\n  pass\n" for i in range(80)
        ),
        responses=[query(), answer()],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                )
            )
            assert run.status == "succeeded", run.error
            result = last_tool_result(harness.adapter.invocations[1].call)
            assert result.error is None
            assert [e["ref"] for e in result.output["runnables"]] == [
                "agic:chat",
                *(f"flow:action_{i:03d}" for i in range(80)),
            ]

    asyncio.run(scenario())


def test_new_root_omits_routes_even_when_history_contains_restricted_batch(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat():\n  hands = none\n  handoffs = none\n  context = none\n  Work.\n"
        "agic target():\n  context = none\n  Inspect.\n",
        responses=[answer(), query(), answer()],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            for runnable in ("chat", "target"):
                run = await harness.executor.run(
                    harness.run_spec(
                        thread=thread,
                        runnable=runnable,
                    ),
                    tracer=tracer,
                )
                assert run.status == "succeeded", run.error
            first, second, _ = [i.call for i in harness.adapter.invocations]
            assert runtime_message(first) in second.messages
            assert route_scopes(first) == dict.fromkeys(
                ("hands", "handoffs", "spawns"), "NONE"
            )
            assert route_scopes(second) == dict.fromkeys(
                ("hands", "handoffs", "spawns"), "ALL"
            )
            assert "<toolang:routes" not in message_text(runtime_message(second).parts)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
