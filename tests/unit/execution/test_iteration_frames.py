"""Iteration scopes expose immutable, bounded entry/exit snapshots."""

import asyncio

import pytest

from toolang.common.errors import ToolangError
from toolang.common.template import render_text_template, template_dependencies
from toolang.execution.executor.common import Local
from toolang.execution.executor.iteration import (
    IterationFrame,
    IterationScope,
    history_available,
    iteration_scope,
    iteration_values,
    snapshot,
)


@pytest.mark.parametrize("value", [False, 0, "", []])
def test_history_guards_test_presence_even_for_empty_values(value):
    frame = IterationFrame(
        snapshot({"_": Local("input", "item")}), snapshot({"_": Local(value, "item")})
    )
    with iteration_scope(IterationScope(1, (frame,))):
        values = iteration_values()
        assert (
            render_text_template(
                "{{#_1._}}present{{/_1._}}{{^_1._}}absent{{/_1._}}", values
            )
            == "present"
        )
        assert render_text_template("{{_1.__}}", values) == "input"


def test_missing_frames_can_be_guarded_but_missing_fields_cannot():
    with iteration_scope(IterationScope(2)):
        assert (
            render_text_template(
                "{{#_1}}value={{_1._}}{{/_1}}{{^_1}}warming{{/_1}}", iteration_values()
            )
            == "warming"
        )
        with pytest.raises(ToolangError, match="not available"):
            render_text_template("{{_1._}}", iteration_values())
        with pytest.raises(ToolangError, match="outside the active window"):
            render_text_template("{{#_3}}hidden{{/_3}}", iteration_values())
        assert not history_available(("{{^_2}}waiting{{/_2}}",))
    with iteration_scope(
        IterationScope(
            1, (IterationFrame(snapshot({}), snapshot({"_": Local("seed", "item")})),)
        )
    ):
        with pytest.raises(ToolangError, match="field is missing"):
            render_text_template("{{#_1.__}}input{{/_1.__}}", iteration_values())


def test_snapshots_exclude_runtime_bindings_and_copy_nested_values():
    mutable = {"items": [1], "_1": "ordinary data"}
    entry = snapshot(
        {
            "report": Local(mutable, "item", type_name="Json"),
            "_1": Local("old", "item"),
            "_past": Local([], "item"),
        }
    )
    mutable["items"].append(2)
    assert set(entry) == {"report"}
    assert entry["report"].value["items"] == (1,)
    assert entry["report"].value["_1"] == "ordinary data"
    assert entry["report"].type_name == "Json"
    with pytest.raises(TypeError):
        entry["report"].value["items"] = []


def test_async_children_shadow_independently_and_restore_the_outer_scope():
    async def child(value):
        frame = IterationFrame(snapshot({}), snapshot({"_": Local(value, "item")}))
        with iteration_scope(IterationScope(1, (frame,))):
            await asyncio.sleep(0)
            return render_text_template("{{_1._}}", iteration_values())

    async def scenario():
        with iteration_scope(IterationScope(3)):
            assert await asyncio.gather(child("a"), child("b")) == ["a", "b"]
            assert iteration_values() == {"_1": None, "_2": None, "_3": None}
        assert iteration_values() == {}

    asyncio.run(scenario())


def test_inference_respects_data_and_history_sections():
    assert template_dependencies(
        "{{items}} {{#items}}{{title}}{{/items}} {{topic}} {{#_1}}{{_}}{{_1._}}{{/_1}}"
    ) == ("items", "topic", "_1")


def test_history_reads_follow_actual_mustache_branches():
    with iteration_scope(IterationScope(1)):
        values = {
            **iteration_values(),
            "options": {"enabled": False},
            "items": [{"enabled": False}],
        }
        assert (
            render_text_template(
                "{{#options.enabled}}{{_1._}}{{/options.enabled}}", values
            )
            == ""
        )
        assert (
            render_text_template(
                "{{#items}}{{#enabled}}{{_1._}}{{/enabled}}{{/items}}", values
            )
            == ""
        )
        values["items"] = [{"enabled": True}]
        with pytest.raises(ToolangError, match="not available"):
            render_text_template(
                "{{#items}}{{#enabled}}{{_1._}}{{/enabled}}{{/items}}", values
            )
