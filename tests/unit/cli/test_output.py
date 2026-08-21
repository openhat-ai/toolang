from io import StringIO
import os
from pathlib import Path
import sys
import tempfile

import pytest
from rich.console import Console

from toolang.cli.common import output
from toolang.cli.common.output import (
    agent_avatar,
    echo_pairs_table,
    info_avatar_text,
    shorten_home_path,
    toolang_logo_text,
)


EXPECTED_INFO_AVATAR = """\
████           ██
 ██   ⬤   ⬤    ██
 ██          ████"""


def test_info_avatar_uses_compact_logo(monkeypatch) -> None:
    monkeypatch.setattr(output, "_INFO_CONSOLE", Console(color_system=None))

    assert toolang_logo_text() == EXPECTED_INFO_AVATAR
    assert info_avatar_text() == EXPECTED_INFO_AVATAR
    avatar = agent_avatar()

    assert avatar.plain == EXPECTED_INFO_AVATAR
    assert avatar.style == ""
    assert avatar.spans == []


def test_info_avatar_renders_solid_cells_with_terminal_background(
    monkeypatch,
) -> None:
    console = Console(
        force_terminal=True,
        color_system="standard",
        no_color=False,
        _environ={},
    )
    monkeypatch.setattr(output, "_INFO_CONSOLE", console)

    avatar = agent_avatar()

    assert avatar.plain == EXPECTED_INFO_AVATAR.replace("█", " ")
    for offset, character in enumerate(EXPECTED_INFO_AVATAR):
        style = avatar.get_style_at_offset(console, offset)
        assert bool(style.reverse) is (character == "█")
        if character in {"█", "⬤"}:
            assert style.color is None
            assert style.bgcolor is None


def test_info_layout_aligns_avatar_with_first_detail_row(monkeypatch) -> None:
    rendered = StringIO()
    monkeypatch.setattr(
        output,
        "_INFO_CONSOLE",
        Console(file=rendered, width=120, color_system=None),
    )
    monkeypatch.setattr(output.typer, "echo", lambda: rendered.write("\n"))

    echo_pairs_table(
        [("Home", "/tmp/eve"), ("Created", "now")],
        avatar="logo-one\nlogo-two\nlogo-3--",
        title="EVE",
    )

    lines = [line.rstrip() for line in rendered.getvalue().splitlines()]
    assert lines == [
        "",
        "               EVE",
        "               ---",
        "   logo-one    Home    /tmp/eve",
        "   logo-two    Created now",
        "   logo-3--",
        "",
    ]


def test_home_path_shortening_uses_the_user_home() -> None:
    path = Path.home() / ".toolang" / "agents" / "eve"

    assert shorten_home_path(path) == str(Path("~") / ".toolang" / "agents" / "eve")


def test_home_path_shortening_keeps_an_unrelated_absolute_path() -> None:
    path = Path.home().parent / "shared" / "toolang" / "agents" / "eve"

    assert shorten_home_path(path) == str(path.resolve(strict=False))


def test_home_path_shortening_uses_the_platform_temp_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    if os.name == "nt":
        monkeypatch.setenv("TEMP", str(temp_root))
    relative = Path("toolang-dev-84c363b2") / "agents" / "dev"
    path = temp_root / relative
    expected_root = "%TEMP%" if os.name == "nt" else "/tmp"

    assert shorten_home_path(path) == str(Path(expected_root) / relative)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS /tmp alias")
def test_home_path_shortening_normalizes_the_macos_private_tmp_alias() -> None:
    path = Path("/private/tmp/toolang-dev-84c363b2/agents/dev")

    assert shorten_home_path(path) == "/tmp/toolang-dev-84c363b2/agents/dev"
