"""Resolve the output reservation using each adapter's native configuration."""

from collections.abc import Mapping

from toolang.base.types.model import ModelInfo, ModelTarget


def output_budget(target: ModelTarget, info: ModelInfo) -> int:
    options = target.options
    if target.adapter == "generate_content":
        generation = options.get("generationConfig", {})
        value = (
            generation.get("maxOutputTokens")
            if isinstance(generation, Mapping)
            else None
        )
    elif target.adapter == "responses":
        value = options.get("max_output_tokens")
    else:
        value = options.get("max_completion_tokens", options.get("max_tokens"))
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("configured output budget must be a positive integer")
    budget = value if isinstance(value, int) else 4096
    thinking = target.reasoning.get("budget_tokens")
    if target.adapter == "messages" and isinstance(thinking, int):
        if value is None:
            budget = max(budget, thinking + 1)
        elif thinking >= budget:
            raise ValueError("thinking budget must be smaller than the output budget")
    if info.max_output_tokens is not None:
        budget = min(budget, info.max_output_tokens)
    if (
        target.adapter == "messages"
        and isinstance(thinking, int)
        and thinking >= budget
    ):
        raise ValueError("thinking budget exceeds the model output limit")
    return budget


def input_budget(info: ModelInfo, output: int) -> int | None:
    """Reserve output and a 5% (at least 1024 token) estimation margin."""

    limits = info.metadata.get("limit", {})
    independent = limits.get("input") if isinstance(limits, Mapping) else None
    capacities = []
    if info.context_window is not None:
        capacities.append(info.context_window - output)
    if type(independent) is int and independent > 0:
        capacities.append(independent)
    if not capacities:
        return None
    capacity = min(capacities)
    return max(0, capacity - max(1024, (capacity + 19) // 20))
