"""Resolve terminal-adaptive background colors before interactive input starts."""

from __future__ import annotations

import math
import os
import re
import select
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

RGB = tuple[int, int, int]

COLOR_SCHEME_ENV = "TOOLANG_COLOR_SCHEME"
DEFAULT_QUERY_TIMEOUT = 0.35
DEFAULT_CODE_CONTRAST = 1.05
DEFAULT_QUEUE_CONTRAST = 1.12
DEFAULT_INPUT_CONTRAST = 1.28
NEAR_BLACK_CODE_CONTRAST = 1.07
NEAR_BLACK_LUMINANCE = 0.005
MINIMUM_TEXT_CONTRAST = 4.5
_MIX_SEARCH_STEPS = 8192

_OSC_QUERY = b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
_OSC_REPLY = re.compile(
    rb"\x1b\](10|11);(rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/"
    rb"[0-9a-fA-F]{1,4})(?:\x07|\x1b\\)"
)
_EXPLICIT_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


@dataclass(frozen=True, slots=True)
class TerminalSurfaces:
    """Concrete backgrounds in user-input-to-run-output order."""

    input_background: str
    queue_background: str
    code_background: str


DARK_TERMINAL_SURFACES = TerminalSurfaces(
    input_background="#1f1f1f",
    queue_background="#121212",
    code_background="#0b0b0b",
)
LIGHT_TERMINAL_SURFACES = TerminalSurfaces(
    input_background="#e3e3e3",
    queue_background="#f2f2f2",
    code_background="#f9f9f9",
)


def resolve_terminal_surfaces(
    *,
    environment: Mapping[str, str] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    probe: bool = True,
) -> TerminalSurfaces:
    """Resolve an explicit scheme, OSC terminal defaults, or the dark fallback.

    This function must run before another reader owns terminal input. Importing
    the module performs no I/O. An invalid non-empty explicit scheme raises
    ``ValueError`` instead of silently selecting a fallback.
    """

    if not math.isfinite(timeout) or not 0.01 <= timeout <= 3:
        raise ValueError("timeout must be between 0.01 and 3 seconds")
    environ = os.environ if environment is None else environment
    configured = environ.get(COLOR_SCHEME_ENV, "").strip()
    if configured:
        return _configured_surfaces(configured)
    if not probe:
        return DARK_TERMINAL_SURFACES
    defaults = _query_terminal_defaults(
        input_stream if input_stream is not None else sys.stdin,
        output_stream if output_stream is not None else sys.stdout,
        timeout,
    )
    if defaults is None:
        return DARK_TERMINAL_SURFACES
    foreground, background = defaults
    return derive_terminal_surfaces(
        foreground=_hex_rgb(foreground),
        background=_hex_rgb(background),
    )


def derive_terminal_surfaces(*, foreground: str, background: str) -> TerminalSurfaces:
    """Derive subtle surfaces from concrete terminal default RGB colors."""

    fg = _parse_hex_rgb(foreground)
    bg = _parse_hex_rgb(background)
    minimum_text = min(MINIMUM_TEXT_CONTRAST, _contrast(fg, bg))
    code_target = (
        NEAR_BLACK_CODE_CONTRAST
        if _luminance(fg) > _luminance(bg) and _luminance(bg) <= NEAR_BLACK_LUMINANCE
        else DEFAULT_CODE_CONTRAST
    )
    input_amount, queue_amount, code_amount = (
        _surface_mix_amount(bg, fg, target)
        for target in (DEFAULT_INPUT_CONTRAST, DEFAULT_QUEUE_CONTRAST, code_target)
    )
    scale = _readable_mix_scale(
        bg,
        fg,
        maximum=input_amount,
        minimum_text=minimum_text,
    )
    if scale < 1:
        # Compress the strengths together instead of clamping them to one color.
        readable_input = input_amount * scale
        if readable_input > code_amount:
            interval_scale = (readable_input - code_amount) / (
                input_amount - code_amount
            )
            input_amount = readable_input
            queue_amount = code_amount + (queue_amount - code_amount) * interval_scale
        else:
            input_amount *= scale
            queue_amount *= scale
            code_amount *= scale
    code_color, queue_color, input_color = _ordered_surface_colors(
        bg,
        fg,
        amounts=(code_amount, queue_amount, input_amount),
        minimum_text=minimum_text,
    )
    return TerminalSurfaces(
        input_background=_hex_rgb(input_color),
        queue_background=_hex_rgb(queue_color),
        code_background=_hex_rgb(code_color),
    )


def _configured_surfaces(value: str) -> TerminalSurfaces:
    keyword = value.casefold()
    if keyword == "dark":
        return DARK_TERMINAL_SURFACES
    if keyword == "light":
        return LIGHT_TERMINAL_SURFACES
    colors = [item.strip() for item in value.split(",")]
    if len(colors) == 3 and all(_EXPLICIT_COLOR.fullmatch(item) for item in colors):
        input_background, queue_background, code_background = (
            item.casefold() for item in colors
        )
        return TerminalSurfaces(
            input_background=input_background,
            queue_background=queue_background,
            code_background=code_background,
        )
    raise ValueError(
        f"{COLOR_SCHEME_ENV} must be 'light', 'dark', or three #RRGGBB colors "
        "in input,queue,code order"
    )


def _query_terminal_defaults(
    input_stream: TextIO,
    output_stream: TextIO,
    timeout: float,
) -> tuple[RGB, RGB] | None:
    if os.name != "posix":
        return None
    try:
        if not input_stream.isatty() or not output_stream.isatty():
            return None
    except OSError:
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None

    original = None
    input_fd = None
    try:
        input_fd = input_stream.fileno()
        output_fd = output_stream.fileno()
        if os.ttyname(input_fd) != os.ttyname(output_fd):
            return None
        if select.select([input_fd], [], [], 0)[0]:
            return None
        original = termios.tcgetattr(input_fd)
        tty.setcbreak(input_fd, termios.TCSANOW)
        output_stream.flush()
        if select.select([input_fd], [], [], 0)[0]:
            return None
        os.write(output_fd, _OSC_QUERY)
        deadline = time.monotonic() + timeout
        data = b""
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([input_fd], [], [], remaining)[0]:
                break
            chunk = os.read(input_fd, 1024)
            if not chunk:
                break
            data += chunk
            replies = _parse_osc_replies(data)
            if 10 in replies and 11 in replies:
                return replies[10], replies[11]
            if len(data) > 8192:
                break
    except (OSError, termios.error):
        return None
    finally:
        if original is not None and input_fd is not None:
            try:
                termios.tcsetattr(input_fd, termios.TCSANOW, original)
            except (OSError, termios.error):
                pass
    return None


def _parse_osc_replies(data: bytes) -> dict[int, RGB]:
    replies: dict[int, RGB] = {}
    for match in _OSC_REPLY.finditer(data):
        try:
            replies[int(match[1])] = _parse_osc_rgb(match[2].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
    return replies


def _parse_osc_rgb(value: str) -> RGB:
    channels = value.removeprefix("rgb:").split("/")
    if len(channels) != 3 or any(
        not re.fullmatch(r"[0-9a-fA-F]{1,4}", channel) for channel in channels
    ):
        raise ValueError("invalid OSC RGB color")
    red, green, blue = (
        round(int(channel, 16) * 255 / (16 ** len(channel) - 1)) for channel in channels
    )
    return red, green, blue


def _parse_hex_rgb(value: str) -> RGB:
    if not _EXPLICIT_COLOR.fullmatch(value):
        raise ValueError("expected #RRGGBB")
    return (
        int(value[1:3], 16),
        int(value[3:5], 16),
        int(value[5:7], 16),
    )


def _hex_rgb(color: RGB) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in color)


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _encoded(value: float) -> int:
    encoded = (
        12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    )
    return round(max(0.0, min(1.0, encoded)) * 255)


def _luminance(color: RGB) -> float:
    return sum(
        weight * _linear(channel)
        for weight, channel in zip((0.2126, 0.7152, 0.0722), color)
    )


def _contrast(first: RGB, second: RGB) -> float:
    low, high = sorted((_luminance(first), _luminance(second)))
    return (high + 0.05) / (low + 0.05)


def _surface_mix_amount(
    background: RGB,
    foreground: RGB,
    target: float,
) -> float:
    base = _luminance(background)
    text = _luminance(foreground)
    if math.isclose(text, base):
        return 0.0
    desired = (
        target * (base + 0.05) - 0.05 if text > base else (base + 0.05) / target - 0.05
    )
    return max(0.0, min(1.0, (desired - base) / (text - base)))


def _mix_surface(background: RGB, foreground: RGB, fraction: float) -> RGB:
    red, green, blue = (
        _encoded(_linear(bg) + fraction * (_linear(fg) - _linear(bg)))
        for bg, fg in zip(background, foreground)
    )
    return red, green, blue


def _readable_mix_scale(
    background: RGB,
    foreground: RGB,
    *,
    maximum: float,
    minimum_text: float,
) -> float:
    if (
        maximum == 0
        or _contrast(
            foreground,
            _mix_surface(background, foreground, maximum),
        )
        >= minimum_text
    ):
        return 1.0
    low, high = 0.0, maximum
    for _ in range(20):
        middle = (low + high) / 2
        if (
            _contrast(
                foreground,
                _mix_surface(background, foreground, middle),
            )
            >= minimum_text
        ):
            low = middle
        else:
            high = middle
    return low / maximum


def _ordered_surface_colors(
    background: RGB,
    foreground: RGB,
    *,
    amounts: tuple[float, float, float],
    minimum_text: float,
) -> tuple[RGB, RGB, RGB]:
    direct = (
        _mix_surface(background, foreground, amounts[0]),
        _mix_surface(background, foreground, amounts[1]),
        _mix_surface(background, foreground, amounts[2]),
    )
    direct_strengths = tuple(_contrast(background, color) for color in direct)
    if (
        all(_contrast(foreground, color) >= minimum_text for color in direct)
        and direct_strengths[0] < direct_strengths[1] < direct_strengths[2]
    ):
        return direct

    maximum = amounts[-1]
    ranges: list[tuple[float, float, RGB]] = []
    previous: RGB | None = None
    start = 0.0
    end = 0.0
    for step in range(_MIX_SEARCH_STEPS + 1):
        fraction = maximum * step / _MIX_SEARCH_STEPS
        color = _mix_surface(background, foreground, fraction)
        if previous is None:
            previous = color
            start = fraction
        elif color != previous:
            ranges.append((start, end, previous))
            previous = color
            start = fraction
        end = fraction
    assert previous is not None
    ranges.append((start, end, previous))

    candidates: list[tuple[float, float, RGB]] = []
    strongest = 0.0
    for start, end, color in ranges:
        if _contrast(foreground, color) < minimum_text:
            continue
        strength = _contrast(background, color)
        if strength <= strongest:
            continue
        candidates.append((start, end, color))
        strongest = strength

    def distance(candidate: tuple[float, float, RGB], target: float) -> float:
        start, end, _ = candidate
        return start - target if target < start else target - end if target > end else 0

    if len(candidates) < len(amounts):
        selected = [
            min(candidates, key=lambda candidate: distance(candidate, target))[2]
            for target in amounts
        ]
        return selected[0], selected[1], selected[2]

    selected: list[RGB] = []
    lower = 0
    for position, target in enumerate(amounts):
        upper = len(candidates) - (len(amounts) - position)
        index = min(
            range(lower, upper + 1),
            key=lambda candidate_index: (
                distance(candidates[candidate_index], target),
                candidate_index,
            ),
        )
        selected.append(candidates[index][2])
        lower = index + 1
    return selected[0], selected[1], selected[2]
