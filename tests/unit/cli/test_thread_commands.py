from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
import typer

from toolang.base.types.model import ModelOverride
from toolang.cli.toolang.commands import thread
from toolang.execution.types import ErrorMessage, Local, Output
from toolang.lang.types import Struct
from toolang.common.layout import AgentLayout


@pytest.mark.parametrize("succeeded", [False, True])
def test_compact_cli_arguments_output_and_exit(tmp_path, monkeypatch, succeeded):
    app = typer.Typer()
    app.command()(thread.compact_command)
    layout = AgentLayout.resident(tmp_path, "alice")
    seen = []

    @contextmanager
    def server(*args, **kwargs):
        assert args == (layout,) and kwargs["sandbox"] is None
        yield None

    value = {"thread": "term_test", "begin": None, "end": "run_end", "summary": "Facts"}

    async def execute(selected, server, request, catalog):
        assert selected == layout and server is None
        seen.append(request)
        return SimpleNamespace(
            id="run_compact",
            status="succeeded" if succeeded else "failed",
            error=ErrorMessage("bad range") if not succeeded else None,
            output=Output(Local(Struct("Compacted", value))),
        )

    monkeypatch.setattr(thread, "context_layout", lambda _: layout)
    monkeypatch.setattr(thread, "acquire_agent_server", server)
    monkeypatch.setattr(thread, "_execute_compact", execute)
    result = CliRunner().invoke(
        app,
        [
            "term_test",
            "--end",
            "run_end",
            "--model",
            "test/model effort=high",
            "--limit",
            "time=60",
        ],
    )
    assert result.exit_code == (0 if succeeded else 1), result.output
    assert str(seen[0].end) == "run_end"
    assert seen[0].model == ModelOverride("test/model", effort="high")
    assert seen[0].commands[0].field == "time" and seen[0].commands[0].value == 60
    if succeeded:
        assert json.loads(result.stdout) == {
            "run": "run_compact",
            "horizon": "run_compact/output",
            "output": value,
        }
    else:
        assert not result.stdout and "run_compact failed: bad range" in result.stderr


def test_rerun_model_option_uses_the_shared_model_body() -> None:
    assert thread._rerun_model_override(
        model_body="test/model effort=4096",
    ) == ModelOverride(identity="test/model", effort=4096)
    assert thread._rerun_model_override(model_body="effort=high") == ModelOverride(
        effort="high"
    )
    assert thread._rerun_model_override(model_body=None) is None


def test_rerun_model_option_rejects_model_removal() -> None:
    with pytest.raises(ValueError, match="does not accept unset"):
        thread._rerun_model_override(model_body="unset")
    with pytest.raises(ValueError, match="was removed"):
        thread._rerun_model_override(model_body="none")
