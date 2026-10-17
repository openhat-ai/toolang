"""LiveTree pruning and aggregation use only small resident values."""

from dataclasses import replace

import pytest

from toolang.base.types.run import ToolCall
from toolang.execution.live import LiveTree
from toolang.execution.schemas import RunNode, StepNode
from toolang.execution.records import StepRecord, StoredModelStepGiven, ModelCallRefs
from toolang.execution.types import (
    CallCounters,
    ControlRef,
    ModelCost,
    ModelStepNoted,
    ModelMessages,
    StepRef,
    TokenUsage,
    ToolStepGiven,
)


def _step(identity):
    ref = StepRef.parse(identity)
    return StepRecord(
        identity,
        "tool",
        (),
        ToolStepGiven(
            "test",
            ToolCall("t", "t", "test__work", {"huge": "x" * 100_000}),
            summary="x" * 1000,
        ),
        ControlRef.for_run(ref.run_id, 0),
        None,
    )


def test_pruning_keeps_async_ancestors_and_never_recounts_children():
    tree = LiveTree("run_root", "chat_test")
    tree.accept_run(
        "run_root", parent=None, runnable="flow:root", created_at="now", occurrence=None
    )
    launch = _step("run_root.0")
    tree.begin_step(launch)
    tree.accept_run(
        "run_child",
        parent=launch.ref,
        runnable="agic:child",
        created_at="now",
        occurrence=None,
    )
    tree.end_step(replace(launch, status="succeeded", finished_at="then"))
    leaf = _step("run_child.0")
    tree.begin_step(leaf)
    before = tree.snapshot()
    assert [n.id for n in before.nodes] == [
        "run_root",
        "run_root.0",
        "run_child",
        "run_child.0",
    ]
    assert isinstance(before.nodes[1], StepNode)
    assert len(before.nodes[1].summary) == 240
    tree.invoke(leaf.ref, "tool")
    assert tree.run_noted("run_root").calls == CallCounters(0, 1)
    assert tree.run_noted("run_child").calls == CallCounters(0, 1)
    with pytest.raises(RuntimeError, match="invocation"):
        tree.invoke(leaf.ref, "tool")
    tree.end_step(replace(leaf, status="failed", finished_at="then"))
    tree.end_run("run_child", "failed", "then")
    assert [n.id for n in tree.snapshot().nodes] == ["run_root"]
    assert tree.run_noted("run_root").calls == CallCounters(0, 1)
    assert isinstance(before.nodes[0], RunNode)
    assert before.nodes[0].calls == CallCounters()
    for i in range(100):
        leaf = _step(f"run_root.{i + 1}")
        tree.begin_step(leaf)
        tree.invoke(leaf.ref, "tool")
        tree.end_step(replace(leaf, status="succeeded", finished_at="then"))
    assert len(tree.snapshot().nodes) == 1
    assert tree.run_noted("run_root").calls == CallCounters(0, 101)


def test_optional_usage_and_cost_unknowns_are_sticky_across_pruned_steps():
    tree = LiveTree("run_root", "chat_test")
    tree.accept_run(
        "run_root", parent=None, runnable="agic:root", created_at="now", occurrence=None
    )
    contributions = [
        ModelStepNoted(TokenUsage(10, 5, 2, 1), ModelCost(0, "USD", "reported", True)),
        ModelStepNoted(TokenUsage(20, 6), ModelCost(1, "EUR", "reported", True)),
        ModelStepNoted(
            TokenUsage(30, 7, 4, 3), ModelCost(0.5, "USD", "estimated", False)
        ),
    ]
    for i, noted in enumerate(contributions):
        step = _step(f"run_root.{i}")
        step = replace(
            step,
            kind="model",
            given=StoredModelStepGiven(
                "test/model",
                ModelCallRefs("ref", ModelMessages(step.ref), None, None, None),
                setup="test",
            ),
        )
        tree.begin_step(step)
        tree.note_model(step.ref, noted)
        tree.end_step(replace(step, status="succeeded", finished_at="then"))
    aggregate = tree.run_noted("run_root")
    assert aggregate.usage == TokenUsage(60, 18)
    assert aggregate.cost is None and aggregate.cost_approximate
    tree.begin_run("run_root", "agic:next", "later")
    assert tree.run_noted("run_root") == aggregate
    root = tree.snapshot().nodes[0]
    assert isinstance(root, RunNode) and root.runnable == "agic:next"
