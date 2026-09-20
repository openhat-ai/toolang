"""Convert catalog request options to mutable JSON wire values."""

from collections.abc import Mapping
import json
from typing import Any

from toolang.common.json import dumps


def request_options(options: Mapping[str, object]) -> dict[str, Any]:
    """Detach options and convert decimals only at the provider wire boundary."""

    return json.loads(dumps(options, indent=None))
