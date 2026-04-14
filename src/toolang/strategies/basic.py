"""Basic run strategy with a baseline model-tool loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.error import ToolangError
from toolang.base.protocols.strategy import RunContext
from toolang.base.types.run import RunResult

MAX_TOOL_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class BasicRunStrategy:
    """Default baseline run strategy."""

    name: str = "basic"

    def run(self, context: RunContext) -> RunResult:
        """Run the baseline model-tool loop."""

        for _ in range(MAX_TOOL_ROUNDS):
            model_call = context.call_model()
            if not model_call.tool_calls:
                return context.finish()
            context.call_tools(model_call.tool_calls)
        raise ToolangError("Model tool loop exceeded the maximum number of rounds.")


STRATEGY = BasicRunStrategy()


def create_strategy(config: Mapping[str, object]) -> BasicRunStrategy:
    """Create the built-in basic run strategy."""

    del config
    return STRATEGY
