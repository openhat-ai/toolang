"""Live test preparation must also work with the current offline setup contract."""

import asyncio

import pytest

from tests.support.live_provider import LIVE_PROVIDER_SOURCE, create_live_agent
from toolang.execution.runnables import resolve_runnable_reference


@pytest.mark.parametrize(
    "source,runnable",
    [
        (LIVE_PROVIDER_SOURCE, "agic:smoke"),
        ("flow alternate:\n  let result = Done\n", "flow:alternate"),
    ],
)
def test_live_agent_preparation_keeps_runnable_selection_at_the_call_site(
    tmp_path, source, runnable
):
    setup, state = asyncio.run(
        create_live_agent(tmp_path, model="test/offline", source=source)
    )

    assert setup.defaults.model is not None
    assert setup.defaults.model.ref == "test/offline"
    assert resolve_runnable_reference(state, runnable).ref == runnable.split(":", 1)[1]
