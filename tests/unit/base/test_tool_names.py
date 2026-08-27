from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.base.utils.tools import encode_tool_name


@pytest.mark.parametrize(
    ("namespace", "leaf", "encoded"),
    [
        ("fs", "read", "fs__read"),
        ("public_tools", "run_task", "public_tools__run_task"),
        ("_me", "create_task", "_me__create_task"),
    ],
)
def test_encode_tool_name_accepts_canonical_components(
    namespace: str,
    leaf: str,
    encoded: str,
) -> None:
    assert encode_tool_name(namespace, leaf) == encoded


@pytest.mark.parametrize(
    "namespace",
    [
        "",
        "_",
        "__me",
        "_me__state",
        "fs1",
        "fs-name",
        "fs.name",
        "fs/name",
        "fs name",
        "文件",
    ],
)
def test_encode_tool_name_rejects_invalid_namespace(namespace: str) -> None:
    with pytest.raises(ToolangError, match="namespace name"):
        encode_tool_name(namespace, "read")


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
