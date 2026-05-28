"""CLI logo and color palette constants."""

from __future__ import annotations

INFO_AVATAR_LINES = (
    "                                    ",
    " ▄▄▄▄▄▄▄▄▄                        ▄▄▄ ",
    " ▀▀▀███▀▀▀                        ███ ",
    "    ███                           ███ ",
    "    ███       ███      ███        ███ ",
    "    ███       ▀▀▀      ▀▀▀        ███ ",
    "    ███                      ▄▄▄▄▄███ ",
    "    ▀▀▀                      ▀▀▀▀▀▀▀▀ ",
    "                                    ",
)

INFO_PALETTE_HEX_ROWS = (
    (
        "63c74d",
        "9edb49",
        "f4d35e",
        "f39c12",
        "f45b69",
        "ff66c4",
        "c77dff",
        "8e7dff",
        "5b8cff",
        "4cc9f0",
        "43d9bd",
    ),
    (
        "4fb03d",
        "89c93b",
        "e6c24f",
        "de8a0d",
        "de4a58",
        "f055b5",
        "b56aed",
        "7b6cf0",
        "4b79ee",
        "3cb7de",
        "36c4aa",
    ),
)


def info_avatar() -> str:
    """Return the CLI info avatar art."""

    return "\n".join(INFO_AVATAR_LINES)


def info_palette_styles() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the two-row CLI info palette as Rich styles."""

    top, bottom = INFO_PALETTE_HEX_ROWS
    return _palette_style_row(top), _palette_style_row(bottom)


def _palette_style_row(row: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"bold #{token}" for token in row)
