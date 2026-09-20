"""Bounded USD amounts at the accounting and wire boundaries."""

from __future__ import annotations

import math
from collections.abc import Iterable
from fractions import Fraction

MICROS_PER_USD = 1_000_000
MAX_COST = 999_999_999.999999
_MAX_MICROS = 999_999_999_999_999


def cost_units(value: float) -> int:
    """Settle USD to integer micro-units, rounding decimal ties upward."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("cost must be a number")
    if value < 0 or value > MAX_COST or not math.isfinite(value):
        raise ValueError(
            "cost must be finite, non-negative, and at most 999999999.999999"
        )
    # Read the shortest decimal representation, avoiding a second binary rounding
    # when multiplying values near the upper bound or a half-micro boundary.
    mantissa, _, exponent = str(value).lower().partition("e")
    whole, _, fraction = mantissa.partition(".")
    coefficient = int(whole + fraction)
    shift = 6 + int(exponent or "0") - len(fraction)
    if shift >= 0:
        units = coefficient * 10**shift
    else:
        divisor = 10**-shift
        units = (coefficient * 2 + divisor) // (2 * divisor)
    if units > _MAX_MICROS:
        raise ValueError("cost exceeds 999999999.999999")
    return units


def normalize_cost(value: float) -> float:
    """Return one settled float amount with at most six fractional digits."""

    return cost_units(value) / MICROS_PER_USD


def cost_text(value: float) -> str:
    """Preserve decimal-text records without exposing binary rounding artifacts."""

    units = cost_units(value)
    whole, fraction = divmod(units, MICROS_PER_USD)
    return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".") if fraction else str(whole)


def number_text(value: float) -> str:
    """Write an unrounded rate or usage quantity, retaining small token prices."""

    text = str(value).removesuffix(".0")
    if "e" not in text:
        return text
    mantissa, exponent = text.split("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + int(exponent)
    if point <= 0:
        return "0." + "0" * -point + digits
    if point >= len(digits):
        return digits + "0" * (point - len(digits))
    return digits[:point] + "." + digits[point:]


def add_cost(left: float, right: float) -> float:
    """Add settled amounts exactly at micro-USD precision."""

    units = cost_units(left) + cost_units(right)
    if units > _MAX_MICROS:
        raise ValueError("cost exceeds 999999999.999999")
    return units / MICROS_PER_USD


def reject_boolean_cost(value: object) -> object:
    """Reject booleans before a schema coerces numeric or legacy text inputs."""

    if isinstance(value, bool):
        raise ValueError("cost must be a number, not a boolean")
    return value


def cost_from_rates(terms: Iterable[tuple[int, float]], *, per: int = 1) -> float:
    """Settle one call from decimal rates, without rounding individual lines."""

    # Rational arithmetic is confined to settlement. Catalogs and public values
    # remain floats; parse their shortest decimal form before multiplication.
    total = sum(
        (quantity * Fraction(str(rate)) for quantity, rate in terms), Fraction()
    )
    micros = total * MICROS_PER_USD / per
    if micros < 0 or micros > _MAX_MICROS:
        raise ValueError("cost must be non-negative and at most 999999999.999999")
    units = (2 * micros.numerator + micros.denominator) // (2 * micros.denominator)
    return units / MICROS_PER_USD
