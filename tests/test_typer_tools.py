from __future__ import annotations

from pathlib import Path
import json

import click
import typer

from toolang.base.types.tool import ToolContext
from toolang.base.utils.typer_tools import TyperToolConfig, create_typer_tools


def _build_test_app() -> typer.Typer:
    app = typer.Typer(add_completion=False)
    skill_app = typer.Typer(add_completion=False)
    app.add_typer(skill_app, name="skill")

    @app.callback()
    def root(
        ctx: typer.Context,
        agent: str | None = typer.Option(None, "--agent", help="Agent name."),
    ) -> None:
        ctx.obj = {"agent": agent}

    @app.command("ping", help="Ping the CLI.")
    def ping(
        ctx: typer.Context,
        loud: bool = typer.Option(False, "--loud", help="Use uppercase output."),
    ) -> None:
        text = f"ping:{ctx.obj.get('agent') or 'global'}"
        typer.echo(text.upper() if loud else text)

    @skill_app.command("add", help="Add a remote skill.")
    def add(
        ctx: typer.Context,
        locator: str = typer.Argument(..., help="Skill locator."),
    ) -> None:
        typer.echo(f"agent={ctx.obj.get('agent') or 'global'} locator={locator}")

    @skill_app.command("secret", hidden=True)
    def secret() -> None:
        typer.echo("secret")

    return app


def _tool_context(home: Path) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".state" / "tools" / "test",
        wd=home,
    )


def test_create_typer_tools_uses_leaf_commands_only() -> None:
    tools = create_typer_tools(_build_test_app(), prog_name="too")

    assert sorted(tools) == ["ping", "skill_add"]


def test_create_typer_tools_can_select_hidden_leaf_when_configured() -> None:
    tools = create_typer_tools(
        _build_test_app(),
        prog_name="too",
        include_paths={("skill", "secret")},
        configs={
            ("skill", "secret"): TyperToolConfig(),
        },
    )

    assert sorted(tools) == ["skill_secret"]


def test_typer_tool_definition_includes_parent_options() -> None:
    tools = create_typer_tools(_build_test_app(), prog_name="too")

    definition = tools["skill_add"].definition()

    assert definition.name == "skill_add"
    assert definition.description == "Add a remote skill."
    assert "agent" in definition.parameters["properties"]
    assert "locator" in definition.parameters["properties"]
    assert definition.parameters["required"] == ["locator"]


def test_typer_tool_invocation_runs_full_cli_path(tmp_path) -> None:
    tools = create_typer_tools(_build_test_app(), prog_name="too")

    result = tools["skill_add"].invoke(
        {"agent": "alice", "locator": "by3gus/pdf-processing"},
        _tool_context(tmp_path),
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["command"] == "too --agent alice skill add by3gus/pdf-processing"
    assert result["stdout"] == "agent=alice locator=by3gus/pdf-processing\n"
    assert result["stderr"] == ""


def test_typer_tool_invocation_returns_cli_error_payload(tmp_path) -> None:
    tools = create_typer_tools(_build_test_app(), prog_name="too")

    result = tools["skill_add"].invoke({}, _tool_context(tmp_path))

    assert result["ok"] is False
    assert result["exit_code"] == 2
    assert "Missing argument 'LOCATOR'" in result["stderr"]


def test_typer_tool_invocation_runs_inside_tool_context_working_directory(tmp_path: Path) -> None:
    app = typer.Typer(add_completion=False)

    @app.command("pwd", help="Print the current working directory.")
    def pwd() -> None:
        typer.echo(str(Path.cwd()))

    result = create_typer_tools(app, prog_name="too")["pwd"].invoke(
        {},
        _tool_context(tmp_path),
    )

    assert result["ok"] is True
    assert result["stdout"].strip() == str(tmp_path)


def test_typer_tool_definition_uses_custom_click_schema() -> None:
    class _JsonType(click.ParamType):
        name = "json"
        tool_schema = {"type": "object"}

        def convert(self, value, param, ctx):
            return value

    app = typer.Typer(add_completion=False)

    @app.command("push", help="Push a JSON payload.")
    def push(
        payload: str = typer.Option(..., "--payload", click_type=_JsonType(), help="JSON payload."),
    ) -> None:
        typer.echo(payload)

    definition = create_typer_tools(app, prog_name="too")["push"].definition()

    assert definition.parameters["properties"]["payload"]["type"] == "object"


def test_typer_tool_config_can_prepare_hidden_arguments_once(tmp_path: Path) -> None:
    app = typer.Typer(add_completion=False)
    prepared_values: list[str] = []

    @app.command("push", help="Push one payload.")
    def push(
        payload: str = typer.Option(..., "--payload", help="Payload JSON."),
        secret: str = typer.Option(..., "--secret", help="Hidden secret."),
    ) -> None:
        typer.echo(json.dumps({"payload": json.loads(payload), "secret": secret}))

    tools = create_typer_tools(
        app,
        prog_name="too",
        configs={
            ("push",): TyperToolConfig(
                hidden_params=frozenset({"secret"}),
                param_aliases={"payload": "input"},
                param_schemas={"input": {"type": "object"}},
                prepare=lambda path, arguments, context: prepared_values.append(context.room.name) or "runtime-secret",
                inject_arguments=lambda path, arguments, context, prepared: {"secret": prepared},
                transform_result=lambda path, payload, arguments, context, prepared: {
                    "prepared": prepared,
                    "json": json.loads(payload["stdout"]),
                },
            )
        },
    )

    definition = tools["push"].definition()
    result = tools["push"].invoke({"input": {"hello": "world"}}, _tool_context(tmp_path))

    assert "secret" not in definition.parameters["properties"]
    assert definition.parameters["properties"]["input"]["type"] == "object"
    assert prepared_values == ["test"]
    assert result == {
        "prepared": "runtime-secret",
        "json": {
            "payload": {"hello": "world"},
            "secret": "runtime-secret",
        },
    }
