"""Flow contracts, inherited frames, and iteration history at execution boundaries.

Authored clauses are parsed from source through the installed grammar.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.execution_assertions import without_runtime_snapshots
from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.execution.events import RunBegin
from toolang.base.types.message import ImagePart, Message, Part, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix
from toolang.lang import Program
from toolang.lang.errors import ToolangError


def _create(
    root: Path, *, source: str, responses: list[str], program: Program | None = None
) -> ExecutionHarness:
    return ExecutionHarness.create(
        root,
        source=source,
        program=program,
        responses=[
            ModelCallResult(message=Message.assistant(text)) for text in responses
        ],
    )


def _run(harness: ExecutionHarness, *, primary: str | None = None):
    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:main",
                    primary=(TextPart(primary),) if primary is not None else None,
                )
            )
            output = (
                harness.store.run_output_text(run_id=run.id)
                if run.output is not None
                else None
            )
            return (
                run,
                output,
                harness.store.resolve_error(run.error) if run.error else None,
            )

    return asyncio.run(scenario())


def _texts(harness: ExecutionHarness) -> list[str]:
    return [
        "\n".join(message_text(message.parts) for message in invocation.call.messages)
        for invocation in harness.adapter.invocations
    ]


@pytest.mark.parametrize(
    "operation,output",
    [
        ("map using transform", "Text[]"),
        ("keep if predicate", "Number[]"),
        ("drop if predicate", "Number[]"),
        ("sort ascending by score", "Number[]"),
        ("keep first 2", "Number[]"),
        ("drop last 2", "Number[]"),
    ],
)
def test_empty_lists_preserve_types_without_child_calls(
    tmp_path: Path, operation: str, output: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Number[]:
  Seed.
agic transform -> Text:
  Transform.
agic predicate -> Boolean:
  Decide.
agic score -> Number:
  Score.
flow main() -> {output}:
  run seed
  {operation}
""",
        responses=["[]"],
    )
    expected_type = output
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert run.output is not None and run.output.type == expected_type
    assert output == "[]"
    assert len(harness.adapter.invocations) == 1


@pytest.mark.parametrize("operation", ["reduce"])
def test_empty_reductions_fail_before_child_calls(
    tmp_path: Path, operation: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Text[]:
  Seed.
agic reduce:
  Reduce.
flow main():
  run seed
  {operation} using reduce
""",
        responses=["[]"],
    )
    run, output, error = _run(harness)
    assert run.status == "failed"
    assert "nonempty array" in str(error)
    assert len(harness.adapter.invocations) == 1


def test_scatter_and_storm_can_omit_primary_and_keep_nested_arrays(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main() -> Text[][]:
  run -> Text[]:
    Make two strings.
  generate 2 in 1 lane -> Text[]:
    Make a pair.
""",
        responses=['["ignored"]', '["a","b"]', '["c"]'],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert output == '[["a","b"],["c"]]'


def test_settle_uses_first_element_then_passes_current_and_previous_output(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed() -> Text[]:
  Seed.
flow main():
  run seed
  reduce:
    current={{_}}; previous={{_1._}}
""",
        responses=['["a","b","c"]', "ab", "abc"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", run.error
    assert "current=b; previous=a" in _texts(harness)[1]
    assert "current=c; previous=ab" in _texts(harness)[2]
    assert output == "abc"


def test_settle_keeps_leading_markdown_in_the_model_prompt(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed() -> Text[]:
  Seed.
flow main():
  run seed
  reduce:
    # Reducer instructions
    ## Preserve this heading
    Combine {{_}} with {{_1._}}.
    from: Initial.
""",
        responses=['["item"]', "combined"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "combined"
    assert (
        "# Reducer instructions\n## Preserve this heading\nCombine item with Initial."
        in _texts(harness)[1]
    )


@pytest.mark.parametrize(
    "initial,output,responses,expected",
    [
        (None, "Text", ['["one"]'], "one"),
        ("not a number", "Number", ["[]"], None),
        ("start", "Text", ['["one"]', "start+one"], "start+one"),
        ("not a number", "Number", ['["one"]'], None),
        (None, "Number", ['["one"]'], None),
    ],
)
def test_settle_seed_contract_and_singleton(
    tmp_path: Path,
    initial: str | None,
    output: str,
    responses: list[str],
    expected: str | None,
) -> None:
    source = f"""
agic seed() -> Text[]:
  Seed.
agic reduce -> {output}:
  user: current={{{{_}}}}; previous={{{{_1._}}}}
flow main() -> {output}:
  run seed
  reduce using reduce
"""
    if initial is not None:
        source = source.replace(
            "  reduce using reduce\n", f"  reduce using reduce:\n    from: {initial}\n"
        )
    if initial is None and output != "Text":
        with pytest.raises(
            ToolangError, match="Reduce without an initializer requires Text output"
        ):
            Program.from_source(source)
        return
    harness = _create(tmp_path, source=source, responses=responses)
    run, output, error = _run(harness)
    assert run.status == ("succeeded" if expected is not None else "failed"), run.error
    if expected is not None:
        assert output == expected
    else:
        assert "Number" in str(error)
    assert len(harness.adapter.invocations) == len(responses)


@pytest.mark.parametrize("initial", [None, "initial"])
@pytest.mark.parametrize("items", ['["seed"]', '["seed","2","3"]'])
def test_settle_only_validates_consumed_elements_as_reducer_inputs(
    tmp_path: Path, initial: str | None, items: str
) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
agic reduce(_: Number) -> Text:
  user: Add {{_}} to {{_1._}}.
flow main():
  run seed
  reduce using reduce
"""
    if initial is not None:
        source = source.replace(
            "  reduce using reduce\n", f"  reduce using reduce:\n    from: {initial}\n"
        )
    harness = _create(
        tmp_path,
        source=source,
        responses=[items, "seed+2", "seed+2+3"],
    )
    run, output, error = _run(harness)
    if initial is not None:
        assert run.status == "failed"
        assert "Number" in str(error)
        assert len(harness.adapter.invocations) == 1
    else:
        assert run.status == "succeeded", error
        singleton = items == '["seed"]'
        assert output == ("seed" if singleton else "seed+2+3")
        assert len(harness.adapter.invocations) == (1 if singleton else 3)


def test_until_waits_for_its_own_history_depth_and_reads_entry_exit(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main -> Text:
  repeat 5 times:
    run: Improve {{_}}.
    until:
      now={{_}}; previous={{_1._}}; older={{_2._}}; entry={{_2.__}}
""",
        responses=["a", "b", "c", "true"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert len(harness.adapter.invocations) == 4
    assert "now=c; previous=b; older=a; entry=seed" in _texts(harness)[-1]
    assert output == "c"


def test_until_captures_boolean_results_without_text_coercion(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
agic check() -> Boolean:
  Check.
flow main():
  repeat 3 times:
    let ready = run check
    until: Ready={{ready}}.
  run: Finished.
""",
        responses=["false", "false", "true", "true", "done"],
    )
    run, output, error = _run(harness)

    assert run.status == "succeeded", error
    assert output == "done"
    assert "Ready=false." in _texts(harness)[1]
    assert "Ready=true." in _texts(harness)[3]


@pytest.mark.parametrize("operation", ["run", "exec"])
def test_inline_calls_capture_structs_and_preserve_field_access(
    tmp_path: Path, operation: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
struct Result:
  passed: Boolean
  receipts: Json
agic make() -> Result:
  Make.
flow main():
  let result = run make
  {operation}: Passed={{{{result.passed}}}}; receipts={{{{result.receipts}}}}.
""",
        responses=[
            '{"passed":false,"receipts":[{"key":"agent.too","digest":"abc"}]}',
            "done",
        ],
    )
    run, output, error = _run(harness)

    assert run.status == "succeeded", error
    assert output == "done"
    assert (
        'Passed=false; receipts=[{"key":"agent.too","digest":"abc"}].'
        in (_texts(harness)[-1])
    )


def test_nested_wait_preserves_receipts_and_three_round_history(tmp_path: Path) -> None:
    receipt = {"key": "agent.too", "digest": "abc"}
    improvement = {"instruction": "Copy exactly.", "receipts": [receipt]}
    harness = _create(
        tmp_path,
        source="""
struct Improvement:
  instruction: Text
  receipts: Json
agic evaluate() -> Boolean:
  Evaluate.
agic improve() -> Improvement:
  Improve.
agic check_loaded(_: Improvement) -> Boolean:
  Check {{_.receipts}}.
flow wait_until_loaded(_: Improvement) -> Improvement:
  repeat:
    let loaded = run check_loaded
    until: Return {{loaded}}.
flow main() -> Improvement:
  repeat 5 times windowing 2:
    let passed = run evaluate
    run improve
    run wait_until_loaded
    let instruction = {{_.instruction}}
    until:
      {{passed}}/{{_1.passed}}/{{_2.passed}}
      {{instruction}}/{{_1.instruction}}/{{_2.instruction}}
""",
        responses=["true", json.dumps(improvement), "true", "true"] * 3 + ["true"],
    )
    run, output, error = _run(harness)

    assert run.status == "succeeded", error
    assert json.loads(output) == improvement
    texts = _texts(harness)
    assert len(texts) == 13
    assert all(
        'Check [{"key":"agent.too","digest":"abc"}].' in texts[i] for i in (2, 6, 10)
    )
    assert "true/true/true\nCopy exactly./Copy exactly./Copy exactly." in texts[-1]


def test_retry_restores_typed_locals_for_inline_capture(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
struct Result:
  count: Number
agic make() -> Result:
  Make.
flow main():
  let result = run make
  run: Count={{result.count}}.
""",
        responses=[
            ModelCallResult(message=Message.assistant('{"count":0}')),
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="flow:main")
            )
            assert run.status == "failed"
            retried = await harness.executor.retry(
                run.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert len(harness.adapter.invocations) == 3
            assert all("Count=0." in text for text in _texts(harness)[1:])

    asyncio.run(scenario())


def test_inline_mapper_preserves_structured_primary_input(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed() -> Json[]:
  Seed.
flow main() -> Text[]:
  run seed
  map in 1 lane: Value={{_.value}}.
""",
        responses=['[{"value":0},{"value":2}]', "zero", "two"],
    )
    run, output, error = _run(harness)

    assert run.status == "succeeded", error
    assert json.loads(output) == ["zero", "two"]
    assert "Value=0." in _texts(harness)[1]
    assert "Value=2." in _texts(harness)[2]


def test_named_calls_keep_their_declared_input_contract(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
agic check() -> Boolean:
  Check.
agic consume(ready: Number):
  Consume {{ready}}.
flow main():
  let ready = run check
  run consume
""",
        responses=["true"],
    )
    run, _output, error = _run(harness)

    assert run.status == "failed"
    assert "not Number" in str(error)
    assert len(harness.adapter.invocations) == 1


@pytest.mark.parametrize("operation", ["run", "exec"])
@pytest.mark.parametrize("name", ["_", "attachment"])
@pytest.mark.parametrize("part", [TextPart("hello"), ImagePart(file_id="image-1")])
def test_inline_captures_concrete_parts(
    tmp_path: Path, operation: str, name: str, part: Part
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
flow main({name}: Part):
  context = none
  instruct = none
  recall = none
  {operation}: Inspect {{{{{name}}}}}.
""",
        responses=["done"],
    )

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:main",
                    primary=(part,) if name == "_" else None,
                    named={name: part} if name != "_" else None,
                )
            )
            assert run.status == "succeeded", (
                harness.store.resolve_error(run.error) if run.error else None
            )
            messages = without_runtime_snapshots(
                harness.adapter.invocations[0].call.messages
            )
            assert len(messages) == 1
            parts = messages[0].parts
            assert isinstance(parts[0], TextPart)
            if isinstance(part, TextPart):
                assert len(parts) == 1
                assert parts[0].text.endswith("Inspect hello.")
            else:
                assert parts[0].text.endswith("Inspect ")
                assert parts[1:] == (part, TextPart("."))

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "exec"])
def test_inline_sections_capture_outer_locals_and_prefer_item_fields(
    tmp_path: Path, operation: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Json:
  Seed.
flow main():
  let prefix = outer
  let rows = run seed
  {operation}: {{{{#rows}}}}{{{{prefix}}}}:{{{{name}}}};{{{{/rows}}}}
""",
        responses=['[{"name":"A"},{"name":"B","prefix":"inner"}]', "done"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "done"
    assert "outer:A;inner:B;" in _texts(harness)[-1]


def test_optional_struct_fields_do_not_resolve_to_python_methods(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
struct Result:
  items?: Text[]
agic make() -> Result:
  Make.
agic describe(result: Result):
  {{#result.items}}wrong{{/result.items}}{{^result.items}}empty{{/result.items}}
flow main():
  let result = run make
  run describe
""",
        responses=["{}", "done"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "done"
    assert "empty" in _texts(harness)[-1]


def test_inline_map_captures_primary_referenced_only_inside_a_section(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic ready() -> Boolean:
  Ready.
agic seed() -> Text[]:
  Seed.
flow main() -> Text[]:
  let enabled = run ready
  run seed
  map in 1 lane: {{#enabled}}Value={{_}}{{/enabled}}
""",
        responses=["true", '["a"]', "done"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == '["done"]'
    assert "Value=a" in _texts(harness)[-1]


def test_inline_section_primary_field_does_not_require_outer_primary(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed() -> Json:
  Seed.
flow main():
  let rows = run seed
  run: {{#rows}}{{_}}{{/rows}}
""",
        responses=['[{"_":"inside"}]', "done"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "done"
    assert "inside" in _texts(harness)[-1]


def test_repeat_history_stays_fixed_across_body_statements(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main:
  repeat 2 times:
    run: first={{_}}; {{#_1}}previous={{_1._}}{{/_1}}
    run: second={{_}}; {{#_1}}previous={{_1._}}; entered={{_1.__}}{{/_1}}
""",
        responses=["a", "b", "c", "d"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    texts = _texts(harness)
    assert "first=b; previous=b" in texts[2]
    assert "second=c; previous=b; entered=seed" in texts[3]


def test_nested_repeat_shadows_then_restores_outer_history(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main:
  repeat 2 times:
    repeat 1 time:
      run: inner={{_}}; {{^_1}}no history{{/_1}}
    run: outer={{_}}; {{#_1}}previous={{_1._}}{{/_1}}
""",
        responses=["a", "b", "c", "d"],
    )
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert "inner=b; no history" in _texts(harness)[2]
    assert "outer=c; previous=b" in _texts(harness)[3]


def test_until_out_of_window_reference_is_rejected_before_execution() -> None:
    source = """
flow main:
  repeat 1 time windowing 1:
    run: Improve {{_}}.
    until: {{#_2}}true{{/_2}}
"""
    with pytest.raises(ToolangError, match="outside the active window"):
        Program.from_source(source)


def test_inherited_until_template_adds_history_requirement(tmp_path: Path) -> None:
    source = """
instruct condition:
  Compare against {{_2._}}.
flow main:
  instruct = condition
  repeat 4 times:
    let note = unchanged
    until: Return true.
"""
    harness = _create(tmp_path, source=source, responses=["true"])
    run, output, error = _run(harness, primary="seed")
    assert run.status == "succeeded", run.error
    assert len(harness.adapter.invocations) == 1
    assert "Compare against seed." in harness.adapter.invocations[0].call.instructions


@pytest.mark.parametrize("child_lanes", [None, 5])
def test_lane_defaults_inherit_and_statement_override_is_local(
    tmp_path: Path, child_lanes: int | None
) -> None:
    source = """
agic worker():
  Work.
flow child() -> Text[]:
  generate 1 in 1 lane using worker
  generate 3 using worker
flow main() -> Text[][]:
  lanes = 2
  generate 1 using child
"""
    if child_lanes is not None:
        source = source.replace(
            "flow child() -> Text[]:\n",
            f"flow child() -> Text[]:\n  lanes = {child_lanes}\n",
        )
    harness = _create(
        tmp_path,
        source=source,
        responses=["a", "b", "c", "d"],
    )

    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main"), tracer=tracer
            )
            assert run.status == "succeeded", run.error
            children = [
                event
                for event in tracer.events
                if isinstance(event, RunBegin) and event.parent is not None
            ]
            inherited = child_lanes or 2
            assert [
                child.occurrence.lane.count
                for child in children
                if child.occurrence and child.occurrence.lane
            ] == [2, 1, inherited, inherited, inherited]

    asyncio.run(scenario())


def test_child_recall_can_override_flow_none_without_automatic_history(
    tmp_path: Path,
) -> None:
    harness = _create(
        tmp_path,
        source="""
agic seed():
  context = none
  user: old question
agic worker(note):
  recall = near
  context = none
  user: flow={{note}}; history={{_past}}
flow main():
  recall = none
  let note = {{_near}}
  run worker
""",
        responses=["old answer", "done"],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await harness.executor.run(harness.run_spec(thread=thread, runnable="seed"))
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="main")
            )
            assert run.status == "succeeded", run.error
            messages = without_runtime_snapshots(
                harness.adapter.invocations[-1].call.messages
            )
            assert len(messages) == 1
            text = message_text(messages[0].parts)
            assert "flow=[]; history=[" in text
            assert "old question" in text and "old answer" in text
            assert '"role":"user"' in text

    asyncio.run(scenario())


def test_inherited_instruct_cannot_capture_undeclared_parent_parameters(
    tmp_path: Path,
) -> None:
    from toolang.base.types.run import ToolCall

    source = """
instruct secret:
  Use {{topic}}.
agic parent(topic):
  hands = worker
  instruct = secret
  user: Delegate.
agic worker():
  user: Work.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("child", "child", "_toolang__run", {"runnable": "worker"}),
                )
            ),
            ModelCallResult(message=Message.assistant("handled")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", named={"topic": "private"}
                )
            )
            assert run.status == "succeeded", run.error
            child = next(
                item
                for item in harness.store.list_run_tree(root_run_id=run.id)
                if item.parent
            )
            assert child.status == "failed"
            assert child.error is not None
            assert "template input is missing: topic" in str(
                harness.store.resolve_error(child.error)
            )
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_settle_initializer_reads_outer_history_before_shadowing(
    tmp_path: Path,
) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
flow main():
  repeat 2 times:
    run seed
    reduce:
      current={{_}}; seed={{_1._}}
      from: {{#_1}}{{_1._}}{{/_1}}{{^_1}}start{{/_1}}
"""
    harness = _create(
        tmp_path,
        source=source,
        responses=['["a"]', "first", '["b"]', "second"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert "current=a; seed=start" in _texts(harness)[1]
    assert "current=b; seed=first" in _texts(harness)[3]
    assert output == "second"


def test_settle_accepts_array_valued_initializer_and_output(tmp_path: Path) -> None:
    source = """
agic seed() -> Text[]:
  Seed.
agic fold -> Number[]:
  user: Merge {{_}} into {{_1._}}.
flow main() -> Number[]:
  run seed
  reduce using fold:
    from: [1,2]
"""
    harness = _create(tmp_path, source=source, responses=['["a"]', "[1,2,3]"])
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert "Merge a into [1,2]." in _texts(harness)[1]
    assert output == "[1,2,3]"


def test_singleton_settle_skips_unrendered_reducer_history(tmp_path: Path) -> None:
    harness = _create(
        tmp_path,
        source="""
flow main():
  generate 1: Seed
  reduce: {{_}} {{_2._}}
""",
        responses=["seed"],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "seed"
    assert len(harness.adapter.invocations) == 1


@pytest.mark.parametrize("value", ["hello", "", "false", "null", "123"])
@pytest.mark.parametrize(
    "operation, output_type, response",
    [
        ("map in 1 lane", "Text[]", "done"),
        ("keep in 1 lane if", "Json[]", "true"),
        ("drop in 1 lane if", "Json[]", "false"),
        ("sort ascending by", "Json[]", "1"),
    ],
)
def test_inline_collection_calls_preserve_json_strings(
    tmp_path: Path, value: str, operation: str, output_type: str, response: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Json[]:
  Seed.
flow main() -> {output_type}:
  run seed
  {operation}: {{{{#_}}}}nonempty{{{{/_}}}}{{{{^_}}}}empty{{{{/_}}}}|{{{{_}}}}
""",
        responses=[json.dumps([value]), response],
    )
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert json.loads(output) == (["done"] if operation.startswith("map") else [value])
    expected = ("nonempty" if value else "empty") + "|" + json.dumps(value)
    assert expected in _texts(harness)[-1]


@pytest.mark.parametrize("operation", ["run", "exec"])
@pytest.mark.parametrize("value", ["hello", "", "false", "null", "123"])
def test_named_calls_and_flow_outputs_preserve_json_strings(
    tmp_path: Path, value: str, operation: str
) -> None:
    harness = _create(
        tmp_path,
        source=f"""
agic seed() -> Json:
  Seed.
agic echo(value: Json) -> Json:
  {{{{#value}}}}nonempty{{{{/value}}}}{{{{^value}}}}empty{{{{/value}}}}|{{{{value}}}}
flow main() -> Json:
  let value = run seed
  {operation} echo
""",
        responses=[json.dumps(value), json.dumps(value)],
    )

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:main",
                )
            )
            assert run.status == "succeeded", run.error
            assert run.output is not None
            assert harness.store.resolve_output(run.output).value == value
            expected = ("nonempty" if value else "empty") + "|" + json.dumps(value)
            assert expected in _texts(harness)[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "exec"])
@pytest.mark.parametrize("declaration", ["", "struct Result:\n  label: Text\n"])
def test_inline_capture_preserves_module_local_structs(
    tmp_path: Path, operation: str, declaration: str
) -> None:
    from toolang.state.prepare import prepare_agent_state

    harness = _create(tmp_path, source="", responses=['{"count":7}', "done"])
    home = harness.setup.layout.home
    (home / "flows").mkdir(parents=True, exist_ok=True)
    (home / "agent.too").write_text(
        f"""
{declaration}
flow main():
  let result = run research
  {operation}: Count={{{{result.count}}}}.
""",
        encoding="utf-8",
    )
    (home / "flows" / "research.too").write_text(
        """
struct Result:
  count: Number
agic seed() -> Result:
  Seed.
flow research() -> Result:
  run seed
""",
        encoding="utf-8",
    )
    harness.state = prepare_agent_state(harness.setup.layout)
    run, output, error = _run(harness)
    assert run.status == "succeeded", error
    assert output == "done"
    assert "Count=7." in _texts(harness)[-1]


@pytest.mark.parametrize("value", ["hello", "false", ""])
def test_retry_restores_json_string_collection_captures(
    tmp_path: Path, value: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic seed() -> Json[]:
  Seed.
flow main() -> Text[]:
  run seed
  map in 1 lane: {{#_}}nonempty{{/_}}{{^_}}empty{{/_}}|{{_}}
""",
        responses=[
            ModelCallResult(message=Message.assistant(json.dumps([value]))),
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:main",
                )
            )
            assert run.status == "failed"
            retried = await harness.executor.retry(
                run.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert len(harness.adapter.invocations) == 3
            expected = ("nonempty" if value else "empty") + "|" + json.dumps(value)
            assert all(expected in text for text in _texts(harness)[1:])
            assert harness.store.run_output_text(run_id=retried.id) == '["done"]'

    asyncio.run(scenario())
