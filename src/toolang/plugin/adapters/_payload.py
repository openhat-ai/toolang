"""Convert catalog request options to mutable JSON wire values."""

from collections.abc import Mapping
import json
from typing import Any, cast

from toolang.common.json import dumps


def request_options(options: Mapping[str, object]) -> dict[str, Any]:
    """Detach immutable catalog options at the provider wire boundary."""

    return json.loads(dumps(options, indent=None))


def output_allowance(
    options: Mapping[str, object], *names: str, sdk_extensions: bool = False
) -> int | None:
    """Read supported aliases at the adapter boundary, including SDK extensions."""

    extra = options.get("extra_body")
    sources = (
        [options, cast(Mapping[str, object], extra)]
        if sdk_extensions and isinstance(extra, Mapping)
        else [options]
    )
    values: list[int] = []
    for source in sources:
        for name in names:
            if name not in source:
                continue
            value = source[name]
            if value is None:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError("provider output allowance must be a positive integer")
            values.append(value)
    if len(set(values)) > 1:
        raise ValueError("conflicting provider output allowance aliases")
    return values[0] if values else None


def clear_options(payload: dict[str, Any], *names: str) -> None:
    """Prevent authored aliases or SDK extensions from overriding resolved controls."""

    extra = payload.get("extra_body")
    for name in names:
        payload.pop(name, None)
        if isinstance(extra, dict):
            extra.pop(name, None)
