"""Reusable optional values retain native Typer behavior outside bare options."""

from pathlib import Path
from typing import Annotated

import pytest
import typer
from typer._click import Context
from typer._click.core import ParameterSource
from typer.core import TyperArgument, TyperCommand, TyperOption
from typer.testing import CliRunner

from toolang.cli.common.options import OptionalValueCommand


class _ProbeCommand(OptionalValueCommand):
    optional_values = {"model": "auto", "budget": "10", "artifact": "."}


def _app(*, rich: bool = True) -> tuple[typer.Typer, dict[str, object]]:
    app = typer.Typer(add_completion=False, rich_markup_mode="rich" if rich else None)
    captured: dict[str, object] = {}

    @app.command(cls=_ProbeCommand)
    def probe(
        ctx: typer.Context,
        model: Annotated[
            str | None,
            typer.Option("--model", "-m", metavar="[TEXT]", envvar="OPTION_TEST_MODEL"),
        ] = None,
        budget: Annotated[
            int | None,
            typer.Option("--budget", "-b", metavar="[INTEGER]", min=0, max=20),
        ] = None,
        artifact: Annotated[
            Path | None,
            typer.Option(
                "--artifact", "-a", metavar="[PATH]", exists=True, resolve_path=True
            ),
        ] = None,
        label: Annotated[str | None, typer.Option("--label", "-l")] = None,
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
        files: Annotated[list[str] | None, typer.Argument()] = None,
    ) -> None:
        captured.update(
            model=model,
            budget=budget,
            artifact=artifact,
            label=label,
            verbose=verbose,
            files=files,
            source=ctx.get_parameter_source("model"),
        )

    return app, captured


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], {"model": None}),
        (["--model"], {"model": "auto"}),
        (["-m"], {"model": "auto"}),
        (["--model", "explicit"], {"model": "explicit"}),
        (["-m", "explicit"], {"model": "explicit"}),
        (["--model=explicit"], {"model": "explicit"}),
        (["-mexplicit"], {"model": "explicit"}),
        (["--model="], {"model": ""}),
        (["-m", ""], {"model": ""}),
        (["--model", "-"], {"model": "-"}),
        (["-m", "-"], {"model": "-"}),
        (["--model=--verbose"], {"model": "--verbose", "verbose": False}),
        (["-m--verbose"], {"model": "--verbose", "verbose": False}),
        (["--model", "--verbose"], {"model": "auto", "verbose": True}),
        (["-m", "-v"], {"model": "auto", "verbose": True}),
        (["-vm"], {"model": "auto", "verbose": True}),
        (["-vmexplicit"], {"model": "explicit", "verbose": True}),
        (["-mv"], {"model": "v", "verbose": False}),
        (["--model", "--label", "text"], {"model": "auto", "label": "text"}),
        (["--label", "--model"], {"model": None, "label": "--model"}),
        (["-lm"], {"model": None, "label": "m"}),
        (["--model", "--", "file"], {"model": "auto", "files": ["file"]}),
        (["-m", "--", "--model"], {"model": "auto", "files": ["--model"]}),
        (["--", "--model"], {"model": None, "files": ["--model"]}),
        (["file", "--model"], {"model": "auto", "files": ["file"]}),
        (["--model", "file"], {"model": "file", "files": None}),
        (["--model", "first", "-m"], {"model": "auto"}),
        (["-m", "--model", "last"], {"model": "last"}),
        (["--model", "--budget"], {"model": "auto", "budget": 10}),
        (["-b", "-m"], {"model": "auto", "budget": 10}),
        (["-b15"], {"budget": 15}),
    ],
)
def test_optional_values_preserve_aliases_boundaries_and_native_values(args, expected):
    app, captured = _app()
    result = CliRunner().invoke(app, args, env={"OPTION_TEST_MODEL": None})
    assert result.exit_code == 0, result.output
    assert expected.items() <= captured.items()


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "--unknown"],
        ["-m", "-x"],
        ["-xm"],
        ["--budget", "invalid"],
        ["--budget=21"],
        ["--budget=-1"],
        ["-b-1"],
        ["--artifact", "missing.whl"],
        ["--label"],
        ["-l"],
    ],
)
def test_optional_values_keep_native_usage_and_validation_errors(
    args, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    app, captured = _app()
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert not captured


@pytest.mark.parametrize("option", ["--artifact", "-a"])
@pytest.mark.parametrize("bare", [False, True])
def test_path_options_convert_bare_and_explicit_values(
    option, bare, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wheel.whl").touch()
    app, captured = _app()
    result = CliRunner().invoke(app, [option] if bare else [option, "wheel.whl"])
    assert result.exit_code == 0, result.output
    assert captured["artifact"] == (tmp_path if bare else tmp_path / "wheel.whl")
    assert isinstance(captured["artifact"], Path)


@pytest.mark.parametrize(
    ("args", "expected", "source"),
    [
        ([], "environment", "ENVIRONMENT"),
        (["-m"], "auto", "COMMANDLINE"),
        (["--model=value"], "value", "COMMANDLINE"),
    ],
)
def test_optional_values_preserve_environment_precedence_and_sources(
    args, expected, source
):
    app, captured = _app()
    result = CliRunner().invoke(app, args, env={"OPTION_TEST_MODEL": "environment"})
    assert result.exit_code == 0, result.output
    assert captured["model"] == expected
    assert captured["source"] is ParameterSource[source]


def test_optional_values_preserve_default_maps_and_declared_defaults():
    app, captured = _app()
    runner = CliRunner()
    for defaults, expected, source in [
        ({}, None, "DEFAULT"),
        ({"model": "mapped"}, "mapped", "DEFAULT_MAP"),
    ]:
        result = runner.invoke(
            app, [], default_map=defaults, env={"OPTION_TEST_MODEL": None}
        )
        assert result.exit_code == 0, result.output
        assert captured["model"] == expected
        assert captured["source"] is ParameterSource[source]


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize(
    "args", [["--help"], ["--model", "--help"], ["-m", "--help"], ["-a", "--help"]]
)
def test_optional_value_help_does_not_convert_or_execute(rich, args, monkeypatch):
    app, captured = _app(rich=rich)
    command = typer.main.get_command(app)
    artifact = next(param for param in command.params if param.name == "artifact")
    monkeypatch.setattr(
        type(artifact.type),
        "convert",
        lambda *_args, **_kwargs: pytest.fail("help converted a path"),
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "[TEXT]" in result.output and "[PATH]" in result.output
    assert not captured


def test_optional_value_completion_retains_native_completer():
    def complete(incomplete: str) -> list[str]:
        return [value for value in ("auto", "accurate") if value.startswith(incomplete)]

    class ModelCommand(OptionalValueCommand):
        optional_values = {"model": "auto"}

    app = typer.Typer(add_completion=False)

    @app.command(cls=ModelCommand)
    def probe(
        model: Annotated[str | None, typer.Option(autocompletion=complete)] = None,
    ):
        pass

    command = typer.main.get_command(app)
    context = Context(command)
    model = next(param for param in command.params if param.name == "model")
    assert [item.value for item in model.shell_complete(context, "ac")] == ["accurate"]


def test_nested_commands_and_unconfigured_siblings_keep_native_parsing():
    child, captured = _app()
    child.callback()(lambda: None)
    root = typer.Typer(add_completion=False)
    root.add_typer(child, name="child")

    @root.command()
    def ordinary(model: str | None = None) -> None:
        typer.echo(model)

    runner = CliRunner()
    result = runner.invoke(root, ["child", "probe", "-vm"])
    assert result.exit_code == 0, result.output
    assert captured["model"] == "auto" and captured["verbose"] is True
    assert runner.invoke(root, ["ordinary", "--model"]).exit_code == 2
    assert (
        runner.invoke(root, ["ordinary", "--model", "value"]).output.strip() == "value"
    )


@pytest.mark.parametrize(
    "parameter",
    [
        None,
        TyperArgument(param_decls=["value"]),
        TyperOption(param_decls=["--value"], is_flag=True),
        TyperOption(param_decls=["--value"], count=True),
        TyperOption(param_decls=["--value"], multiple=True),
        TyperOption(param_decls=["--value"], nargs=2),
    ],
)
def test_optional_value_configuration_requires_a_scalar_option(parameter):
    class InvalidCommand(OptionalValueCommand):
        optional_values = {"value": "bare"}

    with pytest.raises(TypeError, match="value: expected a scalar value option"):
        InvalidCommand(name="invalid", params=[] if parameter is None else [parameter])


def test_unconfigured_command_uses_native_parser():
    command = OptionalValueCommand(name="ordinary")
    native = TyperCommand(name="ordinary")
    assert type(command.make_parser(Context(command))) is type(
        native.make_parser(Context(native))
    )
