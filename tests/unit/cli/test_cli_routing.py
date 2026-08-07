from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import click
import pytest
import typer

import toolang.cli.caps.main as caps_cli
import toolang.cli.toolang.main as cli
from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout
from toolang.cli.toolang.commands import script
from toolang.cli.toolang.commands.chat import main as chat_commands
from toolang.cli.toolang.routing import (
    COMMAND_SPECS,
    RoutingError,
    dispatch_roaming,
    dispatch_visiting,
    normalize,
)
from toolang.cli.common.routing import extract_root_args
from toolang.up import process as agents


def test_extract_root_args_supports_short_option_and_stops_at_separator() -> None:
    root_args, body = extract_root_args(
        ("-r", "/tmp/root", "alice", "chat", "--", "--root", "message")
    )

    assert root_args == ["-r", "/tmp/root"]
    assert body == ["alice", "chat", "--", "--root", "message"]


def test_cli_command_registry_matches_the_typer_surface() -> None:
    group = typer.main.get_command(cli.app)

    assert isinstance(group, click.Group)
    assert set(group.commands) == set(COMMAND_SPECS)


@pytest.mark.parametrize(
    ("command", "targets", "placements"),
    (
        ("new", {"none"}, set()),
        ("remove", {"after"}, {"resident"}),
        ("info", {"before", "after"}, {"resident", "roaming", "visiting"}),
        ("retry", {"before"}, {"resident", "roaming", "visiting"}),
        ("task", {"before"}, {"resident"}),
        ("skill", {"none", "before"}, {"resident"}),
    ),
)
def test_cli_command_registry_declares_target_grammar(
    command: str,
    targets: set[str],
    placements: set[str],
) -> None:
    spec = COMMAND_SPECS[command]

    assert spec.targets == targets
    assert spec.placements == placements


def test_cli_normalize_routes_resident_target_before_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    args, agent = normalize(
        ["-r", str(root), "alice", "retry", "run_1"],
        root=root,
    )

    assert args == ["-r", str(root), "retry", "run_1"]
    assert agent == "alice"


def test_cli_normalize_allows_both_orders_for_agent_self_commands(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")

    prefix_args, prefix_agent = normalize(["alice", "info"], root=root)
    postfix_args, postfix_agent = normalize(["info", "alice"], root=root)

    assert (prefix_args, prefix_agent) == (["info", "alice"], None)
    assert (postfix_args, postfix_agent) == (["info", "alice"], None)


def test_cli_normalize_requires_declared_target_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")

    with pytest.raises(RoutingError, match="retry requires TARGET before"):
        normalize(["retry", "alice", "run_1"], root=root)
    with pytest.raises(RoutingError, match="remove requires TARGET after"):
        normalize(["alice", "remove"], root=root)


def test_cli_explicit_agent_prefix_resolves_command_name_collision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("retry", content="")

    args, agent = normalize(["agent:retry", "info"], root=root)

    assert args == ["info", "retry"]
    assert agent is None


def test_cli_command_name_wins_without_explicit_agent_prefix(tmp_path: Path) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("retry", content="")

    with pytest.raises(RoutingError, match="retry requires TARGET before"):
        normalize(["retry", "info"], root=root)


def test_caps_cli_uses_command_priority_and_explicit_agent_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("skill", content="")

    global_args, global_agent = caps_cli._rewrite_agent_shortcuts(
        ["skill", "list"],
        root=root,
    )
    agent_args, agent = caps_cli._rewrite_agent_shortcuts(
        ["agent:skill", "skill", "list"],
        root=root,
    )

    assert (global_args, global_agent) == (["skill", "list"], None)
    assert (agent_args, agent) == (["skill", "list"], "skill")


def test_cli_prefix_agent_context_is_isolated_between_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    seen: list[str | None] = []

    def fake_app(**_kwargs: Any) -> None:
        barrier.wait()
        seen.append(cli._PREFIX_AGENT.get())

    monkeypatch.setattr(cli, "app", cast(Any, fake_app))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.submit(
                cli._run_app,
                [],
                agent,
                prog_name="toolang",
            )
            for agent in ("alice", "bob")
        )

    assert [result.result() for result in results] == [0, 0]
    assert set(seen) == {"alice", "bob"}
    assert cli._PREFIX_AGENT.get() is None


def test_cli_routes_local_script_to_script_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(
        global_args: list[str],
        argv: list[str],
        *,
        prog_name: str,
    ) -> int:
        captured.update(
            global_args=global_args,
            argv=argv,
            prog_name=prog_name,
        )
        return 7

    monkeypatch.setattr(script, "dispatch", fake_dispatch)

    result = dispatch_roaming(
        [str(source), "demo", "--help"],
        prog_name="too",
        run_app=lambda *_args: pytest.fail("Typer app should not run"),
    )

    assert result == 7
    assert captured == {
        "global_args": [],
        "argv": [str(source), "demo", "--help"],
        "prog_name": "too",
    }


@pytest.mark.parametrize(
    "arguments",
    (
        ["info"],
        ["chat"],
        ["chat", "term_1"],
        ["threads"],
        ["runs", "--thread", "script_1"],
        ["inspect", "run_1"],
        ["steer", "run_1", "change direction"],
        ["retry", "run_1"],
        ["rerun", "run_1"],
    ),
)
def test_cli_routes_roaming_agent_command_to_its_exact_layout(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_app(args: list[str], layout: AgentLayout) -> int:
        captured.update(args=args, layout=layout)
        return 9

    result = dispatch_roaming(
        [str(source), *arguments],
        prog_name="too",
        run_app=fake_run_app,
    )

    assert result == 9
    assert captured == {
        "args": ["info", source.stem] if arguments == ["info"] else arguments,
        "layout": AgentLayout.roaming(source),
    }


def test_cli_opens_roaming_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic chat:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, layout: AgentLayout, **_kwargs: object) -> None:
            captured["layout"] = layout

        def close(self) -> None:
            captured["closed"] = True

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(chat_commands, "LocalChatSession", Session)
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(chat_commands.sys.stdin, "isatty", lambda: False)

    assert cli.main([str(source), "chat"]) == 0
    assert captured == {
        "layout": AgentLayout.roaming(source),
        "closed": True,
    }


def test_cli_routes_visiting_chat_through_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    def resolve(source: str, *, progress: object) -> AgentLayout:
        captured.update(source=source, progress=progress)
        return layout

    def run_app(args: list[str], selected: AgentLayout) -> int:
        captured.update(args=args, layout=selected)
        return 12

    monkeypatch.setattr(agents, "resolve_visiting_layout", resolve)

    result = dispatch_visiting(
        [selector, "chat", "term_1"],
        run_app=run_app,
    )

    assert result == 12
    assert captured["source"] == selector
    assert captured["args"] == ["chat", "term_1"]
    assert captured["layout"] == layout


def test_cli_routes_visiting_inspect_without_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(agents, "visiting_layout", lambda source: layout)
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: pytest.fail("inspect must not materialize"),
    )

    result = dispatch_visiting(
        [selector, "inspect", "run_1"],
        run_app=lambda args, selected: captured.update(
            args=args,
            layout=selected,
        )
        or 13,
    )

    assert result == 13
    assert captured == {
        "args": ["inspect", "run_1"],
        "layout": layout,
    }


def test_cli_routes_command_before_visiting_info_through_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda source, *, progress: captured.update(
            source=source,
            progress=progress,
        )
        or layout,
    )

    result = dispatch_visiting(
        ["info", selector],
        run_app=lambda args, selected: captured.update(
            args=args,
            layout=selected,
        )
        or 14,
    )

    assert result == 14
    assert captured["source"] == selector
    assert captured["args"] == ["info", "researcher"]
    assert captured["layout"] == layout


def test_cli_opens_visiting_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, selected: AgentLayout, **_kwargs: object) -> None:
            captured["layout"] = selected

        def close(self) -> None:
            captured["closed"] = True

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(agents, "resolve_visiting_layout", lambda *_args, **_kwargs: layout)
    monkeypatch.setattr(chat_commands, "LocalChatSession", Session)
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(chat_commands.sys.stdin, "isatty", lambda: False)

    assert cli.main([selector, "chat"]) == 0
    assert captured == {
        "layout": layout,
        "closed": True,
    }


def test_cli_typed_runnable_prefix_escapes_a_roaming_command_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(
        global_args: list[str],
        argv: list[str],
        *,
        prog_name: str,
    ) -> int:
        captured.update(
            global_args=global_args,
            argv=argv,
            prog_name=prog_name,
        )
        return 11

    monkeypatch.setattr(script, "dispatch", fake_dispatch)

    result = dispatch_roaming(
        [str(source), "runnable:steer", "change direction"],
        prog_name="too",
        run_app=lambda *_args: pytest.fail("typed runnable should not be a command"),
    )

    assert result == 11
    assert captured["argv"] == [
        str(source),
        "runnable:steer",
        "change direction",
    ]
