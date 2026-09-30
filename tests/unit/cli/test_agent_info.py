"""Agent info summarizes resources from the unified setup accessors."""

from pathlib import Path

import pytest

from tests.support.setup import materialized_setup
from toolang.base.types.model import Model, ModelToolang, Provider
from toolang.cli.toolang.commands import agent
from toolang.common.layout import AgentLayout
from toolang.plugin.models import query
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentSetup


def _setup(tmp_path: Path, refs: tuple[str, ...]) -> AgentSetup:
    return materialized_setup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers=tuple(
            Provider(id=name, name=name)
            for name in dict.fromkeys(ref.partition("/")[0] for ref in refs)
        ),
        adapters={},
        models=tuple(
            Model(
                id=ref.partition("/")[2],
                name=ref,
                _toolang=ModelToolang(provider=ref.partition("/")[0], ready=True),
            )
            for ref in refs
        ),
        tools=ToolCollection(),
        envs={},
    )


def test_large_model_summary_does_not_use_query_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = tuple(f"provider{index % 5}/model{index}" for index in range(1000))
    setup = _setup(tmp_path, refs)
    monkeypatch.setattr(
        query,
        "match_model_branches",
        lambda *args, **kwargs: pytest.fail("runtime model refs are not queries"),
    )

    assert agent._models_summary(setup) == "1000 models, 5 providers"


@pytest.mark.parametrize(
    ("refs", "expected"),
    [
        ((), "0 models, 0 providers"),
        (("one/a",), "1 model, 1 provider"),
        (("one/a", "one/b"), "2 models, 1 provider"),
        (("one/a", "two/b"), "2 models, 2 providers"),
    ],
)
def test_model_summary_counts_effective_setup_resources(
    tmp_path: Path, refs: tuple[str, ...], expected: str
) -> None:
    assert agent._models_summary(_setup(tmp_path, refs)) == expected
