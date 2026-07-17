from __future__ import annotations

from typing import Any, cast

import pytest

from toolang.execution.setup import AgentSetup


def test_agent_setup_copies_and_freezes_implementation_mappings() -> None:
    tools = {"shell": cast(Any, object())}
    providers = {"openai": cast(Any, object())}
    adapters = {"responses": cast(Any, object())}

    setup = AgentSetup(
        tools=tools,
        model_providers=providers,
        model_adapters=adapters,
    )
    tools.clear()
    providers.clear()
    adapters.clear()

    assert tuple(setup.tools) == ("shell",)
    assert tuple(setup.model_providers) == ("openai",)
    assert tuple(setup.model_adapters) == ("responses",)
    with pytest.raises(TypeError):
        cast(dict[str, object], setup.tools)["other"] = object()
