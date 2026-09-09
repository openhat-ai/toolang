"""Runnable signature metadata can be reused outside Script commands."""

import pytest
from typer._click import Context
from typer.core import TyperArgument, TyperCommand

from toolang.cli.common.runnable_parameters import RunnableArgument, runnable_parameters
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program, Span


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("", [("_", "INPUT", "PART[]", True)]),
        ("()", []),
        ("(topic: Text)", [("topic", "topic=<ARGUMENT>", "TEXT", True)]),
        (
            "(_: Text, class: Number, enabled?: Boolean, items?: Part[])",
            [
                ("class", "class=<ARGUMENT>", "NUMBER", True),
                ("enabled", "enabled=<ARGUMENT>", "BOOLEAN", False),
                ("items", "items=<ARGUMENT>", "PART[]", False),
                ("_", "INPUT", "TEXT", True),
            ],
        ),
    ],
)
def test_runnable_parameters_follow_the_authored_signature(
    kind: str, signature: str, expected: list[tuple[str, str, str, bool]]
) -> None:
    body = "  Describe the result." if kind == "agic" else "  let note = observed"
    program = Program.from_source(f"{kind} demo{signature}:\n{body}\n")
    runnable = program.agics[0] if kind == "agic" else program.flows[0]
    parameters = runnable_parameters(runnable)
    command = TyperCommand("demo", params=[*parameters])
    ctx = Context(command)
    assert [
        (param.name, param.metavar, param.type.get_metavar(param, ctx), param.required)
        for param in parameters
    ] == expected
    assert all(type(param) is TyperArgument for param in parameters)
    assert [param.help for param in parameters] == [
        "Primary input, or simply input"
        if name == "_"
        else "Named input, or simply argument"
        for name, *_ in expected
    ]
    assert all(not param.show_default for param in parameters)


@pytest.mark.parametrize("kind", [AgicDecl, FlowDecl])
def test_runnable_parameters_keep_docs_and_accept_capture_help(
    kind: type[AgicDecl] | type[FlowDecl],
) -> None:
    span = Span(line=1)
    runnable = kind(
        name="demo",
        span=span,
        params=(
            Parameter(name="topic", type_name="Text", span=span, doc="  Topic.  "),
        ),
        input=Parameter(name="_", type_name="Text", span=span, doc="  Evidence.  "),
    )
    native = runnable_parameters(runnable)
    with_capture = runnable_parameters(runnable, input_help="Read one input.")
    assert [param.help for param in native] == ["Topic.", "Evidence."]
    assert [param.help for param in with_capture] == [
        "Topic.",
        "Evidence. Read one input.",
    ]


def test_runnable_parameters_are_native_arguments_with_raw_values() -> None:
    program = Program.from_source("agic demo(_: Text, class: Number):\n  Describe.\n")
    parameters = runnable_parameters(program.agics[0])
    command = TyperCommand(
        "demo", params=[*parameters], callback=lambda **values: values
    )
    result = command.main(["12", "Evidence"], standalone_mode=False)
    assert result == {"class": "12", "_": "Evidence"}


def test_runnable_help_leaves_arguments_to_the_command_collector() -> None:
    program = Program.from_source(
        "agic demo(topic: Text, enabled?: Boolean):\n  Describe.\n"
    )
    parameters = runnable_parameters(program.agics[0], help_only=True)
    command = TyperCommand(
        "demo",
        params=[
            *parameters,
            TyperArgument(param_decls=["items"], nargs=-1, hidden=True),
        ],
        callback=lambda **values: values,
    )
    assert all(isinstance(param, RunnableArgument) for param in parameters)
    assert [(param.name, param.required) for param in parameters] == [
        ("topic", True),
        ("enabled", False),
    ]
    assert all(param.get_usage_pieces(Context(command)) == [] for param in parameters)
    items = ["enabled=true", "topic=History"]
    assert command.main(items, standalone_mode=False) == {"items": tuple(items)}
    # The caller, not the display metadata, validates missing required inputs.
    assert command.main([], standalone_mode=False) == {"items": ()}
