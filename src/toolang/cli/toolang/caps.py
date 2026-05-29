"""Caps commands mounted under the main toolang CLI."""

from __future__ import annotations

import typer

from ..caps.commands import CAP_KINDS, register_toolang_caps_commands as _register_toolang_caps_commands

__all__ = ["CAP_KINDS", "register_caps_commands"]


def register_caps_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    _register_toolang_caps_commands(app, rich_help_panel=rich_help_panel)
