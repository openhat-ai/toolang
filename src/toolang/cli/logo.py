"""CLI logo and color palette constants."""

from __future__ import annotations

from rich.text import Text

INFO_AVATAR_TEXT = """
██████████                    ████
▀▀▀████▀▀▀                    ████
   ████     ▄▄▄▄     ▄▄▄▄     ████
   ████     ████     ████     ████
   ████     ▀▀▀▀     ▀▀▀▀     ████
   ████                   ▄▄▄▄████
   ████                   ████████
""".strip("\n")

INFO_AVATAR_MARGIN = " "

INFO_PALETTE_COLORS = (
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


def info_avatar() -> Text:
    """Return the styled CLI info avatar art."""

    return _rainbow_text(info_avatar_text())


def info_avatar_text() -> str:
    """Return the plain CLI info avatar art."""

    return _with_line_margin(INFO_AVATAR_TEXT, margin=INFO_AVATAR_MARGIN)


def info_palette() -> Text:
    """Return the styled two-row CLI info palette."""

    top, bottom = INFO_PALETTE_COLORS
    palette = Text()
    for style in _color_row_styles(top):
        palette.append("██", style=style)
    palette.append("\n")
    for style in _color_row_styles(bottom):
        palette.append("██", style=style)
    return palette


def _rainbow_text(text: str) -> Text:
    rainbow_styles = _color_row_styles(INFO_PALETTE_COLORS[0])
    styled = Text()
    for row, line in enumerate(text.splitlines()):
        for column, char in enumerate(line):
            if char == " ":
                styled.append(char)
                continue
            style_index = (column + (row * 2)) % len(rainbow_styles)
            styled.append(char, style=rainbow_styles[style_index])
        styled.append("\n")
    if styled.plain.endswith("\n"):
        styled = styled[:-1]
    return styled


def _with_line_margin(text: str, *, margin: str) -> str:
    return "\n".join(f"{margin}{line}{margin}" for line in text.splitlines())


def _color_row_styles(colors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"bold #{color}" for color in colors)
