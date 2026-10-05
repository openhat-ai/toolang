"""Source checks reject definite errors without executing or resolving resources."""

import pytest

from toolang.lang import Program
from toolang.lang.errors import ToolangError, ToolangSourceError


@pytest.mark.parametrize(
    "source, message",
    [
        ("agic work(_):\n  {{#items}}Hello\n", "unclosed"),
        ("instruct:\n  {{#items}}Hello\n", "unclosed"),
        ("context:\n  {{#items}}Hello\n", "unclosed"),
        ("prompt bad:\n  {{#items}}Hello\n", "unclosed"),
        ("flow work:\n  let note = {{#items}}Hello\n", "unclosed"),
        ("flow work:\n  ask: {{#items}}Hello\n", "unclosed"),
        ("flow work:\n  let note = {{runtime}}\n", "missing.*runtime"),
        ("agic work(_):\n  {{_typo}}\n", "unknown runtime reference"),
        ("agic work(_):\n  {{_0._}}\n", "outside the active window"),
        ("agic work(_):\n  {{_01._}}\n", "outside the active window"),
        ("agic work(_):\n  {{missing}}\n", "missing"),
        ("agic work(_: Missing):\n  {{_}}\n", "Unknown.*type"),
        ("agic work() -> Missing:\n  Hello\n", "Unknown.*type"),
        ("struct Record:\n  child: Missing[]\n", "Unknown.*type"),
        (
            "agic worker(_, topic):\n  {{topic}}\nflow work:\n  run worker\n",
            "Missing.*topic",
        ),
        ("flow work():\n  map using missing\n", "unknown runnable"),
        (
            "flow work:\n  run -> Text: Values\n  map: {{_}}\n",
            "outer array",
        ),
        (
            "flow work:\n  run -> Text[]: Values\n  reduce -> Number: {{_}}\n",
            "Reduce without from requires Text output",
        ),
        (
            "flow work:\n  repeat 5 times windowing 1:\n"
            "    run: {{_}}\n    until: {{_2._}}\n",
            "outside the active window",
        ),
        (
            "flow work:\n  run -> Text[]: Values\n  reduce: {{_}} {{_2._}}\n",
            "outside the active window",
        ),
        (
            "flow work:\n  run -> Text[]: Values\n  reduce:\n    {{_}}\n"
            "    from: {{#items}}Hello\n",
            "unclosed",
        ),
    ],
)
def test_source_determined_errors_are_rejected(source, message):
    with pytest.raises(ToolangError, match=message):
        Program.from_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "struct Node:\n  children: Node[]\nagic work(_: Node) -> Node:\n  {{_}}\n",
        "struct A:\n  value: B\nstruct B:\n  value: Text\n",
        "agic work(records: Json):\n  {{#records}}{{_field}}{{/records}}\n",
        "agic worker(_, topic?):\n  {{topic}}\nflow work:\n  run worker\n",
        "agic reducer(_):\n  {{_}} {{_1._}}\n",
        "flow work:\n  run -> Text[]: Values\n  map -> Text[]: {{_}}\n  run: {{_}}\n",
        "flow work:\n  repeat 0 times:\n    map: {{_}}\n",
        "flow work:\n  generate 0: No calls\n",
        "flow work:\n  repeat 2 times:\n    run: {{_}}\n    until: {{_3._}}\n",
        "flow work:\n  repeat 2 times:\n    run -> Text[]: Values\n    reduce:\n"
        "      {{_}} {{_1._}}\n      from: {{_2._}}\n",
        "flow work:\n  repeat 2 times:\n    repeat 2 times windowing 1:\n"
        "      run: {{_}}\n      until: {{_1._}}\n    until: {{_3._}}\n",
        "flow work:\n  repeat 2 times:\n    let topic = Value\n"
        "    until: {{topic}}\n  run: {{topic}}\n",
    ],
)
def test_unknown_or_valid_runtime_conditions_are_preserved(source):
    Program.from_source(source)


def test_static_errors_have_structured_source_locations():
    with pytest.raises(ToolangError) as caught:
        Program.from_source("# Heading\nagic work(_):\n  {{missing}}\n")
    assert isinstance(caught.value, ToolangSourceError)
    assert caught.value.line == 3
    assert caught.value.column == 3


@pytest.mark.parametrize("kind", ["instruct", "context"])
@pytest.mark.parametrize("selection", ["history", "default"])
@pytest.mark.parametrize("owner", ["worker", "main"])
def test_selected_templates_obey_the_callers_nearest_window(kind, selection, owner):
    source = f"""
{kind}{" history" if selection == "history" else ""}:
  {{{{_2._}}}}
agic worker:
  {f"{kind} = {selection}" if owner == "worker" else ""}
  Work.
flow main:
  {f"{kind} = {selection}" if owner == "main" else ""}
  repeat 3 times windowing 1:
    run worker
"""
    with pytest.raises(ToolangError, match="outside the active window"):
        Program.from_source(source)


@pytest.mark.parametrize("operation", ["reduce"])
def test_known_empty_collections_are_rejected(operation):
    with pytest.raises(ToolangError, match="requires a nonempty array"):
        Program.from_source(f"""
flow main:
  generate 0: No calls
  {operation}: {{{{_}}}}
""")


def test_named_and_discarded_results_do_not_replace_current():
    Program.from_source("""
flow main:
  run -> Text[]: Items
  let saved = run: {{_}}
  let run: {{_}}
  map: {{_}} {{saved}}
""")
    with pytest.raises(ToolangError, match="outer array"):
        Program.from_source("""
flow main(_: Text):
  let items = run -> Text[]: Items
  map: {{_}}
""")


def test_remote_seek_does_not_use_a_same_named_local_signature():
    Program.from_source("""
agic review(_, local_only):
  Review {{_}} with {{local_only}}.
flow main:
  seek reviewer review
""")


def test_callee_override_can_disable_inherited_history_template():
    Program.from_source("""
instruct history:
  {{_2._}}
agic worker:
  instruct = none
  Work.
flow main:
  instruct = history
  repeat 2 times windowing 1:
    run worker
""")


def test_repeat_backedge_widens_changed_shapes_without_unrolling():
    Program.from_source("""
flow main:
  run -> Text[]: Items
  repeat 1000000000 times:
    map: {{_}}
    run: {{_}}
""")


@pytest.mark.parametrize(
    "kind", ["agic", "flow", "struct", "instruct", "context", "prompt"]
)
def test_duplicate_declaration_diagnostic_points_to_the_second_declaration(kind):
    body = "  value: Text" if kind == "struct" else "  Hello"
    name = "Same" if kind == "struct" else "same"
    with pytest.raises(ToolangSourceError, match="Duplicate") as caught:
        Program.from_source(f"{kind} {name}:\n{body}\n{kind} {name}:\n{body}\n")
    assert caught.value.line == 3
    assert caught.value.column == 1


def test_zero_repeat_still_checks_until_history_window():
    with pytest.raises(ToolangError, match="outside the active window"):
        Program.from_source("""
flow main:
  repeat 0 times windowing 1:
    run: {{_}}
    until: {{_2._}}
""")


def test_zero_repeat_does_not_require_condition_call_inputs():
    Program.from_source("""
flow main:
  repeat 0 times:
    run: {{_}}
    until: {{missing}}
""")


@pytest.mark.parametrize(
    "statement",
    [
        "generate 0: {{missing}}",
        "generate 0: {{_}}",
        "generate 0: Values\n  map: {{_}} {{missing}}",
        "generate 0: Values\n  keep if: {{_}} {{missing}}",
        "generate 0: Values\n  drop if: {{_}} {{missing}}",
        "generate 0: Values\n  sort ascending by: {{_}} {{missing}}",
    ],
)
def test_empty_parallel_operations_still_validate_required_inputs(statement):
    with pytest.raises(ToolangError, match="Missing input"):
        Program.from_source(f"flow main():\n  {statement}\n")


@pytest.mark.parametrize(
    "operation", ["map", "keep if", "drop if", "sort ascending by"]
)
def test_empty_parallel_operations_do_not_render_child_history(operation):
    Program.from_source(f"""
flow main():
  repeat 1 time windowing 1:
    generate 0: Values
    {operation}: {{{{_}}}} {{{{_2._}}}}
""")


def test_zero_storm_does_not_render_child_history():
    Program.from_source("""
flow main():
  repeat 1 time windowing 1:
    generate 0: {{_2._}}
""")


@pytest.mark.parametrize(
    "steps",
    [
        "generate 1: Seed",
        "generate 1: Seed\n  map: {{_}}",
        "generate 1: Seed\n  sort ascending by: {{_}}",
        "generate 3: Seed\n  keep first 1",
        "generate 2: Seed\n  drop last 1",
    ],
)
def test_singleton_settle_does_not_render_reducer_history(steps):
    Program.from_source(f"flow main():\n  {steps}\n  reduce: {{{{_}}}} {{{{_2._}}}}\n")


def test_singleton_settle_with_initializer_still_checks_reducer_history():
    with pytest.raises(ToolangError, match="outside the active window"):
        Program.from_source("""
flow main():
  generate 1: Seed
  reduce:
    {{_}} {{_2._}}
    from: Initial
""")
