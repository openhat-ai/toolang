"""Executor dependency injection never broadens the ordinary ToolContext."""

from typing import cast

import pytest

from toolang.base.protocols.tool import ToolHistory, ToolRuntime
from toolang.base.types.tool import ToolService
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps.tool import _tool_context
from toolang.plugin.toolsets.loading import load_tools


@pytest.mark.parametrize(
    "name,extra",
    [
        ("fs__read", None),
        ("shell__execute", None),
        ("web__search", None),
        ("service__init", "services"),
        ("history__read_runs", "history"),
        ("_toolang__pick", "runtime"),
        ("me__get", "layout"),
    ],
)
def test_only_the_selected_toolset_receives_special_dependencies(tmp_path, name, extra):
    layout = AgentLayout(root=tmp_path, name="alice", placement="resident")
    runtime = cast(ToolRuntime, object())
    history = cast(ToolHistory, object())
    services = (ToolService("api", {}, {"TOKEN": "private"}),)
    context = _tool_context(
        layout=layout,
        tool=load_tools()[name],
        services=services,
        runtime=runtime,
        history=history,
        workspaces={"repo": tmp_path / "repo"},
    )
    for field, value in {
        "services": services,
        "runtime": runtime,
        "history": history,
        "layout": layout,
    }.items():
        if field == extra:
            assert getattr(context, field) is value
        else:
            assert not hasattr(context, field)
    assert context.home == layout.home
    assert context.room == layout.tool_room(name.split("__")[0])
    assert context.workspaces == {"repo": tmp_path / "repo"}
