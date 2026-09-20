"""Private run-limit accounting."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.base.money import add_cost, cost_units
from toolang.base.types.model import Model
from toolang.base.types.policy import RunLimits
from toolang.common.errors import ToolangError
from toolang.execution.accounting import selected_usd_cost
from toolang.execution.types import ModelAccounting
from toolang.plugin.models.collections import ModelCollection


class _RunLimitExceeded(ToolangError):
    """Raised when one effective run limit has been exhausted."""


@dataclass(slots=True)
class _RunLimitState:
    """Mutable root-run accounting shared by every child run."""

    limits: RunLimits
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    error: str | None = None

    def restore(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cost: float | None,
    ) -> None:
        """Restore one effective committed model call into root totals."""

        if self.limits.tokens is not None:
            if input_tokens is None or output_tokens is None:
                self.error = "Model usage is required by the run token limit"
                return
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
        if self.limits.cost is not None:
            if cost is not None:
                self.cost = add_cost(self.cost, cost)

    def check_restored(self) -> None:
        """Validate restored effective totals before resumed execution."""

        if self.error is not None:
            raise _RunLimitExceeded(self.error)
        tokens = self.input_tokens + self.output_tokens
        if self.limits.tokens is not None and tokens > self.limits.tokens:
            raise _RunLimitExceeded(
                f"Run token limit exceeded: {tokens} > {self.limits.tokens}"
            )
        if self.limits.cost is not None and cost_units(self.cost) > cost_units(
            self.limits.cost
        ):
            raise _RunLimitExceeded(
                f"Run cost limit exceeded: {self.cost} > {self.limits.cost} USD"
            )

    def require_pricing(
        self,
        model: Model,
        models: ModelCollection,
    ) -> None:
        """Reject a priced run before invoking a model with unknown prices."""

        del model, models

    def record_model(
        self,
        model: Model,
        accounting: ModelAccounting | None,
    ) -> None:
        """Record one completed model call and enforce root-tree totals."""

        if self.limits.tokens is None and self.limits.cost is None:
            return
        usage = accounting
        if usage is None and self.limits.tokens is not None:
            raise ToolangError(
                f"Model usage is required by run token or cost limits: {model.ref}"
            )
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
        tokens = self.input_tokens + self.output_tokens
        if self.limits.tokens is not None and tokens > self.limits.tokens:
            raise _RunLimitExceeded(
                f"Run token limit exceeded: {tokens} > {self.limits.tokens}"
            )
        if self.limits.cost is None:
            return
        cost = selected_usd_cost(accounting)
        if cost is None:
            return
        self.cost = add_cost(self.cost, cost)
        if cost_units(self.cost) > cost_units(self.limits.cost):
            raise _RunLimitExceeded(
                f"Run cost limit exceeded: {self.cost} > {self.limits.cost} USD"
            )

    def expire_time(self) -> None:
        """Record the root-run time-limit failure."""

        limit = self.limits.time
        if limit is None:
            raise RuntimeError("run time limit is disabled")
        self.error = f"Run time limit exceeded: {limit}s"
