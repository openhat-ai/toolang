"""Normalize model usage and estimate auditable catalog costs."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, cast

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelInfo, ModelTarget
from toolang.base.types.run import ModelUsage

from .types import (
    ModelAccounting,
    ModelCost,
    ModelCostLine,
    ModelPricing,
    ModelReasoningAccounting,
    ModelUsageMeter,
)

_PER_MILLION = Decimal(1_000_000)
_LOCAL_API_TOKEN_RATE_NAMES = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "input_audio",
    "output_audio",
)


def build_model_accounting(
    target: ModelTarget,
    usage: ModelUsage | None,
    catalog: ModelCatalogSnapshot | None,
    *,
    info: ModelInfo | None = None,
) -> ModelAccounting | None:
    """Build one versioned accounting value from observed usage and catalog rates."""

    if usage is None:
        return None
    model = catalog.find(target.provider, target.model) if catalog is not None else None
    rates, plan, match = _selected_rates(
        model,
        info=info,
        target=target,
        usage=usage,
    )
    estimate = (
        _estimate_cost(
            usage,
            rates,
            force_partial=_has_unsupported_billing(usage.billing),
        )
        if rates is not None
        else None
    )
    reported = (
        ModelCost(
            amount=_decimal_text(usage.reported_cost),
            currency=usage.reported_currency or "USD",
            complete=True,
        )
        if usage.reported_cost is not None
        else None
    )
    selected = _selected_cost_source(reported=reported, estimate=estimate)
    return ModelAccounting(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        meters=_usage_meters(usage),
        reasoning=ModelReasoningAccounting(
            requested=dict(target.reasoning) or None,
            selected=None,
        ),
        pricing=(
            ModelPricing(
                source=target.catalog
                or (model.catalog if model is not None else None)
                or "unknown",
                revision=target.catalog_revision
                or (catalog.revision if catalog is not None else None),
                plan=plan,
                match=match,
            )
            if rates is not None or usage.billing
            else None
        ),
        reported=reported,
        estimate=estimate,
        selected=selected,
    )


def selected_usd_cost(accounting: ModelAccounting | None) -> Decimal | None:
    """Return the selected USD amount for limits and summary projections."""

    if accounting is None:
        return None
    selected = (
        accounting.reported
        if accounting.selected == "reported"
        else accounting.estimate
        if accounting.selected == "estimated"
        else None
    )
    if selected is None or selected.currency.upper() != "USD":
        return None
    return Decimal(selected.amount)


def _selected_cost_source(
    *,
    reported: ModelCost | None,
    estimate: ModelCost | None,
) -> Literal["reported", "estimated", "none"]:
    if reported is not None and reported.currency.upper() == "USD":
        return "reported"
    if estimate is not None and estimate.currency.upper() == "USD":
        return "estimated"
    if reported is not None:
        return "reported"
    if estimate is not None:
        return "estimated"
    return "none"


def selected_cost_is_approximate(accounting: ModelAccounting | None) -> bool:
    """Return whether the selected cost needs an approximation marker."""

    if accounting is None or accounting.selected == "reported":
        return False
    estimate = accounting.estimate
    if accounting.selected != "estimated" or estimate is None:
        return True
    exact_zero = (
        estimate.complete
        and bool(estimate.lines)
        and all(Decimal(line.rate) == 0 for line in estimate.lines)
    )
    return not exact_zero


def cache_hit_ratio(accounting: ModelAccounting | None) -> Decimal | None:
    """Return an exact cache-read ratio when both numerator and total are known."""

    if accounting is None or accounting.input_tokens <= 0:
        return None
    meter = next(
        (item for item in accounting.meters if item.name == "input.cache_read"),
        None,
    )
    if meter is None:
        return None
    return Decimal(meter.quantity) / Decimal(accounting.input_tokens)


def _selected_rates(
    model: Model | None,
    *,
    info: ModelInfo | None,
    target: ModelTarget,
    usage: ModelUsage,
) -> tuple[Mapping[str, object] | None, str, dict[str, object]]:
    match: dict[str, object] = {}
    if usage.billing:
        match["billing"] = dict(sorted(usage.billing.items()))
    metadata = info.metadata if info is not None else {}
    raw_cost = model.cost if model is not None else metadata.get("cost")
    if not isinstance(raw_cost, Mapping):
        return None, "standard", match
    rates = cast(Mapping[str, object], raw_cost)
    plan = "standard"
    requested_mode = target.mode
    experimental = (
        model.experimental if model is not None else metadata.get("experimental")
    )
    if isinstance(requested_mode, str) and isinstance(experimental, Mapping):
        modes = experimental.get("modes")
        mode = (
            cast(Mapping[str, object], modes).get(requested_mode)
            if isinstance(modes, Mapping)
            else None
        )
        if isinstance(mode, Mapping):
            plan = requested_mode
            match["mode"] = requested_mode
            mode_cost = cast(Mapping[str, object], mode).get("cost")
            if isinstance(mode_cost, Mapping):
                rates = cast(Mapping[str, object], mode_cost)
    tiers = rates.get("tiers")
    selected_tier: Mapping[str, object] | None = None
    if isinstance(tiers, tuple | list):
        for item in tiers:
            if not isinstance(item, Mapping):
                continue
            item_data = cast(Mapping[str, object], item)
            condition = item_data.get("tier")
            if not isinstance(condition, Mapping):
                continue
            condition_data = cast(Mapping[str, object], condition)
            if condition_data.get("type") != "context":
                continue
            size = condition_data.get("size")
            if (
                isinstance(size, int)
                and not isinstance(size, bool)
                and usage.input_tokens > size
            ):
                selected_tier = item_data
    if selected_tier is not None:
        rates = {**rates, **selected_tier}
        rates = {key: value for key, value in rates.items() if key != "tier"}
        match["tier"] = dict(cast(Mapping[str, object], selected_tier["tier"]))
    local = model.local if model is not None else metadata.get("local") is True
    if local:
        rates = {
            **{name: 0 for name in _LOCAL_API_TOKEN_RATE_NAMES},
            **rates,
        }
    return rates, plan, match


def _estimate_cost(
    usage: ModelUsage,
    rates: Mapping[str, object],
    *,
    force_partial: bool = False,
) -> ModelCost | None:
    lines: list[ModelCostLine] = []
    complete = not force_partial

    cache_read = usage.input_cache_read_tokens
    cache_write = usage.input_cache_write_tokens
    uncached = usage.input_uncached_tokens
    if uncached is None and cache_read is not None:
        uncached = max(usage.input_tokens - cache_read - (cache_write or 0), 0)
    input_audio = usage.input_audio_tokens
    if input_audio is not None and _rate(rates, "input_audio") is not None:
        _append_line(lines, "input.audio", input_audio, _rate(rates, "input_audio"))
        if uncached is not None:
            uncached = max(uncached - input_audio, 0)
    if cache_read is not None and _rate(rates, "cache_read") is not None:
        _append_line(lines, "input.cache_read", cache_read, _rate(rates, "cache_read"))
    elif cache_read not in {None, 0}:
        complete = False
    if cache_write is not None and _rate(rates, "cache_write") is not None:
        _append_line(
            lines, "input.cache_write", cache_write, _rate(rates, "cache_write")
        )
    elif cache_write not in {None, 0}:
        complete = False
    input_rate = _rate(rates, "input")
    if input_rate is not None:
        if uncached is not None:
            _append_line(lines, "input.uncached", uncached, input_rate)
        else:
            input_quantity = usage.input_tokens
            if input_audio is not None and _rate(rates, "input_audio") is not None:
                input_quantity = max(input_quantity - input_audio, 0)
            _append_line(lines, "input", input_quantity, input_rate)
            if _rate(rates, "cache_read") is not None:
                complete = False
    else:
        complete = False

    reasoning = usage.output_reasoning_tokens
    visible = usage.output_visible_tokens
    if visible is None and reasoning is not None:
        visible = max(usage.output_tokens - reasoning, 0)
    output_audio = usage.output_audio_tokens
    if output_audio is not None and _rate(rates, "output_audio") is not None:
        _append_line(lines, "output.audio", output_audio, _rate(rates, "output_audio"))
        if visible is not None:
            visible = max(visible - output_audio, 0)
    reasoning_rate = _rate(rates, "reasoning")
    output_rate = _rate(rates, "output")
    if reasoning_rate is not None:
        if reasoning is None:
            if output_rate is not None:
                output_quantity = usage.output_tokens
                if (
                    output_audio is not None
                    and _rate(rates, "output_audio") is not None
                ):
                    output_quantity = max(output_quantity - output_audio, 0)
                _append_line(lines, "output", output_quantity, output_rate)
                complete = complete and reasoning_rate == output_rate
            else:
                complete = False
        else:
            _append_line(lines, "output.reasoning", reasoning, reasoning_rate)
            if output_rate is not None and visible is not None:
                _append_line(lines, "output.visible", visible, output_rate)
            elif visible not in {None, 0}:
                complete = False
    elif output_rate is not None:
        output_quantity = usage.output_tokens
        if output_audio is not None and _rate(rates, "output_audio") is not None:
            output_quantity = max(output_quantity - output_audio, 0)
        _append_line(lines, "output", output_quantity, output_rate)
    else:
        complete = False

    known_names = {
        "input.uncached",
        "input.cache_read",
        "input.cache_write",
        "input.audio",
        "output.visible",
        "output.reasoning",
        "output.audio",
    }
    if any(meter.name not in known_names for meter in usage.meters):
        complete = False
    if not lines:
        return None
    amount = sum((Decimal(line.amount) for line in lines), Decimal(0))
    return ModelCost(
        amount=_decimal_text(amount),
        currency="USD",
        complete=complete,
        lines=tuple(lines),
    )


def _has_unsupported_billing(billing: Mapping[str, str]) -> bool:
    supported = {
        "service_tier": {"default", "standard"},
        "traffic_type": {"on_demand"},
    }
    return any(
        value not in supported.get(name, set()) for name, value in billing.items()
    )


def _usage_meters(usage: ModelUsage) -> tuple[ModelUsageMeter, ...]:
    meters: list[ModelUsageMeter] = []
    for name, value in (
        ("input.uncached", usage.input_uncached_tokens),
        ("input.cache_read", usage.input_cache_read_tokens),
        ("input.cache_write", usage.input_cache_write_tokens),
        ("input.audio", usage.input_audio_tokens),
        ("output.visible", usage.output_visible_tokens),
        ("output.reasoning", usage.output_reasoning_tokens),
        ("output.audio", usage.output_audio_tokens),
    ):
        if value is not None:
            meters.append(ModelUsageMeter(name=name, quantity=str(value), unit="token"))
    meters.extend(
        ModelUsageMeter(
            name=meter.name,
            quantity=_decimal_text(meter.quantity),
            unit=meter.unit,
        )
        for meter in usage.meters
    )
    return tuple(meters)


def _rate(rates: Mapping[str, object], name: str) -> Decimal | None:
    value = rates.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    return Decimal(str(value))


def _append_line(
    lines: list[ModelCostLine],
    meter: str,
    quantity: int,
    rate: Decimal | None,
) -> None:
    if rate is None:
        return
    amount = Decimal(quantity) * rate / _PER_MILLION
    lines.append(
        ModelCostLine(
            meter=meter,
            quantity=str(quantity),
            unit="token",
            rate=_decimal_text(rate),
            per=str(_PER_MILLION),
            amount=_decimal_text(amount),
        )
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
