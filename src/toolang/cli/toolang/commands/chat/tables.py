"""Width-aware layout for structured Chat result tables."""

from __future__ import annotations

from collections.abc import Sequence

from toolang.cli.common.execution_progress.formatting import display_width

_COLUMN_GAP = 2
_ELLIPSIS = "…"


def table_lines(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    width: int | None = None,
    shrink_order: Sequence[int] = (),
    protected_suffixes: Sequence[str | None] = (),
) -> tuple[str, ...]:
    """Lay out one table without wrapping any header or cell."""

    if not headers:
        return ()
    normalized_headers = tuple(_one_line(value) for value in headers)
    normalized_rows = tuple(
        tuple(_one_line(value) or "-" for value in row) for row in rows
    )
    if any(len(row) != len(normalized_headers) for row in normalized_rows):
        raise ValueError("table rows must match the header column count")
    suffixes = tuple(protected_suffixes) + (None,) * (
        len(normalized_headers) - len(protected_suffixes)
    )
    minimum_widths = tuple(
        max(1, display_width(suffix)) if suffix else 1 for suffix in suffixes
    )
    preferred = [
        max(
            display_width(header),
            *(display_width(row[index]) for row in normalized_rows),
        )
        for index, header in enumerate(normalized_headers)
    ]
    widths = _fit_widths(
        preferred,
        header_widths=tuple(display_width(header) for header in normalized_headers),
        minimum_widths=minimum_widths,
        width=width,
        shrink_order=shrink_order,
    )
    return (
        _row(normalized_headers, widths, suffixes=()),
        _separator(widths),
        *(_row(row, widths, suffixes=suffixes) for row in normalized_rows),
    )


def _fit_widths(
    preferred: Sequence[int],
    *,
    header_widths: Sequence[int],
    minimum_widths: Sequence[int],
    width: int | None,
    shrink_order: Sequence[int],
) -> tuple[int, ...]:
    widths = [max(1, value) for value in preferred]
    if width is None:
        return tuple(widths)
    gaps = _COLUMN_GAP * max(0, len(widths) - 1)
    target = max(sum(minimum_widths) + gaps, width)
    overflow = max(0, sum(widths) + gaps - target)
    order = [
        *(index for index in shrink_order if 0 <= index < len(widths)),
        *(index for index in range(len(widths)) if index not in shrink_order),
    ]
    for floors in (
        tuple(max(1, value) for value in header_widths),
        tuple(max(1, value) for value in minimum_widths),
    ):
        for index in order:
            reduction = min(max(0, widths[index] - floors[index]), overflow)
            widths[index] -= reduction
            overflow -= reduction
            if not overflow:
                break
        if not overflow:
            break
    return tuple(widths)


def _row(
    values: Sequence[str],
    widths: Sequence[int],
    *,
    suffixes: Sequence[str | None],
) -> str:
    cells: list[str] = []
    for index, (value, width) in enumerate(zip(values, widths, strict=True)):
        suffix = suffixes[index] if index < len(suffixes) else None
        fitted = _truncate(value, width=width, protected_suffix=suffix)
        cells.append(f"{fitted}{' ' * max(0, width - display_width(fitted))}")
    return (" " * _COLUMN_GAP).join(cells).rstrip()


def _separator(widths: Sequence[int]) -> str:
    return (" " * _COLUMN_GAP).join("─" * width for width in widths)


def _truncate(
    value: str,
    *,
    width: int,
    protected_suffix: str | None = None,
) -> str:
    if display_width(value) <= width:
        return value
    if protected_suffix and value.endswith(protected_suffix):
        suffix_width = display_width(protected_suffix)
        if width <= suffix_width:
            return protected_suffix
        return (
            _truncate(value[: -len(protected_suffix)], width=width - suffix_width)
            + protected_suffix
        )
    if width <= 1:
        return _ELLIPSIS
    available = width - display_width(_ELLIPSIS)
    pieces: list[str] = []
    used = 0
    for character in value:
        character_width = display_width(character)
        if used + character_width > available:
            break
        pieces.append(character)
        used += character_width
    return f"{''.join(pieces).rstrip()}{_ELLIPSIS}"


def _one_line(value: str) -> str:
    return " ".join(value.split())
