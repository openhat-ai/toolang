"""Read-only compatibility for retired durable control formats."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, cast

from pydantic import PlainSerializer, TypeAdapter

from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunLimits
from toolang.common.immutable import freeze_mapping, mutable_data
from .types import AgentResources

_LIMITS = TypeAdapter(RunLimits)
_MODEL = TypeAdapter(ModelRequest)


@dataclass(frozen=True, slots=True)
class LegacyRetryPayload:
    """Keep original history data and only decode effective rerun settings."""

    data: Mapping[str, object]
    resources: AgentResources
    limits: RunLimits
    model_request: ModelRequest | None

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "LegacyRetryPayload":
        resources, limits = data.get("resources"), data.get("limits")
        if not isinstance(resources, Mapping) or not isinstance(limits, Mapping):
            raise ValueError("legacy retry payload requires resources and limits")
        model = data.get("model_request")
        return cls(
            freeze_mapping(data),
            AgentResources.from_data(cast(Mapping[str, object], resources)),
            _LIMITS.validate_python(limits),
            _MODEL.validate_python(model) if model is not None else None,
        )

    def to_data(self) -> dict[str, object]:
        """Expose surviving historical facts without constructing new controls."""
        return mutable_data(self.data)


LegacyRetryPayloadField = Annotated[
    LegacyRetryPayload,
    PlainSerializer(lambda payload: payload.to_data(), return_type=dict[str, object]),
]
