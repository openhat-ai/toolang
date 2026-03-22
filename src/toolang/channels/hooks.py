"""Hook binding lookup and decoding helpers."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.concepts.channel import InboundDelivery
from toolang.concepts.persisted.hooks_config import HookBinding, HooksConfig

from .contracts import HookRequest
from .load import create_channel_plugin


@dataclass(frozen=True, slots=True)
class HookMatch:
    """One matched hook binding and decoded inbound delivery."""

    name: str
    binding: HookBinding
    delivery: InboundDelivery


def find_hook_binding(
    config: HooksConfig,
    *,
    path: str,
    method: str,
) -> tuple[str, HookBinding] | None:
    """Find the first named hook binding matching one request."""

    normalized_method = method.strip().upper()
    for name, binding in config.hooks.items():
        if binding.path != path:
            continue
        if binding.method != normalized_method:
            continue
        return name, binding
    return None


def decode_hook_delivery(
    config: HooksConfig,
    *,
    path: str,
    method: str,
    headers: dict[str, str],
    query: dict[str, str],
    body: bytes,
    content_type: str | None = None,
) -> HookMatch | None:
    """Decode one hook request using the first matching hook binding."""

    matched = find_hook_binding(config, path=path, method=method)
    if matched is None:
        return None
    name, binding = matched
    plugin = create_channel_plugin(binding.plugin, config=binding.config)
    delivery = plugin.decode_hook(
        HookRequest(
            method=binding.method,
            path=path,
            headers=dict(headers),
            query=dict(query),
            body=body,
            content_type=content_type,
        )
    )
    if delivery is None:
        return None
    return HookMatch(name=name, binding=binding, delivery=delivery)
