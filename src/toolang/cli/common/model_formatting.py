"""Pure model presentation shared by CLI surfaces."""

from __future__ import annotations

from toolang.base.types.model import ModelRequest


def model_reasoning_value(model: ModelRequest) -> str | None:
    """Return one explicit effort level or token budget for display."""

    reasoning = model.reasoning
    if reasoning is None:
        return None
    if reasoning.effort is not None:
        return reasoning.effort
    return str(reasoning.budget_tokens) if reasoning.budget_tokens is not None else None
