"""Installed built-in entry points agree with factory and inspection identities."""

from collections.abc import Callable
from importlib.metadata import distribution
from typing import Any, cast

import pytest

from toolang.base.protocols.channel import AgentChannel
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.catalogs.models_dev.path import PACKAGED_MODEL_CATALOG


@pytest.mark.parametrize(
    ("family", "names"),
    [
        ("toolset", {"_toolang", "fs", "history", "me", "service", "shell", "web"}),
        ("model_catalog", {"models_dev", "ollama", "llama_cpp"}),
        (
            "model_adapter",
            {"chat_completions", "generate_content", "messages", "responses"},
        ),
        ("channel", {"telegram"}),
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
        if info.source == "built-in"
    } == names

    for name, entry in entries.items():
        factory = cast(Callable[[dict[str, Any]], Any], entry.load())
        configs: dict[str, dict[str, Any]] = {
            "models_dev": {"path": PACKAGED_MODEL_CATALOG},
            "telegram": {"token": "test-token"},
        }
        plugin = factory(configs.get(name, {}))
        if family == "channel":
            assert isinstance(plugin, AgentChannel)
        else:
            assert plugin.name == name
