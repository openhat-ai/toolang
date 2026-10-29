"""Installed built-in entry points agree with factory and inspection identities."""

from collections.abc import Callable
from importlib.metadata import distribution
from typing import Any, cast

import pytest

from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.catalogs.models_dev.path import PACKAGED_MODEL_CATALOG


def test_builtin_entry_points_only_register_supported_plugin_families() -> None:
    assert {
        entry.group
        for entry in distribution("toolang").entry_points
        if entry.group.startswith("toolang.")
    } == {
        "toolang.toolset",
        "toolang.model_catalog",
        "toolang.model_adapter",
        "toolang.sandbox",
    }


@pytest.mark.parametrize(
    ("family", "names"),
    [
        (
            "toolset",
            {
                "runtime",
                "convo",
                "fs",
                "history",
                "state",
                "service",
                "shell",
                "tavily",
                "serpapi",
            },
        ),
        ("model_catalog", {"models_dev", "ollama", "llama_cpp"}),
        (
            "model_adapter",
            {"chat_completions", "generate_content", "messages", "responses"},
        ),
        ("sandbox", {"host", "docker"}),
    ],
)
def test_builtin_factories_and_inspection_share_registered_identities(
    family: str,
    names: set[str],
) -> None:
    group = f"toolang.{family}"
    entries = {
        entry.name: entry
        for entry in distribution("toolang").entry_points
        if entry.group == group
    }
    assert set(entries) == names
    assert {
        info.name
        for info in list_plugin_infos(group=group)
        if info.package == "toolang"
    } == names

    for name, entry in entries.items():
        factory = cast(Callable[[dict[str, Any]], Any], entry.load())
        configs: dict[str, dict[str, Any]] = {
            "models_dev": {"path": PACKAGED_MODEL_CATALOG},
        }
        plugin = factory(configs.get(name, {}))
        assert plugin.name == name
