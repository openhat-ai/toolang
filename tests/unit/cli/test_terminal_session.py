"""Shared presentation contract and CLI configuration source boundaries."""

from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from wcwidth import wcswidth

from toolang.cli.common import terminal_session as sessions
from toolang.cli.common.terminal_session import TerminalSession, prepare_terminal
from toolang.cli.common.terminal_title import TerminalTitle
from toolang.cli.config import TITLE_KINDS, resolve_title_prefix
from toolang.common.config_sources import ConfigSource, project_sources
from toolang.common.layout import AgentLayout


def source(path: str, **prefixes) -> ConfigSource:
    return ConfigSource(Path(path), {"terminal": {"title": {"prefix": prefixes}}})


@pytest.mark.parametrize("kind", sorted(TITLE_KINDS))
def test_prefix_precedence_preserves_empty_and_spaces(kind):
    sources = [
        source("root", **{kind: "root ", "chat": "root "}),
        source("home", **{kind: " home  "}),
    ]
    key = f"TOOLANG_TITLE_PREFIX_{kind.upper()}"
    assert resolve_title_prefix(kind, sources=sources, environment={}) == " home  "
    assert resolve_title_prefix(kind, sources=sources, environment={key: ""}) == ""
    assert resolve_title_prefix(kind, sources=sources, environment={key: " "}) == " "
    assert (
        resolve_title_prefix(kind, sources=sources, environment={key: "env "}) == "env "
    )
    assert resolve_title_prefix(kind, sources=[], environment={}) == ""


def test_partial_prefix_tables_merge_by_type():
    sources = [source("root", chat="chat", top="top"), source("home", chat="")]
    assert resolve_title_prefix("top", sources=sources, environment={}) == "top"
    assert resolve_title_prefix("chat", sources=sources, environment={}) == ""
    assert resolve_title_prefix("talk", sources=sources, environment={}) == ""


@pytest.mark.parametrize(
    "config,key",
    [
        ({"terminal": "x"}, "terminal"),
        ({"terminal": {"title": []}}, "terminal.title"),
        ({"terminal": {"title": {"prefix": "x"}}}, "terminal.title.prefix"),
        (
            {"terminal": {"title": {"prefix": {"chat": 1}}}},
            "terminal.title.prefix.chat",
        ),
        (
            {"terminal": {"title": {"prefix": {"all": "x"}}}},
            "terminal.title.prefix.all",
        ),
    ],
)
def test_invalid_prefix_reports_source_and_key_even_with_env_override(config, key):
    with pytest.raises(ValueError) as error:
        resolve_title_prefix(
            "chat",
            sources=[ConfigSource(Path("broken.toml"), config)],
            environment={"TOOLANG_TITLE_PREFIX_CHAT": "override"},
        )
    assert "broken.toml" in str(error.value) and key in str(error.value)


@pytest.mark.parametrize("agent", [False, True])
def test_config_read_errors_are_cli_errors_with_the_source_path(tmp_path, agent):
    from typer._click.exceptions import ClickException

    layout = AgentLayout.resident(tmp_path, "alice")
    path = layout.config if agent else layout.place_config
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[terminal.title.prefix\n")
    with pytest.raises(ClickException) as error:
        prepare_terminal(
            "chat", layout=layout if agent else None, root=tmp_path, environment={}
        )
    assert str(path) in str(error.value)


@pytest.mark.parametrize(
    "prefix,label,expected",
    [
        ("💬", "alice", "💬alice"),
        (" 💬  ", "alice", " 💬  alice"),
        ("   ", "alice", "   alice"),
        ("\x1b[31m💬\x1b[0m ", " alice\n  topic ", "💬 alice topic"),
        ("\x1b]52;c;secret\x07\x00\x9c\ud800\u2028", "alice", "alice"),
        ("", "x" * 40, "x" * 40),
        ("", "x" * 128, "x" * 128),
        ("", "开发" * 32, "开发" * 32),
        ("💬 ", "开发" * 32, "💬 " + "开发" * 31 + "…"),
        ("x" * 136, "alice", "x" * 127 + "…"),
        (" " * 136, "alice", " " * 127 + "…"),
        ("", "x" * 126 + "👩🏽‍💻", "x" * 126 + "👩🏽‍💻"),
        ("", "x" * 126 + "👩🏽‍💻z", "x" * 126 + "…"),
        ("", "e\u0301" * 129, "e\u0301" * 127 + "…"),
    ],
)
def test_title_safe_grapheme_clipping_and_exact_prefix(prefix, label, expected):
    output = StringIO()
    title = TerminalTitle(
        prefix=prefix, write=output.write, flush=output.flush, enabled=True
    )
    assert title.set(label)
    assert title.set(label)
    title.close()
    title.close()
    assert not title.set("late")
    assert output.getvalue() == f"\x1b]0;{expected}\x07\x1b]0;\x07"
    assert wcswidth(expected) <= 128


def test_failed_initial_title_can_succeed_later_and_still_clear():
    output = StringIO()
    fail = True

    def write(text):
        if fail:
            raise OSError("closed")
        return output.write(text)

    title = TerminalTitle(prefix="", write=write, flush=output.flush, enabled=True)
    assert not title.set("alice")
    fail = False
    assert title.set("alice")
    title.close()
    assert output.getvalue() == "\x1b]0;alice\x07\x1b]0;\x07"


def test_identity_updates_never_republish_osc_and_errors_clear():
    output = StringIO()
    with pytest.raises(RuntimeError):
        with TerminalSession(
            label="rerun source", enabled=True, stream=output
        ) as terminal:
            terminal.set_run("new")
            terminal.set_thread("thread")
            assert output.getvalue() == "\x1b]0;rerun source\x07"
            raise RuntimeError("failed")
    assert output.getvalue().endswith("\x1b]0;\x07")


class Tty(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal_io(monkeypatch):
    def create():
        output = Tty()
        monkeypatch.setattr(sessions.sys, "stdin", Tty())
        monkeypatch.setattr(sessions.sys, "stdout", output)
        return output

    return create


@pytest.mark.parametrize("placement", ["resident", "visiting"])
def test_resident_and_root_only_sources(tmp_path, terminal_io, placement):
    terminal_io = terminal_io()
    layout = AgentLayout(tmp_path, "alice", placement)
    layout.home.mkdir(parents=True)
    layout.place_config.write_text(
        '[terminal.title.prefix]\nchat="root "\ntop="root "\n'
    )
    layout.config.write_text('[terminal.title.prefix]\nchat="home "\n')
    layout.place_env.write_text('TOOLANG_TITLE_PREFIX_CHAT="root-env "\n')
    layout.env.write_text('TOOLANG_TITLE_PREFIX_CHAT="home-env "\n')
    for kwargs, expected in [
        (
            {"layout": layout},
            ("home-env alice" if placement == "resident" else "home alice"),
        ),
        (
            {
                "layout": layout,
                "environment": {"TOOLANG_TITLE_PREFIX_CHAT": "process "},
            },
            "home-env alice" if placement == "resident" else "process alice",
        ),
        (
            {"layout": layout, "environment": {"TOOLANG_TITLE_PREFIX_CHAT": ""}},
            "home-env alice" if placement == "resident" else "alice",
        ),
        ({"root": tmp_path}, "root-env alice"),
    ]:
        terminal_io.seek(0)
        terminal_io.truncate()
        with prepare_terminal(
            "chat", label="alice", **cast(dict[str, Any], {"environment": {}, **kwargs})
        ):
            pass
        assert terminal_io.getvalue() == f"\x1b]0;{expected}\x07\x1b]0;\x07"


def test_visiting_pane_marks_publish_the_selected_root(
    tmp_path, terminal_io, monkeypatch
):
    terminal_io()
    root = tmp_path / "selected"
    layout = AgentLayout.visiting("gh:example/alice", "alice", root=root)
    published = {}
    marks_type = sessions.PaneMarks

    def marks(*, environment, fields):
        published.update(fields)
        return marks_type(environment={}, fields=fields)

    monkeypatch.setattr(sessions, "PaneMarks", marks)
    with prepare_terminal("chat", layout=layout, environment={}):
        pass
    assert layout.place != root
    assert published["root"] == str(root)


@pytest.mark.parametrize("prefix", [None, "env ", ""])
def test_roaming_uses_captured_project_layers_and_no_dotenv(
    tmp_path, terminal_io, prefix
):
    from dataclasses import replace

    terminal_io = terminal_io()
    # A concrete projected roaming layout avoids creating any execution state.
    layout = replace(
        AgentLayout.resident(tmp_path, "script"),
        placement="roaming",
        project_inputs=True,
        managed_membership=False,
    )
    layout.home.mkdir(parents=True)
    layout.config.write_text(
        project_sources(
            [
                source("outer/toolang.toml", run="outer"),
                source("inner/toolang.toml", run="inner "),
            ],
            source=tmp_path / "file.too",
        )
    )
    layout.place_config.write_text('[terminal.title.prefix]\nrun="wrong"\n')
    layout.env.write_text("TOOLANG_TITLE_PREFIX_RUN=wrong\n")
    layout.place_env.write_text("TOOLANG_TITLE_PREFIX_RUN=wrong\n")
    environment = {} if prefix is None else {"TOOLANG_TITLE_PREFIX_RUN": prefix}
    with prepare_terminal(
        "run", layout=layout, environment=environment, label="run file.too"
    ):
        pass
    expected = "inner " if prefix is None else prefix
    assert terminal_io.getvalue() == f"\x1b]0;{expected}run file.too\x07\x1b]0;\x07"


@pytest.mark.parametrize("excluded", ["input", "output", "presentation", "eligible"])
def test_publication_exclusions_include_cleanup(
    tmp_path, terminal_io, monkeypatch, excluded
):
    terminal_io()
    output = Tty()
    if excluded == "input":
        monkeypatch.setattr(sessions.sys, "stdin", StringIO())
    if excluded == "output":
        monkeypatch.setattr(sessions.sys, "stdout", StringIO())
    if excluded == "presentation":
        output = StringIO()
    with prepare_terminal(
        "top",
        root=tmp_path,
        label="top",
        stream=output,
        eligible=excluded != "eligible",
        environment={},
    ):
        pass
    assert output.getvalue() == ""


def test_tmux_opt_out_keeps_osc_enabled(tmp_path, terminal_io, monkeypatch):
    from libtmux import Pane

    output = terminal_io()

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled marks must not resolve tmux")

    monkeypatch.setattr(Pane, "from_env", forbidden)
    with prepare_terminal(
        "talk",
        root=tmp_path,
        label="@alice",
        convo="dm_full",
        environment={"TOOLANG_TMUX": "0", "TMUX": "unreachable", "TMUX_PANE": "%1"},
    ):
        pass
    assert output.getvalue() == "\x1b]0;@alice\x07\x1b]0;\x07"
