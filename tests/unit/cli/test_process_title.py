from pathlib import Path

import pytest
import typer
from typer._click import Context
from typer.core import TyperGroup

from toolang.cli.common import process_title
from toolang.cli.common.context import CliContext
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.routing import normalize


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["alice", "chat", "--thread", "term_x"], "too:alice chat --thread term_x"),
        (["alice", "start"], "too:alice start"),
        (["start", "alice"], "too:alice start"),
        (["start", "--host", "alice", "alice"], "too:alice start --host alice"),
        (
            ["start", "--dev", "--host", "127.0.0.1", "alice"],
            "too:alice start --dev --host 127.0.0.1",
        ),
        (["new", "psyche"], "too new psyche"),
        (
            ["run", "task.too", "main", "--agent", "alice"],
            "too run task.too main --agent alice",
        ),
        (["list"], "too list"),
    ],
)
def test_title_uses_parsed_scope_and_preserves_argument_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: str
) -> None:
    titles: list[str] = []
    monkeypatch.setattr(process_title, "_set", titles.append)
    args, agent = normalize(argv)
    root = typer.main.get_command(cli.app)
    assert isinstance(root, TyperGroup)
    ctx = Context(root, info_name="too", obj=CliContext(root=tmp_path, agent=agent))
    with process_title.invocation():
        root.commands[args[0]].make_context(args[0], args[1:], parent=ctx)
        assert titles == [expected]
    assert titles[-1] != expected


def test_server_title_waits_for_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    titles: list[str] = []
    monkeypatch.setattr(process_title, "_set", titles.append)
    root = typer.main.get_command(cli.app)
    assert isinstance(root, TyperGroup)
    ctx = Context(root, info_name="too", obj=CliContext(root=tmp_path))
    with process_title.invocation():
        root.commands["_serve"].make_context("_serve", ["alice"], parent=ctx)
        assert not titles
        process_title.apply()
        assert titles == ["too:alice _serve"]


def test_display_failure_does_not_fail_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_title: str) -> None:
        raise OSError("unsupported")

    monkeypatch.setattr(process_title.setproctitle, "setproctitle", fail)
    with process_title.invocation():
        process_title.select("alice", ["chat"])


def test_title_escapes_agent_whitespace_and_restores_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    titles: list[str] = []
    monkeypatch.setattr(process_title, "_set", titles.append)
    with process_title.invocation():
        process_title.select("alice bob", ["chat", "two words"])
        assert titles[-1] == "too:alice%20bob chat 'two words'"
    titles.clear()
    process_title.apply()
    assert not titles
