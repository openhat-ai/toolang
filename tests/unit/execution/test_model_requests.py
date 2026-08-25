from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from toolang.base.types.message import Message
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import ModelInfo, ModelTarget, Provider, ResolvedProvider
from toolang.base.types.run import ModelCall
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.model_requests import build_model_request
from toolang.setup import AgentSetup


class _RequestAdapter:
    name = "request_test"
    description = "Request inspection test adapter."
    default_api = "https://example.test/v1"

    def request_payload(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> Mapping[str, object]:
        return {
            "model": target.model,
            "messages": [message.to_data() for message in request.messages],
            "api_key": "must-not-leak",
            "transport": {
                "headers": {"Authorization": "must-not-leak"},
                "password": "must-not-leak",
            },
        }

    async def invoke(self, target: ModelTarget, request: ModelCall):
        raise AssertionError((target, request))

    async def stream(self, target: ModelTarget, request: ModelCall, *, on_event):
        raise AssertionError((target, request, on_event))


def _setup(tmp_path: Path, *, adapter: object | None = None) -> AgentSetup:
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={},
        resolved=ResolvedProvider(
            adapter="request_test",
            api="https://example.test/v1",
            env=(),
            ready=True,
        ),
    )
    return AgentSetup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers={provider.id: provider},
        adapters={
            "request_test": cast(ModelAdapter, adapter or _RequestAdapter()),
        },
        models=(
            ModelInfo(
                ref="test/model-v1",
                provider="test",
                name="Model V1",
                model="model-v1",
                selectors=("test/model-v1",),
                adapter="request_test",
                streaming=False,
                metadata={"resolved_ready": True},
            ),
        ),
        tools={},
        envs={},
    )


def test_model_request_projects_an_exact_model_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    request = build_model_request(
        _setup(tmp_path),
        model_id="test/model-v1",
        call=ModelCall(instructions="System", messages=[Message.user("Hello")]),
    )

    assert request.model.ref == "test/model-v1"
    assert request.body["model"] == "model-v1"
    assert request.body["api_key"] == "<redacted>"
    assert request.body["transport"] == {
        "headers": {"Authorization": "<redacted>"},
        "password": "<redacted>",
    }


@pytest.mark.parametrize("model_id", ("model-v1", "test/model-*", "other/model"))
def test_model_request_rejects_non_exact_or_unknown_model_ids(
    tmp_path: Path,
    model_id: str,
) -> None:
    with pytest.raises(ToolangError):
        build_model_request(
            _setup(tmp_path),
            model_id=model_id,
            call=ModelCall(instructions="", messages=[]),
        )


def test_model_request_requires_an_inspectable_adapter(tmp_path: Path) -> None:
    with pytest.raises(ToolangError, match="does not support request inspection"):
        build_model_request(
            _setup(tmp_path, adapter=object()),
            model_id="test/model-v1",
            call=ModelCall(instructions="", messages=[]),
        )
