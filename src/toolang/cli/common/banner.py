"""Pure, responsive startup panels shared by terminal clients."""

from dataclasses import dataclass
import re
import unicodedata

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .execution_progress.formatting import display_width
from .output import toolang_logo, toolang_logo_text

_ESCAPE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]"
)
_HORIZONTAL_PADDING = 2
_COLUMN_GAP = 4
_FIELD_GAP = 2
_MIN_WIDE_WIDTH = 69


def version_label(version: str) -> str:
    """Format an explicit process version without consulting the environment."""
    return version if version == "unknown" else "v" + version.removeprefix("v")


def _safe_text(value: str | Text, console: Console) -> Text:
    """Remove controls and collapse whitespace while preserving field styles."""
    if isinstance(value, str):
        value = Text(_ESCAPE.sub("", value))
    result = Text()
    escaped = set()
    for match in _ESCAPE.finditer(value.plain):
        escaped.update(range(*match.span()))
    for index, character in enumerate(value.plain):
        if index in escaped:
            continue
        if character.isspace():
            if result and not result.plain.endswith(" "):
                result.append(" ", value.get_style_at_offset(console, index))
        elif unicodedata.category(character) not in {"Cc", "Cf", "Cs"}:
            result.append(character, value.get_style_at_offset(console, index))
    result.rstrip()
    return result


@dataclass(frozen=True, slots=True)
class Banner:
    caption: str
    fields: tuple[tuple[str, str | Text], ...]

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, options.max_width)
        caption = _safe_text(self.caption, console)
        caption.style = "not bold not dim"
        fields = tuple((key, _safe_text(value, console)) for key, value in self.fields)
        logo_width = max(
            display_width(line) for line in toolang_logo_text().splitlines()
        )
        inset = 2 + 2 * _HORIZONTAL_PADDING
        if width < logo_width + inset:
            yield caption
            for key, value in fields:
                yield Text(key, style="dim")
                yield value
            return

        key_width = max(display_width(key) for key, _ in fields)
        details = Table.grid(padding=(0, _FIELD_GAP))
        if width - inset <= key_width + _FIELD_GAP:
            details.add_column(overflow="fold")
            for key, value in fields:
                details.add_row(Text(key, style="dim"))
                details.add_row(value)
        else:
            details.add_column(no_wrap=True)
            details.add_column(overflow="fold")
            for key, value in fields:
                details.add_row(Text(key, style="dim"), value)

        details_width = (
            key_width + _FIELD_GAP + max(value.cell_len for _, value in fields)
        )
        wide_width = inset + logo_width + _COLUMN_GAP + details_width
        wide = width >= max(_MIN_WIDE_WIDTH, wide_width)
        content = Table.grid(padding=(0, _COLUMN_GAP) if wide else 0)
        content.add_column(no_wrap=wide, vertical="top")
        if wide:
            content.add_column(vertical="top")
            content.add_row(toolang_logo(console), details)
        else:
            content.add_row(toolang_logo(console))
            content.add_row(Text())
            content.add_row(details)

        title_fits = caption.cell_len + 4 <= width
        natural_width = wide_width if wide else inset + max(logo_width, details_width)
        yield Panel(
            content if title_fits else Group(caption, Text(), content),
            title=caption if title_fits else None,
            title_align="left",
            width=min(width, max(natural_width, caption.cell_len + 4))
            if title_fits
            else None,
            box=box.ROUNDED,
            border_style="dim",
            padding=(1, _HORIZONTAL_PADDING),
            expand=False,
        )
