from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message
from toolang.base.types.model import (
    ModelInfo,
    ModelTarget,
    Provider,
    ResolvedProvider,
)
from toolang.base.types.run import ModelCall, ModelCallResult
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.model_requests import project_model_request
from toolang.setup import AgentSetup


class _InspectableAdapter:
    name = "test"
    description = None
    default_api = "https://models.example/v1"

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        raise AssertionError("request projection must not invoke the provider")

    async def stream(self, target, request, *, on_event) -> ModelCallResult:
        raise AssertionError("request projection must not stream from the provider")

    def request_payload(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> Mapping[str, object]:
        return {
            "model": target.model,
            "input": request.instructions,
            "api_key": "body-secret",
            "headers": {"Authorization": "header-secret"},
        }


class _UninspectableAdapter:
    name = "plain"
    description = None
    default_api = "https://models.example/v1"

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        raise AssertionError("unsupported projection must not invoke the provider")

    async def stream(self, target, request, *, on_event) -> ModelCallResult:
        raise AssertionError("unsupported projection must not stream the provider")


def test_project_model_request_requires_exact_identity_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)

    projection = project_model_request(
        setup,
        model_id="test/model",
        call=ModelCall(instructions="review", messages=[Message.user("draft")]),
    )

    assert projection.model.ref == "test/model"
    assert projection.payload == {
        "model": "model",
        "input": "review",
        "api_key": "<redacted>",
        "headers": {"Authorization": "<redacted>"},
    }


@pytest.mark.parametrize("model_id", ("model", "test/*", "default"))
def test_project_model_request_rejects_non_exact_model_ids(
    tmp_path: Path,
    model_id: str,
) -> None:
    with pytest.raises(ToolangError, match="exact provider/model_id"):
        project_model_request(
            _setup(tmp_path),
            model_id=model_id,
            call=ModelCall(instructions="", messages=[]),
        )


def test_project_model_request_rejects_unknown_and_unavailable_models(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    with pytest.raises(ToolangError, match="unknown model id"):
        project_model_request(
            setup,
            model_id="test/missing",
            call=ModelCall(instructions="", messages=[]),
        )

    unavailable = replace(
        setup,
        models=(
            replace(
                setup.models[0],
                metadata={"resolved_ready": False},
            ),
        ),
    )
    with pytest.raises(ToolangError, match="model is unavailable"):
        project_model_request(
            unavailable,
            model_id="test/model",
            call=ModelCall(instructions="", messages=[]),
        )


def test_project_model_request_reports_unsupported_adapter_capability(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    adapter = _UninspectableAdapter()
    unsupported = replace(
        setup,
        adapters={adapter.name: adapter},
        models=(replace(setup.models[0], adapter=adapter.name),),
    )

    with pytest.raises(ToolangError, match="does not support request inspection"):
        project_model_request(
            unsupported,
            model_id="test/model",
            call=ModelCall(instructions="", messages=[]),
        )


def _setup(tmp_path: Path) -> AgentSetup:
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={},
        resolved=ResolvedProvider(
            adapter="test",
            api="https://models.example/v1",
            env=(),
            ready=True,
        ),
    )
    adapter = _InspectableAdapter()
    return AgentSetup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers={provider.id: provider},
        adapters={adapter.name: adapter},
        models=(
            ModelInfo(
                ref="test/model",
                provider="test",
                name="Model",
                model="model",
                adapter=adapter.name,
                metadata={
                    "resolved_ready": True,
                    "resolved_api": "https://models.example/v1",
                },
            ),
        ),
        tools={},
        envs={},
    )
