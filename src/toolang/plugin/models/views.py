"""Display-oriented model catalog views."""

from __future__ import annotations


from toolang.base.types.model import Model


def model_target_profile(model: Model) -> str:
    """Return a compact profile string for one selectable model."""

    parts: list[str] = [f"tools={'y' if model.tool_call else 'n'}"]
    context = model.limit.get("context")
    if context is not None:
        parts.append(f"ctx={_format_k(context)}")
    max_output = model.limit.get("output")
    if max_output is not None:
        parts.append(f"max_out={_format_k(max_output)}")
    cost = model.cost or {}
    input_price = _optional_price(cost.get("input"))
    output_price = _optional_price(cost.get("output"))
    if input_price is not None or output_price is not None:
        in_price = "-" if input_price is None else _format_price(input_price)
        out_price = "-" if output_price is None else _format_price(output_price)
        parts.append(f"price=${in_price}/${out_price}")
    return ", ".join(parts)


def _optional_price(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    return float(value)


def _format_k(value: int) -> str:
    if value >= 1_000_000:
        return f"{_format_decimal_unit(value / 1_000_000)}M"
    if value >= 1_000:
        return f"{_format_decimal_unit(value / 1_000)}k"
    return str(value)


def _format_decimal_unit(value: float) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_price(value: float) -> str:
    return f"{value:g}"
