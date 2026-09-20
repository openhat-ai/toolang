"""Resolve one model call's output allowance and input admission budget."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from toolang.base.types.model import Model, Reasoning


def output_budget(
    model: Model,
    *,
    demand: int | None = None,
    reasoning: Reasoning | None = None,
) -> int | None:
    """Resolve the inclusive output allowance for one model call.

    `None` means the call imposes no explicit allowance, so the provider applies
    its native maximum output allowance.
    """

    limit = model.limit.get("output")
    if demand is None:
        budget = limit
    elif limit is None:
        budget = demand
    else:
        budget = min(demand, limit)
    if budget is None:
        route = model._toolang.route
        options = route.options
        generation = options.get("generationConfig")
        raw = {
            "messages": options.get("max_tokens"),
            "responses": options.get("max_output_tokens"),
            "chat_completions": options.get(
                "max_completion_tokens", options.get("max_tokens")
            ),
            "generate_content": cast(Mapping[str, object], generation).get(
                "maxOutputTokens"
            )
            if isinstance(generation, Mapping)
            else None,
        }.get(route.adapter or "")
        if raw is not None:
            if type(raw) is not int or raw <= 0:
                raise ValueError("provider output allowance must be a positive integer")
            budget = raw
    if budget is None and model.limit.get("context") is not None:
        raise ValueError("known context requires max_output or a model output limit")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
    ):
        raise ValueError("max_output must be a positive integer")
    thinking = reasoning.budget_tokens if reasoning is not None else None
    if thinking is not None and budget is not None and budget <= thinking:
        raise ValueError(
            f"max_output {budget} must exceed the reasoning budget {thinking}"
        )
    return budget


def context_capacity(model: Model) -> int | None:
    """Return the provider's joint input+output window, when known."""

    return model.limit.get("context")


def input_budget(model: Model, output: int | None) -> int | None:
    """Reserve inclusive output and a 5% (at least 1024 token) input margin."""

    independent = model.limit.get("input")
    capacities: list[int] = []
    available: list[int] = []
    context = model.limit.get("context")
    if context is not None:
        if output is None:
            raise ValueError(
                "known context requires max_output or a model output limit"
            )
        capacities.append(context)
        available.append(context - output)
    if type(independent) is int and independent > 0:
        capacities.append(independent)
        available.append(independent)
    if not capacities:
        return None
    capacity = min(capacities)
    budget = min(available) - max(1024, (capacity + 19) // 20)
    if budget <= 0:
        raise ValueError(
            "model output allowance and estimation margin leave no input budget"
        )
    return budget
