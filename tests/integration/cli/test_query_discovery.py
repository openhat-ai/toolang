from __future__ import annotations

import json

from click import unstyle
import pytest
from typer.testing import CliRunner

from toolang.cli.caps.main import app as caps_app
from toolang.cli.toolang.main import app as toolang_app


runner = CliRunner()


@pytest.mark.parametrize(
    ("app", "command", "collection", "identity"),
    [
        (toolang_app, ["models"], "models", "provider/model"),
        (toolang_app, ["providers"], "providers", "provider"),
        (toolang_app, ["adapters"], "adapters", "adapter"),
        (toolang_app, ["tools"], "tools", "toolset/tool"),
        (caps_app, ["list"], "caps", "kind/cap"),
        (caps_app, ["skill", "list"], "skills", "skill"),
    ],
)
def test_query_enabled_commands_publish_human_and_json_schema(
    app,
    command: list[str],
    collection: str,
    identity: str,
) -> None:
    human = runner.invoke(app, [*command, "--query-help"])
    machine = runner.invoke(app, [*command, "--query-schema"])

    assert human.exit_code == 0, human.stderr
    assert f"Collection: {collection}" in unstyle(human.stdout)
    assert f"Identity: {identity}" in unstyle(human.stdout)
    assert "Fields:" in unstyle(human.stdout)
    assert machine.exit_code == 0, machine.stderr
    payload = json.loads(machine.stdout)
    assert payload["collection"] == collection
    assert payload["fields"]


@pytest.mark.parametrize(
    ("app", "command", "legacy_option"),
    [
        (toolang_app, ["models"], "--filter"),
        (toolang_app, ["providers"], "--filter"),
        (toolang_app, ["adapters"], "--filter"),
        (toolang_app, ["tools"], "--filter"),
        (toolang_app, ["tools"], "--select"),
        (caps_app, ["list"], "--filter"),
        (caps_app, ["skill", "list"], "--filter"),
    ],
)
def test_migrated_commands_reject_legacy_query_options(
    app,
    command: list[str],
    legacy_option: str,
) -> None:
    result = runner.invoke(app, [*command, legacy_option, "*"])

    assert result.exit_code == 2
    assert f"No such option: {legacy_option}" in unstyle(result.stderr)
