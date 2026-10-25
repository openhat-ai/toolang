from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

import pytest
from rich.cells import cell_len
import typer
from typer._click.utils import strip_ansi
from typer._click.exceptions import ClickException

from toolang.catalog.templates import load_template
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import script
from toolang.cli.toolang.commands.init import init_script
from toolang.lang.ast import Program


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["too"])
    monkeypatch.setattr("sys.stdin", StringIO())
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "root"))


def _source(text: str = "agic():\n  Hello.\n") -> str:
    Path("demo.too").write_text(text, encoding="utf-8")
    return "demo.too"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "source",
    ["missing", "invalid", "directory", "root_option", "expanduser", "resolve"],
)
def test_completion_suppresses_script_header_errors(
    explicit, source, tmp_path, monkeypatch, capsys
):
    from typer._click.shell_completion import _resolve_context

    label = "demo.too"
    if source == "invalid":
        Path(label).write_text("flow broken(:\n")
    elif source == "directory":
        Path(label).mkdir()
    elif source == "root_option":
        _source()
    elif source in {"expanduser", "resolve"}:
        original = getattr(Path, source)

        def fail_script_path(self, *args, **kwargs):
            if self == Path(label):
                error = RuntimeError if source == "expanduser" else OSError
                raise error("cannot resolve script path")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, source, fail_script_path)
    command = typer.main.get_command(cli.app)
    before = set(tmp_path.rglob("*"))
    ctx = _resolve_context(
        command,
        {},
        "too",
        [
            *(["--root", str(tmp_path)] if source == "root_option" else []),
            *(["run"] if explicit else []),
            label,
        ],
    )
    assert ctx.command.name == "run"
    assert ctx.resilient_parsing
    assert set(tmp_path.rglob("*")) == before
    output = capsys.readouterr()
    assert not output.out and not output.err
    if source in {"expanduser", "resolve"}:
        assert cli.main([*(["run"] if explicit else []), label]) == 1
        assert "cannot resolve script path" in capsys.readouterr().err
        assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("explicit", [False, True])
def test_completion_keeps_valid_script_signatures(explicit, monkeypatch, tmp_path):
    from typer._click.shell_completion import _resolve_context

    label = _source("flow research(topic: Text):\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("completion ran script")
    )
    before = set(tmp_path.rglob("*"))
    ctx = _resolve_context(
        typer.main.get_command(cli.app),
        {},
        "too",
        [*(["run"] if explicit else []), label, "research"],
    )
    assert ctx.command.name == "research"
    assert any(param.name == "topic" and param.required for param in ctx.command.params)
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("executable", ["too", "toolang"])
@pytest.mark.parametrize("arguments", [[], ["--help"], ["-h"]])
def test_init_without_directory_only_shows_help(
    executable, arguments, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", [executable])
    before = set(tmp_path.rglob("*"))
    assert cli.main(["init", *arguments]) == 0
    assert set(tmp_path.rglob("*")) == before
    output = " ".join(capsys.readouterr().out.split())
    assert f"Usage: {executable} init [OPTIONS] <DIR>" in output
    assert "* DIR" in output
    assert "[default: .]" not in output
    assert "Directory to set up for Toolang" in output
    assert output.endswith("Show this message and exit")


@pytest.mark.parametrize("option", ["-d", "--workdir"])
@pytest.mark.parametrize("before_runnable", [False, True])
def test_workdir_option_aliases_reach_script_invocation(
    option, before_runnable, monkeypatch
):
    path = _source("agic explain():\n  Explain.\n")
    captured = {}
    monkeypatch.setattr(
        script, "_run", lambda *_args, **kwargs: captured.update(kwargs) or 0
    )
    args = [option, "repo=."]
    args = [*args, "explain"] if before_runnable else ["explain", *args]

    assert cli.main([path, *args]) == 0
    assert captured["workdir"] == "repo=."


def test_mixed_workdir_aliases_are_rejected(monkeypatch, capsys):
    path = _source("agic explain():\n  Explain.\n")
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("accepted two workdirs")
    )

    assert cli.main([path, "-d", ".", "explain", "--workdir", "."]) == 2
    assert "--workdir may only be specified once" in capsys.readouterr().err


@pytest.mark.parametrize("explicit_run", [False, True])
@pytest.mark.parametrize("before_runnable", [False, True])
def test_script_rejects_removed_no_auto_workspace(
    explicit_run, before_runnable, monkeypatch, capsys
):
    path = _source("agic explain():\n  Explain.\n")
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("must reject removed option"),
    )
    args = (
        ["--no-auto-workspace", "explain"]
        if before_runnable
        else ["explain", "--no-auto-workspace"]
    )
    assert cli.main([*(["run"] if explicit_run else []), path, *args]) == 2
    assert "No such option: --no-auto-workspace" in strip_ansi(capsys.readouterr().err)


@pytest.mark.parametrize("explicit_run", [False, True])
def test_script_merges_workspace_options_around_runnable(explicit_run, monkeypatch):
    path = _source("agic explain():\n  Explain.\n")
    captured = {}
    monkeypatch.setattr(
        script, "_run", lambda *_args, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        cli.main(
            [
                *(["run"] if explicit_run else []),
                path,
                "-w",
                "one=.",
                "explain",
                "--workspace",
                "two=.",
                "-d",
                "one://",
            ]
        )
        == 0
    )
    assert captured["workspace_options"] == ("one=.", "two=.")
    assert captured["workdir"] == "one://"


@pytest.mark.parametrize(
    "directory", [".", "existing", "new/nested", "hello world/你好"]
)
@pytest.mark.parametrize("executable", ["too", "toolang"])
def test_init_creates_script_and_portable_config(
    directory, executable, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", [executable])
    (tmp_path / "existing").mkdir()
    neighbor = tmp_path / "existing" / "keep.txt"
    neighbor.write_text("keep")
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert cli.main(["init", directory]) == 0
    destination = (tmp_path / directory / "aid.too").resolve()
    assert destination.read_text() == load_template("script").raw_text
    assert destination.read_text().startswith("#!/usr/bin/env too\n")
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == before | {
        destination,
        destination.with_name("toolang.toml"),
    }
    assert neighbor.read_text() == "keep"
    assert destination.stat().st_mode & 0o111 == 0o111
    program = Program.from_source(destination.read_text())
    assert [agic.name for agic in program.agics] == [
        "issue",
        "fix",
        "review",
        "whats_for",
        "whats_new",
        "update_i18n",
    ]
    assert program.flows == ()
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert lines == [
        "Created aid.too and toolang.toml.",
        "Add .toolang/ to .gitignore.",
    ]


@pytest.mark.parametrize(
    "entry", [None, "issue", "fix", "review", "whats_for", "whats_new", "update_i18n"]
)
def test_initialized_script_help_exposes_project_helpers(entry, monkeypatch, capsys):
    assert cli.main(["init", "."]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    assert cli.main(["run", "aid.too", *([entry] if entry else []), "--help"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    if entry is None:
        for name in ("issue", "fix", "review", "whats_for", "whats_new", "update_i18n"):
            assert f"{name} agic" in " ".join(output.split())
        assert "agic:<entry>" not in output
    elif entry in {"issue", "fix", "review"}:
        assert "INPUT" in output
    elif entry == "whats_new":
        assert "since=<SINCE>" in output
        assert "* since=" not in output
    elif entry == "update_i18n":
        assert "locale=<LOCALE>" in output
        assert "[locale=" not in output


@pytest.mark.parametrize("explicit", [False, True])
def test_initialized_script_without_runnable_only_shows_help(
    explicit, monkeypatch, capsys
):
    assert cli.main(["init", "."]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        script,
        "_run",
        lambda *args, **kwargs: pytest.fail("selected a default runnable"),
    )

    assert cli.main([*(["run"] if explicit else []), "aid.too"]) == 0
    assert "whats_for agic" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("entry", ["issue", "fix", "review", "update_i18n"])
def test_initialized_script_requires_its_input(entry, monkeypatch):
    assert cli.main(["init", "."]) == 0
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("executed without input")
    )

    assert cli.main(["aid.too", entry]) == 2


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
@pytest.mark.parametrize("filename", ["aid.too", "toolang.toml"])
def test_init_never_overwrites_existing_output(kind, filename, tmp_path, capsys):
    output = tmp_path / filename
    target = tmp_path / "target"
    if kind == "file":
        output.write_text("keep")
    elif kind == "directory":
        output.mkdir()
    else:
        if kind == "symlink":
            target.write_text("keep")
        output.symlink_to(target)
    assert cli.main(["init", "."]) == 1
    message = capsys.readouterr()
    assert message.err == f"Error: aborted to avoid overwriting: {filename}\n"
    assert message.out == ""
    other = "toolang.toml" if filename == "aid.too" else "aid.too"
    assert not (tmp_path / other).exists()
    if kind == "file":
        assert output.read_text() == "keep"
    elif kind == "symlink":
        assert target.read_text() == "keep"
    elif kind == "dangling":
        assert output.is_symlink() and not target.exists()


def test_init_reports_all_existing_outputs(tmp_path, capsys):
    for name in ("aid.too", "toolang.toml"):
        (tmp_path / name).write_text("keep")

    assert cli.main(["init", "."]) == 1
    message = capsys.readouterr()
    assert message.err == (
        "Error: aborted to avoid overwriting: aid.too, toolang.toml\n"
    )
    assert message.out == ""
    for name in ("aid.too", "toolang.toml"):
        assert (tmp_path / name).read_text() == "keep"


def test_init_rejects_a_file_as_directory(tmp_path, capsys):
    target = tmp_path / "file"
    target.write_text("keep")
    assert cli.main(["init", str(target)]) == 1
    assert target.read_text() == "keep"
    assert capsys.readouterr().err == "Error: file already exists\n"


def test_init_reports_permission_errors(tmp_path, monkeypatch, capsys):
    original = Path.open

    def denied(self, *args, **kwargs):
        if self == tmp_path / "aid.too":
            raise PermissionError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert cli.main(["init", "."]) == 1
    assert capsys.readouterr().err == "Error: permission denied\n"
    assert (tmp_path / "toolang.toml").is_file()
    assert not (tmp_path / "aid.too").exists()


@pytest.mark.parametrize("filename", ["aid.too", "toolang.toml"])
def test_init_reports_a_postcheck_collision(filename, tmp_path, monkeypatch, capsys):
    original = Path.open
    output = tmp_path / filename

    def create_before_exclusive_open(self, mode="r", *args, **kwargs):
        if self == output and mode == "x":
            with original(self, "w", encoding="utf-8") as stream:
                stream.write("keep")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_before_exclusive_open)
    assert cli.main(["init", "."]) == 1
    message = capsys.readouterr()
    assert message.err == f"Error: aborted to avoid overwriting: {filename}\n"
    assert message.out == ""
    assert output.read_text() == "keep"
    assert (tmp_path / "toolang.toml").is_file()
    assert (tmp_path / "aid.too").exists() == (filename == "aid.too")


def test_concurrent_init_has_one_winner(tmp_path):
    def create():
        try:
            init_script(tmp_path)
            return True
        except ClickException:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(results) == [False, True]
    assert (tmp_path / "aid.too").read_text() == load_template("script").raw_text


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("declaration", ["agic()", "flow()"])
def test_script_defaults_to_unnamed_entry(explicit, declaration, monkeypatch):
    path = _source(f"{declaration}:\n  pass\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main([*(["run"] if explicit else []), path]) == 0
    assert len(captured) == 1
    assert captured[0]["runnable"].startswith("<entry:")
    assert captured[0]["runnable_kind"] == declaration.split("(")[0].split()[0]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_explicit_unnamed_entry_selection(explicit, kind, monkeypatch):
    path = _source(f"{kind}():\n  pass\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main([*(["run"] if explicit else []), path, f"{kind}:"]) == 0
    assert captured[0]["runnable"].startswith("<entry:")
    assert captured[0]["runnable_kind"] == kind


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_typed_selector_selects_the_same_entry_as_omission(kind, monkeypatch):
    path = _source(f"{kind}():\n  pass\n\nagic main():\n  Named.\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main([path]) == 0
    assert cli.main([path, f"{kind}:"]) == 0
    assert [call["runnable"] for call in captured] == ["<entry:1>", "<entry:1>"]
    assert [call["runnable_kind"] for call in captured] == [kind, kind]


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_entry_selector_requires_an_entry_of_the_requested_kind(
    kind, monkeypatch, capsys
):
    path = _source(f"{kind}():\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("ran wrong kind")
    )
    other = "flow" if kind == "agic" else "agic"
    assert cli.main([path, f"{other}:"]) == 2
    assert f"runnable not found: {other}:" in capsys.readouterr().err
    Path(path).write_text("agic main():\n  Named.\n")
    assert cli.main([path, f"{kind}:"]) == 2
    assert f"runnable not found: {kind}:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "selector",
    [
        "_",
        "agic:_",
        "flow:_",
        "runnable:_",
        "runnable:",
        "<entry>",
        "agic:<entry>",
        "runnable:<entry>",
        "<entry:1>",
        "entry",
    ],
)
def test_script_rejects_legacy_entry_selectors(selector, monkeypatch):
    path = _source()
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("ran legacy alias")
    )
    assert cli.main([path, selector]) != 0


@pytest.mark.parametrize("name", ["run", "serve", "init", "default", "entry"])
def test_explicit_run_preserves_command_like_runnable_names(name, monkeypatch):
    path = _source(f"agic {name}():\n  Hello.\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main(["run", path, name]) == 0
    assert captured[0]["runnable"] == name


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("selector", [[], ["agic:"]])
@pytest.mark.parametrize("primary", [["--", "hello", "--root", "literal"], ["-"]])
def test_unnamed_entry_preserves_options_named_inputs_and_stdin(
    explicit, selector, primary, monkeypatch
):
    path = _source("agic(_: Part[], topic: Text):\n  {{_}}\n")
    monkeypatch.setattr("sys.stdin", StringIO("from stdin"))
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert (
        cli.main(
            [
                *(["run"] if explicit else []),
                path,
                *selector,
                "--model",
                "--root",
                "--dev",
                "--quiet",
                "topic=demo",
                *primary,
            ]
        )
        == 0
    )
    assert captured[0]["model_body"] == "--root"
    assert captured[0]["dev"] == Path(".")
    assert captured[0]["raw_named"] == {"topic": "demo"}
    assert captured[0]["input"].get("_") == (
        "from stdin" if primary == ["-"] else "hello --root literal"
    )


@pytest.mark.parametrize("arguments", [[], ["--help"], ["agic:", "--help"]])
def test_entry_help_and_missing_input_never_execute(arguments, monkeypatch, capsys):
    path = _source("## Handle the request.\nagic:\n  {{_}}\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("executed help")
    )
    if arguments:

        class Unreadable:
            def read(self):
                pytest.fail("help read stdin")

        monkeypatch.setattr("sys.stdin", Unreadable())
    assert cli.main(["run", path, *arguments]) == (0 if arguments else 2)
    captured = capsys.readouterr()
    if arguments:
        assert not captured.err
        assert "too run demo.too" in captured.out
        assert "Handle the request" in captured.out
    else:
        assert not captured.out
        assert "Error: Missing required primary input." in captured.err
        assert "Usage: too run demo.too [OPTIONS] [INPUT...]" in captured.err


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "Missing required inputs: tone=<TONE>, INPUT."),
        (["tone=warm"], "Missing required primary input."),
        (["style=warm"], "Unknown named input 'style'."),
    ],
)
def test_implicit_entry_errors_preserve_the_invoked_usage(
    explicit, kind, arguments, message, monkeypatch, capsys
):
    path = _source(f"{kind}(_: Part[], tone: Text):\n  pass\n")
    monkeypatch.setattr(
        script,
        "_run",
        lambda *args, **kwargs: pytest.fail("invalid input must not run"),
    )
    assert cli.main([*(["run"] if explicit else []), path, *arguments]) == 2
    output = capsys.readouterr()
    assert not output.out
    error = strip_ansi(output.err)
    prefix = "run " if explicit else ""
    assert error.startswith(f"Error: {message}\n\nUsage:")
    assert f"Usage: too {prefix}demo.too [OPTIONS] [ARGUMENTS] [INPUT...]" in error
    assert f"{kind}:" not in error and "demo.too _" not in error


@pytest.mark.parametrize(
    "arguments",
    [[], ["--help"], ["--model", "test/model"], ["--dev", "--", "helper"], ["unknown"]],
)
def test_no_entry_shows_help_or_requires_an_explicit_runnable(arguments, monkeypatch):
    path = _source("agic helper():\n  Hello.\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("selected helper")
    )
    assert cli.main(["run", path, *arguments]) == (
        0 if arguments in ([], ["--help"]) else 2
    )


@pytest.mark.parametrize("executable", ["too", "toolang"])
@pytest.mark.parametrize("arguments", [[], ["--help"], ["--"]])
def test_static_run_help(executable, arguments, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [executable])
    assert cli.main(["run", *arguments]) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert (
        f"{executable} run <FILE> [RUNNABLE] [OPTIONS] [ARGUMENTS] [INPUT...]" in output
    )
    assert "* FILE" in output
    assert "Path to a .too file" in output
    assert "RUNNABLE" in output
    assert "Execute a .too file." in output
    assert "RUNNABLE Runnable name" in output
    assert "ARGUMENTS Named runnable inputs" in output
    assert "INPUT Primary runnable input" in output
    assert "Input:" not in output
    assert "script" not in output.lower()
    assert "NAME=VALUE" not in output
    assert "local" not in output.lower()
    assert "Omit run" not in output
    assert "Add --help after FILE or RUNNABLE for details." in output


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_runnable_listing_leaves_unnamed_names_and_missing_descriptions_empty(
    kind, monkeypatch, capsys
):
    path = _source(f"{kind}():\n  pass\n\n{kind} helper():\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    assert cli.main(["run", path, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    rows = output.split("Runnables:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert [row.split() for row in rows] == [[kind], ["helper", kind]]


@pytest.mark.parametrize("width", [44, 80, 120])
@pytest.mark.parametrize("description", ["", "## Explain the research findings.\n"])
def test_runnable_listing_keeps_names_and_kinds_on_one_line(
    width, description, monkeypatch, capsys
):
    path = _source(
        f"flow():\n  pass\n\n{description}agic synthesize_report():\n  pass\n"
    )
    monkeypatch.setenv("COLUMNS", str(width))
    assert cli.main(["run", path, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    rows = output.split("Runnables:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert rows[0].split() == ["flow"]
    assert rows[1].split()[:2] == ["synthesize_report", "agic"]
    assert all(cell_len(line) <= width for line in output.splitlines())


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_unnamed_runnable_help_uses_a_readable_title(kind, monkeypatch, capsys):
    path = _source(f"{kind}():\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    assert cli.main(["run", path, f"{kind}:", "--help"]) == 0
    assert strip_ansi(capsys.readouterr().out).startswith(f"Run the unnamed {kind}.\n")


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("doc", ["Compare [a] and [b].", "Render [/literal]."])
def test_runnable_help_preserves_literal_author_descriptions(
    kind, named, doc, monkeypatch, capsys
):
    name = " helper" if named else ""
    path = _source(f"## {doc}\n{kind}{name}():\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    for selection in ([], ["helper" if named else f"{kind}:"]):
        assert cli.main(["run", path, *selection, "--help"]) == 0
        output = capsys.readouterr()
        assert doc in strip_ansi(output.out)
        assert not output.err


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("suffix", [[], ["--", "text"], ["count=2"], ["--help"]])
@pytest.mark.parametrize("token", ["---", "----", "-----"])
def test_unnamed_entry_rejects_unknown_options_before_reading_or_running(
    explicit, suffix, token, monkeypatch, capsys
):
    path = _source("agic:\n  {{_}}\n")
    monkeypatch.setattr(
        "sys.stdin.read", lambda *_args: pytest.fail("invalid input read stdin")
    )
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("invalid input ran")
    )

    assert cli.main([*(["run"] if explicit else []), path, token, *suffix]) == 2

    error = " ".join(strip_ansi(capsys.readouterr().err).split())
    assert error.startswith(f"Error: No such option: {token}")
    assert "fenced input" not in error


@pytest.mark.parametrize("selector", ["agic:", "echo"])
@pytest.mark.parametrize("prefix", [["--"], ["hello"]])
@pytest.mark.parametrize("token", ["---", "----", "-----"])
def test_hyphens_after_the_input_boundary_remain_primary_input(
    selector, prefix, token, monkeypatch
):
    path = _source("agic:\n  {{_}}\n\nagic echo(_):\n  {{_}}\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs["input"]) or 0
    )
    assert cli.main(["run", path, selector, *prefix, token]) == 0
    assert captured == [{"_": token if prefix == ["--"] else f"hello {token}"}]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "arguments", [["--model", "dummy"], ["--", "hello"], ["topic=x"]]
)
def test_missing_script_entry_uses_one_concise_diagnostic(explicit, arguments, capsys):
    path = _source("flow research(_: Text) -> Text:\n  pass\n")
    assert cli.main([*(["run"] if explicit else []), path, *arguments]) == 2
    error = capsys.readouterr().err
    assert error.startswith("Error: No unnamed entry; select a runnable.\n")
    assert "Usage: too " in error


@pytest.mark.parametrize("root_boundary", [[], ["--"]])
@pytest.mark.parametrize("file_boundary", [[], ["--"]])
@pytest.mark.parametrize("tail", [[], ["agic:"], ["--", "literal --help"]])
def test_explicit_run_preserves_arguments_after_option_boundaries(
    root_boundary, file_boundary, tail, monkeypatch
):
    path = _source(
        "agic(_: Text):\n  Hello.\n" if tail[:1] == ["--"] else "agic():\n  Hello.\n"
    )
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main([*root_boundary, "run", *file_boundary, path, *tail]) == 0
    assert len(captured) == 1
    assert captured[0]["runnable"].startswith("<entry:")
    if tail[:1] == ["--"]:
        assert captured[0]["input"].get("_") == "literal --help"


@pytest.mark.parametrize("name", ["-demo.too", " demo.too"])
def test_explicit_run_preserves_special_filenames(monkeypatch, name):
    path = Path(_source()).rename(name)
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main(["run", "--", str(path)]) == 0
    assert len(captured) == 1


@pytest.mark.parametrize("tail", [["--help"], ["agic:", "--help"]])
def test_file_help_after_option_boundary_does_not_execute(tail, monkeypatch, capsys):
    path = _source()
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    assert cli.main(["run", "--", path, *tail]) == 0
    assert "too run demo.too" in capsys.readouterr().out


@pytest.mark.parametrize("executable", ["too", "toolang"])
@pytest.mark.parametrize("width", [44, 80, 120])
@pytest.mark.parametrize(
    "page",
    [
        ["init", "--help"],
        ["run", "--help"],
        ["serve", "--help"],
        ["run", "demo.too", "--help"],
        ["run", "demo.too", "agic:", "--help"],
    ],
)
def test_script_and_hosting_help_use_consistent_usage_and_fit_the_terminal(
    executable, width, page, monkeypatch, capsys
):
    _source()
    monkeypatch.setattr("sys.argv", [executable])
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("help executed")
    )
    assert cli.main(page) == 0
    output = strip_ansi(capsys.readouterr().out)
    assert f"Usage: {executable} {page[0]}" in output
    assert all(cell_len(line) <= width for line in output.splitlines())
    if page == ["run", "demo.too", "--help"]:
        assert "<entry:" not in output
        assert "agic:" not in output
        assert "agic" in output
        assert "Arguments:" not in output
        assert "Omit RUNNABLE" not in output
        assert "Pass primary input" not in output


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("alice", "expected a .too file"),
        ("main.ttt", "expected a .too file"),
        ("https://example.com/demo.too", "URLs are not supported"),
        ("owner/agent", "expected a .too file"),
        ("folder.too", "expected a file, got a directory"),
        ("folder", "expected a file, got a directory"),
    ],
)
@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_run_reports_invalid_files_without_hosting_advice(
    target, reason, arguments, capsys
):
    if target in {"folder.too", "folder"}:
        Path(target).mkdir()
    elif target == "main.ttt":
        Path(target).write_text("agic():\n  Hello.\n")
    assert cli.main(["run", target, *arguments]) == 2
    output = " ".join(capsys.readouterr().err.split())
    assert f"{reason}: {target}" in output
    assert "Invalid value for 'FILE'" in output
    assert "Usage: too run <FILE> [RUNNABLE] [OPTIONS] [ARGUMENTS] [INPUT...]" in output
    assert "serve" not in output
    assert "local" not in output.lower()


@pytest.mark.parametrize("label", ["--help", "-h"])
def test_run_option_boundary_keeps_a_following_filename_literal(label, capsys):
    assert cli.main(["run", "--", label]) == 2
    error = capsys.readouterr().err
    assert "Invalid value for 'FILE'" in error
    assert f"expected a .too file: {label}" in error


@pytest.mark.parametrize("explicit", [False, True])
def test_file_help_without_entry_marks_runnable_required(explicit, capsys):
    path = _source("agic helper():\n  Hello.\n")
    assert cli.main([*(["run"] if explicit else []), path, "--help"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "demo.too [OPTIONS] <RUNNABLE>" in output
    assert "Arguments:" not in output
    assert "Runnables:" in output
    assert "[default: main]" not in output


def test_run_reports_a_missing_source(capsys):
    assert cli.main(["run", "missing.too"]) == 1
    assert "script not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "root_args", [["--root", "root"], ["-r", "root"], ["--root=root"], ["-rroot"]]
)
def test_run_rejects_global_root_overrides(root_args, capsys):
    assert cli.main([*root_args, "run", _source()]) == 2
    assert "does not support global" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["alice", "demo.too"])
def test_target_first_run_rejects_agent(target, capsys):
    _source()
    assert cli.main([target, "run"]) == 2
    assert "Usage:" in capsys.readouterr().err


def test_script_cannot_select_a_synthetic_runtime_runnable(monkeypatch, capsys):
    path = _source()
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("ran synthetic default")
    )
    assert cli.main(["run", path, "--", "/agic:default\n\nHello."]) == 1
    assert "unknown or ambiguous" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_script_help_allows_unnamed_entry_with_explicit_main(
    kind, tmp_path, monkeypatch, capsys
):
    source = tmp_path / "conflict.too"
    source.write_text(f"{kind}:\n  pass\n\n{kind} main:\n  pass\n")
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("executed help")
    )
    assert cli.main(["run", str(source), "--help"]) == 0
    output = capsys.readouterr().out
    rows = output.split("Runnables:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert [row.split() for row in rows] == [[kind], ["main", kind]]


@pytest.mark.parametrize("explicit_run", [False, True])
def test_escaped_script_filename_stays_literal_across_entry_forms(
    explicit_run, monkeypatch, capsys
):
    source = Path("-demo.too")
    source.write_text("flow research():\n  pass\n")
    calls = []
    monkeypatch.setattr(
        script,
        "_run",
        lambda path, **kwargs: calls.append((path, kwargs["runnable"])) or 0,
    )
    header = ["run", "--", str(source)] if explicit_run else ["--", str(source)]
    assert cli.main([*header, "research", "--help"]) == 0
    assert calls == []
    assert "research" in capsys.readouterr().out
    assert cli.main([*header, "research"]) == 0
    assert calls == [(source.resolve(), "research")]
