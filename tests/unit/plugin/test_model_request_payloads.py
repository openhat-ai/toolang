from __future__ import annotations

from toolang.base.protocols.model import InspectableModelAdapter
from toolang.base.types.message import Message
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall
from toolang.plugin.models.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
    chat_completion_payload,
)
from toolang.plugin.models.adapters.generate_content import (
    GenerateContentModelAdapter,
    generate_content_payload,
)
from toolang.plugin.models.adapters.messages import (
    MessagesModelAdapter,
    messages_payload,
)
from toolang.plugin.models.adapters.responses import (
    ResponsesModelAdapter,
    response_payload,
)


def test_bundled_adapters_project_through_their_transport_payload_builders() -> None:
    call = ModelCall(
        instructions="Review carefully.",
        messages=[Message.user("draft")],
    )
    responses_target = _target("responses", provider="openai")
    chat_target = _target("chat_completions", provider="openrouter")
    messages_target = _target("messages", provider="anthropic")
    generate_target = _target("generate_content", provider="google")
    adapters = (
        ResponsesModelAdapter(),
        ChatCompletionsModelAdapter(),
        MessagesModelAdapter(),
        GenerateContentModelAdapter(),
    )

    assert all(isinstance(adapter, InspectableModelAdapter) for adapter in adapters)
    assert adapters[0].request_payload(responses_target, call) == response_payload(
        responses_target,
        call,
        stateful=True,
    )
    assert adapters[1].request_payload(chat_target, call) == chat_completion_payload(
        chat_target,
        call,
        stream=True,
    )
    assert adapters[2].request_payload(messages_target, call) == messages_payload(
        messages_target,
        call,
        stream=True,
    )
    assert adapters[3].request_payload(generate_target, call) == (
        generate_content_payload(generate_target, call)
    )


def _target(adapter: str, *, provider: str) -> ModelTarget:
    return ModelTarget(
        ref=f"{provider}/model",
        provider=provider,
        name="Model",
        model="model",
        adapter=adapter,
        base_url="https://models.example/v1",
    )
