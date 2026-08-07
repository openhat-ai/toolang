"""Private run-limit accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelUsage, RunLimits
from toolang.common.errors import ToolangError


class _RunLimitExceeded(ToolangError):
    """Raised when one effective run limit has been exhausted."""


@dataclass(slots=True)
class _RunLimitState:
    """Mutable root-run accounting shared by every child run."""

    limits: RunLimits
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)
    error: str | None = None

    def require_pricing(
        self,
        target: ModelTarget,
        models: tuple[ModelInfo, ...],
    ) -> None:
        """Reject a priced run before invoking a model with unknown prices."""

        if self.limits.cost is None:
            return
        info = _model_info(target, models)
        if (
            info is None
            or info.input_price is None
            or info.output_price is None
        ):
            raise ToolangError(
                f"Model pricing is required by the run cost limit: {target.ref}"
            )

    def record_model(
        self,
        target: ModelTarget,
        models: tuple[ModelInfo, ...],
        usage: ModelUsage | None,
    ) -> None:
        """Record one completed model call and enforce root-tree totals."""

        if self.limits.tokens is None and self.limits.cost is None:
            return
        if usage is None:
            raise ToolangError(
                f"Model usage is required by run token or cost limits: {target.ref}"
            )
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        tokens = self.input_tokens + self.output_tokens
        if self.limits.tokens is not None and tokens > self.limits.tokens:
            raise _RunLimitExceeded(
                f"Run token limit exceeded: {tokens} > {self.limits.tokens}"
            )
        if self.limits.cost is None:
            return
        info = _model_info(target, models)
        if (
            info is None
            or info.input_price is None
            or info.output_price is None
        ):
            raise ToolangError(
                f"Model pricing is required by the run cost limit: {target.ref}"
            )
        self.cost += (
            Decimal(str(info.input_price)) * usage.input_tokens
            + Decimal(str(info.output_price)) * usage.output_tokens
        )
        if self.cost > self.limits.cost:
            raise _RunLimitExceeded(
                f"Run cost limit exceeded: {self.cost} > {self.limits.cost} USD"
            )

    def expire_time(self) -> None:
        """Record the root-run time-limit failure."""

        limit = self.limits.time
        if limit is None:
            raise RuntimeError("run time limit is disabled")
        self.error = f"Run time limit exceeded: {limit}s"


def _model_info(
    target: ModelTarget,
    models: tuple[ModelInfo, ...],
) -> ModelInfo | None:
    return next(
        (
            item
            for item in models
            if item.provider == target.provider and item.ref == target.ref
        ),
        None,
    )
