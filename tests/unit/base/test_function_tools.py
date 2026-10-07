from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import Annotated, Any, Literal, List

import pytest
from pydantic import BeforeValidator, StrictInt

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool


def _context(home: Path) -> ToolContext:
    return ToolContext(home=home, room=home / ".runtime" / "tools" / "test")


def test_function_tool_runs_sync_callable_in_worker_thread(tmp_path: Path) -> None:
    owner_thread = threading.get_ident()

    @tool()
    def current_thread() -> dict[str, int]:
        return {"thread": threading.get_ident()}

    result = asyncio.run(
        create_function_tool(current_thread).invoke({}, _context(tmp_path))
    ).output

    assert result["thread"] != owner_thread


def test_function_tool_awaits_async_callable_on_owner_loop(tmp_path: Path) -> None:
    owner_thread = threading.get_ident()

    @tool()
    async def current_thread() -> dict[str, int]:
        await asyncio.sleep(0)
        return {"thread": threading.get_ident()}

    result = asyncio.run(
        create_function_tool(current_thread).invoke({}, _context(tmp_path))
    ).output

    assert result["thread"] == owner_thread


def test_function_preserves_keyword_argument_binding(tmp_path: Path):
    @tool()
    def accepts_kwargs(**kwargs):
        return kwargs

    result = asyncio.run(
        create_function_tool(accepts_kwargs).invoke(
            {"extra": "false"}, _context(tmp_path)
        )
    ).output
    assert result == {"extra": "false"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_binding_coerces_before_paths_and_preserves_raw_summary(tmp_path, asynchronous):
    seen = []

    def paths(arguments, context):
        seen.append(dict(arguments))
        return {}

    def sync(count: int, *, flags: list[bool], rows: dict[str, list[float]], omitted=3):
        return {"count": count, "flags": flags, "rows": rows, "omitted": omitted}

    async def async_func(
        count: int, *, flags: list[bool], rows: dict[str, list[float]], omitted=3
    ):
        return sync(count, flags=flags, rows=rows, omitted=omitted)

    wrapped = create_function_tool(
        tool(paths=paths, summary=lambda args, _: str(args["count"]))(
            async_func if asynchronous else sync
        )
    )
    arguments = {"count": "7", "flags": ["false", "true"], "rows": {"a": ["2.5"]}}
    expected = {"count": 7, "flags": [False, True], "rows": {"a": [2.5]}}
    bound = wrapped.bind_arguments(arguments)
    assert bound == expected
    assert wrapped.bind_arguments(bound) == bound
    assert wrapped.paths(arguments, _context(tmp_path)) == {}
    assert seen == [expected]
    assert wrapped.summary(arguments) == "7"
    assert asyncio.run(wrapped.invoke(arguments, _context(tmp_path))).output == {
        **expected,
        "omitted": 3,
    }
    assert arguments == {
        "count": "7",
        "flags": ["false", "true"],
        "rows": {"a": ["2.5"]},
    }


@pytest.mark.parametrize(
    "annotation,value",
    [
        (int, "bad"),
        (int, 1.5),
        (int, True),
        (float, True),
        (float, "nan"),
        (float, float("inf")),
        (list[int], [True]),
        (dict[str, list[float]], {"x": [False]}),
        (list[int], "[1,2]"),
        (str, 7),
        (int, None),
        (StrictInt, "7"),
        (Literal[1, 2], True),
        (Literal[False, 1], True),
        (Literal[True, 0], False),
    ],
)
def test_invalid_types_do_not_enter_paths_or_body(tmp_path, annotation, value):
    def unreachable(*args):
        pytest.fail("invalid input reached hook/body")

    def work(value):
        unreachable()

    work.__annotations__ = {"value": annotation}
    wrapped = create_function_tool(tool(paths=unreachable)(work))
    for action in (
        wrapped.bind_arguments,
        lambda args: wrapped.paths(args, _context(tmp_path)),
        lambda args: asyncio.run(wrapped.invoke(args, _context(tmp_path))),
    ):
        with pytest.raises((ToolangError, ValueError), match="value"):
            action({"value": value})


def test_unions_any_and_kwargs_preserve_native_values(tmp_path):
    @tool()
    def work(value: int | bool | None, native: Any, **extras: int):
        return {"value": value, "native": native, **extras}

    wrapped = create_function_tool(work)
    for value in (True, None, 7):
        bound = wrapped.bind_arguments({"value": value, "native": "[1]", "extra": "8"})
        assert bound == {"value": value, "native": "[1]", "extra": 8}
        assert type(bound["value"]) is type(value)
    assert wrapped.definition().parameters["additionalProperties"] == {
        "type": "integer"
    }
    with pytest.raises((ToolangError, ValueError), match="extra"):
        wrapped.bind_arguments({"value": 1, "native": None, "extra": "bad"})
    with pytest.raises((ToolangError, ValueError), match="context"):
        wrapped.bind_arguments({"value": 1, "native": None, "context": None})


@pytest.mark.parametrize(
    "arguments,match",
    [
        ({}, "count"),
        ({"count": 1, "extra": 2}, "extra"),
        ({"count": 1, "context": None}, "context"),
    ],
)
def test_names_and_required_arguments(arguments, match):
    @tool()
    def work(count: int, context):
        pytest.fail("binding must not execute the function")

    with pytest.raises((ToolangError, TypeError, ValueError), match=match):
        create_function_tool(work).bind_arguments(arguments)


def test_context_injection_and_defaults(tmp_path):
    @tool()
    def work(context, *, count: int = 7):
        return {"count": count, "context": context}

    wrapped = create_function_tool(work)
    context = _context(tmp_path)
    assert wrapped.bind_arguments({}) == {}
    assert wrapped.definition().parameters["properties"] == {
        "count": {"type": "integer", "default": 7}
    }
    assert asyncio.run(wrapped.invoke({}, context)).output == {
        "count": 7,
        "context": context,
    }


def test_resolved_annotations_drive_schema_and_explicit_schema_is_preserved():
    @tool()
    def work(
        count: int, flags: list[bool], choice: Literal["a", "b"], maybe: int | None
    ):
        pass

    schema = create_function_tool(work).definition().parameters
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["properties"]["flags"] == {
        "type": "array",
        "items": {"type": "boolean"},
    }
    assert schema["properties"]["choice"]["enum"] == ["a", "b"]
    assert schema["properties"]["maybe"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]
    assert schema["additionalProperties"] is False
    for explicit in (
        {},
        {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 5, "description": "Count"}
            },
        },
    ):
        wrapped = create_function_tool(tool(parameters=explicit)(work))
        assert wrapped.definition().parameters == explicit
        assert (
            wrapped.bind_arguments(
                {"count": "1", "flags": [], "choice": "a", "maybe": None}
            )["count"]
            == 1
        )


@pytest.mark.parametrize(
    "annotation",
    [
        "MissingType",
        "list[",
        tuple[int, ...],
        Path,
        List,
        Annotated[int, BeforeValidator(int)],
        Literal[float("inf")],  # type: ignore[invalid-type-form]
    ],
)
def test_unsupported_annotations_fail_preparation(annotation):
    def work(value):
        pass

    work.__annotations__ = {"value": annotation}
    with pytest.raises(ToolangError, match="value"):
        create_function_tool(tool()(work))


def test_invalid_defaults_and_unusable_signatures_fail_preparation():
    def default(count: int = "7"):  # type: ignore[invalid-parameter-default]
        pass

    def positional(value, /):
        pass

    def variadic(*values):
        pass

    for func in (default, positional, variadic):
        with pytest.raises(ToolangError):
            create_function_tool(tool()(func))


def test_input_resolution_does_not_validate_return_annotations():
    @tool()
    def work(value: "list[int]"):
        return value

    work.__annotations__["return"] = "UnresolvedReturn"

    assert create_function_tool(work).bind_arguments({"value": ["7"]}) == {"value": [7]}


def test_boolean_literal_branch_and_sync_awaitable_result(tmp_path):
    async def result(value):
        return {"value": value}

    @tool()
    def work(value: Literal[1, True]):
        return result(value)

    wrapped = create_function_tool(work)
    output = asyncio.run(wrapped.invoke({"value": True}, _context(tmp_path))).output
    assert output["value"] is True


@pytest.mark.parametrize("arguments", [{1: "7"}, {"context": None}])
def test_kwargs_do_not_allow_invalid_or_reserved_names(arguments):
    @tool()
    def work(**values: int):
        pytest.fail("invalid names must not reach the body")

    with pytest.raises(ToolangError):
        create_function_tool(work).bind_arguments(arguments)
