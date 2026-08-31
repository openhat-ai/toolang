"""Private run-limit accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelUsage
from toolang.common.errors import ToolangError
from toolang.execution.accounting import build_model_accounting, selected_usd_cost
from toolang.execution.types import ModelAccounting
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.models.resolution import model_target_ref


class _RunLimitExceeded(ToolangError):
    """Raised when one effective run limit has been exhausted."""


@dataclass(frozen=True, slots=True)
class _TokenPrice:
    """Captured USD price per input and output token."""

    input: Decimal | None = None
    output: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _ModelAccounting:
    """Accounting facts for one completed model call."""

    usage: ModelUsage | None
    price: _TokenPrice | None = None
    cost: Decimal | None = None
    accounting: ModelAccounting | None = None


@dataclass(slots=True)
class _RunLimitState:
    """Mutable root-run accounting shared by every child run."""

    limits: RunLimits
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)
    error: str | None = None

    def restore(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cost: Decimal | None,
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
                self.cost += cost

    def check_restored(self) -> None:
        """Validate restored effective totals before resumed execution."""

        if self.error is not None:
            raise _RunLimitExceeded(self.error)
        tokens = self.input_tokens + self.output_tokens
        if self.limits.tokens is not None and tokens > self.limits.tokens:
            raise _RunLimitExceeded(
                f"Run token limit exceeded: {tokens} > {self.limits.tokens}"
            )
        if self.limits.cost is not None and self.cost > self.limits.cost:
            raise _RunLimitExceeded(
                f"Run cost limit exceeded: {self.cost} > {self.limits.cost} USD"
            )

    def require_pricing(
        self,
        target: ModelTarget,
        models: ModelCollection,
    ) -> None:
        """Reject a priced run before invoking a model with unknown prices."""

        del target, models

    def record_model(
        self,
        target: ModelTarget,
        accounting: _ModelAccounting,
    ) -> None:
        """Record one completed model call and enforce root-tree totals."""

        if self.limits.tokens is None and self.limits.cost is None:
            return
        usage = accounting.usage
        if usage is None and self.limits.tokens is not None:
            raise ToolangError(
                f"Model usage is required by run token or cost limits: {target.ref}"
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
        if accounting.cost is None:
            return
        self.cost += accounting.cost
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


def _model_info(target: ModelTarget, models: ModelCollection) -> ModelInfo | None:
    ref = model_target_ref(target)
    return models.resolve(ref).info if models.contains(ref) else None


def _model_accounting(
    target: ModelTarget,
    models: ModelCollection,
    usage: ModelUsage | None,
) -> _ModelAccounting:
    info = _model_info(target, models)
    durable = build_model_accounting(target, usage, None, info=info)
    price = _accounting_price(durable) or _model_price(target, models)
    selected_cost = selected_usd_cost(durable)
    return _ModelAccounting(
        usage=usage,
        price=price,
        cost=selected_cost if selected_cost is not None else _model_cost(usage, price),
        accounting=durable,
    )


def _accounting_price(accounting: ModelAccounting | None) -> _TokenPrice | None:
    if accounting is None or accounting.estimate is None:
        return None
    input_rate: Decimal | None = None
    output_rate: Decimal | None = None
    for line in accounting.estimate.lines:
        rate = Decimal(line.rate) / Decimal(line.per)
        if line.meter in {"input", "input.uncached"}:
            input_rate = rate
        if line.meter in {"output", "output.visible"}:
            output_rate = rate
    if input_rate is None and output_rate is None:
        return None
    return _TokenPrice(input=input_rate, output=output_rate)


def _model_price(
    target: ModelTarget,
    models: ModelCollection,
) -> _TokenPrice | None:
    info = _model_info(target, models)
    if info is None or (info.input_price is None and info.output_price is None):
        return None
    return _TokenPrice(
        input=(
            Decimal(str(info.input_price)) if info.input_price is not None else None
        ),
        output=(
            Decimal(str(info.output_price)) if info.output_price is not None else None
        ),
    )


def _model_cost(
    usage: ModelUsage | None,
    price: _TokenPrice | None,
) -> Decimal | None:
    if usage is None or price is None or price.input is None or price.output is None:
        return None
    return price.input * usage.input_tokens + price.output * usage.output_tokens
