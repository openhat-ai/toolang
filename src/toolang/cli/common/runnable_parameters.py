"""Map authored runnable signatures to native Typer parameter metadata."""

from collections.abc import Mapping
from inspect import Parameter as SignatureParameter, Signature
from typing import Annotated, Any

import typer
from typer._click import Context, Parameter as CliParameter
from typer._click.types import StringParamType
from typer.core import TyperArgument
from typer.main import get_click_param
from typer.utils import get_params_from_function

from toolang.lang.ast import AgicDecl, FlowDecl, Parameter


class _InputType(StringParamType):
    """Display the authored type while leaving coercion to input processing."""

    def __init__(self, name: str) -> None:
        self.name = name.upper()

    def get_metavar(self, param: CliParameter, ctx: Context) -> str:
        return self.name


class RunnableArgument(TyperArgument):
    """Signature metadata for help; the command's collector owns parsing."""

    def __init__(self, argument: TyperArgument) -> None:
        super().__init__(
            param_decls=argument.opts,
            type=argument.type,
            required=argument.required,
            metavar=argument.metavar,
            help=argument.help,
            show_default=argument.show_default,
            expose_value=False,
        )

    def add_to_parser(self, parser: Any, ctx: Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: Context,
        opts: Mapping[str, Any],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        # The command describes its capture syntax, independently of the rows.
        return []


def runnable_parameters(
    runnable: AgicDecl | FlowDecl,
    *,
    input_help: str | None = None,
    help_only: bool = False,
) -> list[TyperArgument]:
    """Build named arguments in signature order, followed by accepted input.

    Names, authored types, requiredness, and docs come from the runnable.
    Missing docs fall back to the input/argument terminology.
    Commands supply any input-capture help and own capture and coercion.
    Use help_only when a collector parses NAME=VALUE rather than positional values.
    """
    arguments = [_argument(parameter) for parameter in runnable.params]
    if runnable.input is not None:
        arguments.append(_argument(runnable.input, input_help=input_help))
    if help_only:
        return [RunnableArgument(argument) for argument in arguments]
    return arguments


def _argument(parameter: Parameter, *, input_help: str | None = None) -> TyperArgument:
    primary = parameter.name == "_"
    doc = (parameter.doc or "").strip()
    help_text = doc or (
        "Primary input, or simply input"
        if primary
        else "Named input, or simply argument"
    )
    if primary and input_help:
        separator = " " if doc else "; "
        help_text = f"{help_text}{separator}{input_help}"
    annotation = Annotated[
        str,
        typer.Argument(
            metavar="INPUT" if primary else f"{parameter.name}=ARGUMENT",
            click_type=_InputType(parameter.type_name or "Part[]"),
            help=help_text,
            show_default=False,
        ),
    ]

    def declaration() -> None:
        """Supply the authored parameter's Annotated metadata to Typer."""

    setattr(
        declaration,
        "__signature__",
        Signature(
            [
                SignatureParameter(
                    "value",
                    kind=SignatureParameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=None if parameter.optional else SignatureParameter.empty,
                )
            ]
        ),
    )
    argument, _ = get_click_param(get_params_from_function(declaration)["value"])
    assert isinstance(argument, TyperArgument)
    # Authored names can be Python keywords, so restore them after construction.
    argument.name = parameter.name
    argument.opts = [parameter.name]
    return argument
