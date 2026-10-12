from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.base.utils.tools import encode_tool_name


@pytest.mark.parametrize(
    ("toolset", "leaf", "encoded"),
    [
        ("fs", "read", "fs__read"),
        ("public_tools", "run_task", "public_tools__run_task"),
        ("state", "create", "state__create"),
        ("runtime", "run", "runtime__run"),
    ],
)
def test_encode_tool_name_accepts_canonical_components(
    toolset: str,
    leaf: str,
    encoded: str,
) -> None:
    assert encode_tool_name(toolset, leaf) == encoded


@pytest.mark.parametrize(
    "toolset",
    [
        "",
        "_",
        "__me",
        "state__state",
        "fs1",
        "fs-name",
        "fs.name",
        "fs/name",
        "fs name",
        "文件",
    ],
)
def test_encode_tool_name_rejects_invalid_toolset(toolset: str) -> None:
    with pytest.raises(ToolangError, match="toolset name"):
        encode_tool_name(toolset, "read")


@pytest.mark.parametrize(
    "leaf",
    [
        "",
        "_read",
        "read1",
        "read-file",
        "read.file",
        "read__file",
        "read/file",
        "read file",
        "读取",
    ],
)
def test_encode_tool_name_rejects_invalid_leaf(leaf: str) -> None:
    with pytest.raises(ToolangError, match="tool name"):
        encode_tool_name("fs", leaf)


@pytest.mark.parametrize("toolset", ["public_tools", "public_tools_", "_internal_"])
def test_identity_forms_share_validated_components(toolset):
    from toolang.base.utils.tools import decode_tool_name
    from toolang.plugin.toolsets.registry import ToolRef

    ref = ToolRef("plugin", toolset, "run_task")
    assert ref.identity == f"{toolset}/run_task"
    assert ref.model_name == f"{toolset}__run_task"
    assert ref.display_name == f"{toolset}.run_task"
    assert decode_tool_name(ref.model_name) == (ref.toolset, ref.name)
    assert decode_tool_name("_toolang__execute") == ("_toolang", "execute")
    for invalid in (ref.identity, ref.display_name, "a__b__c", "a____b", "a__", "__a"):
        assert decode_tool_name(invalid) is None
