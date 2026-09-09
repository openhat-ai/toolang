"""Opt selected scalar options into optional values on the pinned Typer parser."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from typer._click import Command, Context
from typer._click.parser import _Option, _OptionParser, _ParsingState, _normalize_opt
from typer.core import TyperCommand, TyperGroup, TyperOption


# Use with text options when the caller needs a distinct bare-selection marker.
# NUL cannot occur in an OS command-line argument, including an explicit value.
BARE_VALUE = "\0"


class OptionalValueParser(_OptionParser):
    """Parse optional scalar values; subclasses may customize input boundaries."""

    def __init__(self, ctx: Context, optional_values: Mapping[str, str]) -> None:
        super().__init__(ctx)
        self.optional_values = optional_values

    def _bare_value(self, option: _Option | None, state: _ParsingState) -> str | None:
        if (
            option is not None
            and option.dest in self.optional_values
            and (
                not state.rargs
                or (
                    len(state.rargs[0]) > 1 and state.rargs[0][:1] in self._opt_prefixes
                )
            )
        ):
            return self.optional_values[option.dest]
        return None

    def _match_long_opt(
        self, opt: str, explicit_value: str | None, state: _ParsingState
    ) -> None:
        if explicit_value is None:
            explicit_value = self._bare_value(self._long_opt.get(opt), state)
        super()._match_long_opt(opt, explicit_value, state)

    def _match_short_opt(self, arg: str, state: _ParsingState) -> None:
        # Only the first value-taking option in a cluster can consume a value.
        # Leave attached values and ordinary options to the native parser.
        for index, char in enumerate(arg[1:], start=1):
            option = self._short_opt.get(_normalize_opt(f"{arg[0]}{char}", self.ctx))
            if option is None:
                if self.ignore_unknown_options:
                    continue
                break
            if option.takes_value:
                if index == len(arg) - 1:
                    value = self._bare_value(option, state)
                    if value is not None:
                        state.rargs.insert(0, value)
                break
        super()._match_short_opt(arg, state)


class _OptionalValueSupport(Command):
    """Supply configured raw values for bare scalar options, by parameter name.

    Subclasses declare ``optional_values = {"parameter": "bare value"}`` and
    use an optional metavar such as ``[PATH]`` in the option declaration.
    Defaults, conversion, validation, callbacks, and completion remain native.
    Compose with an existing command class to retain its routing and help.
    Override ``parser_class`` with an ``OptionalValueParser`` subclass when
    the command also needs custom positional-input parsing.
    """

    optional_values: ClassVar[Mapping[str, str]] = {}
    parser_class: ClassVar[type[OptionalValueParser]] = OptionalValueParser

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        by_name = {param.name: param for param in self.params}
        for name, value in self.optional_values.items():
            param = by_name.get(name)
            if (
                not isinstance(param, TyperOption)
                or param.is_flag
                or param.count
                or param.multiple
                or param.nargs != 1
                or not isinstance(value, str)
            ):
                raise TypeError(
                    f"{name}: expected a scalar value option and a raw string"
                )

    def make_parser(self, ctx: Context) -> _OptionParser:
        if not self.optional_values:
            return super().make_parser(ctx)
        parser = self.parser_class(ctx, self.optional_values)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser


class OptionalValueCommand(_OptionalValueSupport, TyperCommand):
    """Opt a Typer command into configured optional scalar values."""


class OptionalValueGroup(_OptionalValueSupport, TyperGroup):
    """Opt a Typer group into configured optional scalar values."""
