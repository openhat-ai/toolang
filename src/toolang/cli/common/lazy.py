"""Lazy Typer command assembly for multi-command CLI entry points."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

import typer
from typer._click import Command, Context
from typer.core import TyperCommand, TyperGroup
from typer.models import CompletionItem

from toolang.common.typer.ui import inherit_ui

from .help import CliCommand


class LazyCommand(TyperCommand):
    """Expose command metadata without loading its implementation."""

    def __init__(
        self,
        name: str,
        *,
        loader: Callable[[], Command],
        help: str,
        short_help: str | None = None,
        hidden: bool = False,
        rich_help_panel: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            help=help,
            short_help=short_help,
            hidden=hidden,
            rich_help_panel=rich_help_panel,
        )
        self._loader = loader
        self._loaded: Command | None = None

    def load(self) -> Command:
        """Load and cache the real command."""

        if self._loaded is None:
            command = self._loader()
            if command.name != self.name:
                raise RuntimeError(
                    f"lazy command name mismatch: expected {self.name!r}, "
                    f"got {command.name!r}"
                )
            self._loaded = command
        return self._loaded

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        """Parse one invocation with the real command."""

        command = inherit_ui(self.load(), parent)
        return command.make_context(info_name, args, parent=parent, **extra)

    def shell_complete(
        self,
        ctx: Context,
        incomplete: str,
    ) -> list[CompletionItem]:
        """Complete parameters from the real command."""

        return self.load().shell_complete(ctx, incomplete)


def lazy_typer_command(name: str, target: str, **kwargs: Any) -> LazyCommand:
    """Create a lazy proxy for one Typer command callback."""

    return _lazy_command(
        name,
        loader=lambda: _load_typer_command(name, target, kwargs),
        kwargs=kwargs,
    )


def lazy_typer_group(name: str, target: str, **kwargs: Any) -> LazyCommand:
    """Create a lazy proxy for one Typer command group or group factory."""

    return _lazy_command(
        name,
        loader=lambda: _load_typer_group(name, target, kwargs),
        kwargs=kwargs,
    )


def _lazy_command(
    name: str,
    *,
    loader: Callable[[], Command],
    kwargs: dict[str, Any],
) -> LazyCommand:
    help_text = kwargs.get("help")
    if not isinstance(help_text, str):
        raise TypeError(f"lazy command requires explicit help: {name}")
    return LazyCommand(
        name,
        loader=loader,
        help=help_text,
        short_help=kwargs.get("short_help"),
        hidden=bool(kwargs.get("hidden", False)),
        rich_help_panel=kwargs.get("rich_help_panel"),
    )


def _load_typer_command(
    name: str,
    target: str,
    kwargs: dict[str, Any],
) -> Command:
    callback = _load_target(target)
    command_app = _loader_app()
    command_kwargs = {**kwargs}
    command_kwargs.setdefault("cls", CliCommand)
    command_app.command(name, **command_kwargs)(callback)
    return _loaded_child(command_app, name)


def _load_typer_group(
    name: str,
    target: str,
    kwargs: dict[str, Any],
) -> Command:
    group = _load_target(target)
    if callable(group) and not isinstance(group, typer.Typer):
        group = group()
    if not isinstance(group, typer.Typer):
        raise TypeError(f"lazy command group target is not a Typer app: {target}")
    command_app = _loader_app()
    command_app.add_typer(group, name=name, **kwargs)
    return _loaded_child(command_app, name)


def _load_target(target: str) -> Any:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid lazy command target: {target}")
    return getattr(import_module(module_name), attribute)


def _loader_app() -> typer.Typer:
    command_app = typer.Typer(
        add_completion=False,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    command_app.callback()(_loader_callback)
    return command_app


def _loader_callback() -> None:
    pass


def _loaded_child(command_app: typer.Typer, name: str) -> Command:
    root = typer.main.get_command(command_app)
    if not isinstance(root, TyperGroup):
        raise TypeError("lazy command loader must assemble a command group")
    return root.commands[name]


__all__ = ["LazyCommand", "lazy_typer_command", "lazy_typer_group"]
