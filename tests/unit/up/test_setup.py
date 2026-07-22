from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from toolang.up.setup import AgentSetup


def test_agent_setup_copies_and_freezes_implementation_mappings() -> None:
    tools = {"shell": cast(Any, object())}
    providers = {"openai": cast(Any, object())}
    adapters = {"responses": cast(Any, object())}
    environ = {"OPENAI_API_KEY": "secret"}

    setup = AgentSetup(
        name="alice",
        home=Path("/agents/alice"),
        tools=tools,
        model_providers=providers,
        model_adapters=adapters,
        model_environ=environ,
    )
    tools.clear()
    providers.clear()
    adapters.clear()
    environ.clear()

    assert tuple(setup.tools) == ("shell",)
    assert tuple(setup.model_providers) == ("openai",)
    assert tuple(setup.model_adapters) == ("responses",)
    assert setup.model_environ == {"OPENAI_API_KEY": "secret"}
    with pytest.raises(TypeError):
        cast(dict[str, object], setup.tools)["other"] = object()
