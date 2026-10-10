from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
import shlex

import pytest
from rich.cells import cell_len
import typer
from typer.core import TyperCommand
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
    destination = (tmp_path / directory / "aide.too").resolve()
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
    assert lines[:2] == [
        "Created aide.too and toolang.toml.",
        "Add .toolang/ to .gitignore.",
    ]
    assert len(lines) == 3
    assert lines[2].startswith("Try: ")
    assert shlex.split(lines[2].removeprefix("Try: ")) == [
        executable,
        str(Path(directory) / "aide.too"),
        "--help",
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
    assert cli.main(["run", "aide.too", *([entry] if entry else []), "--help"]) == 0
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

    assert cli.main([*(["run"] if explicit else []), "aide.too"]) == 0
    assert "whats_for agic" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("entry", ["issue", "fix", "review", "update_i18n"])
def test_initialized_script_requires_its_input(entry, monkeypatch):
    assert cli.main(["init", "."]) == 0
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("executed without input")
    )

    assert cli.main(["aide.too", entry]) == 2


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
@pytest.mark.parametrize("filename", ["aide.too", "toolang.toml"])
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
    other = "toolang.toml" if filename == "aide.too" else "aide.too"
    assert not (tmp_path / other).exists()
    if kind == "file":
        assert output.read_text() == "keep"
    elif kind == "symlink":
        assert target.read_text() == "keep"
    elif kind == "dangling":
        assert output.is_symlink() and not target.exists()


def test_init_reports_all_existing_outputs(tmp_path, capsys):
    for name in ("aide.too", "toolang.toml"):
        (tmp_path / name).write_text("keep")

    assert cli.main(["init", "."]) == 1
    message = capsys.readouterr()
    assert message.err == (
        "Error: aborted to avoid overwriting: aide.too, toolang.toml\n"
    )
    assert message.out == ""
    for name in ("aide.too", "toolang.toml"):
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
        if self == tmp_path / "aide.too":
            raise PermissionError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert cli.main(["init", "."]) == 1
    assert capsys.readouterr().err == "Error: permission denied\n"
    assert (tmp_path / "toolang.toml").is_file()
    assert not (tmp_path / "aide.too").exists()


@pytest.mark.parametrize("filename", ["aide.too", "toolang.toml"])
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
    assert (tmp_path / "aide.too").exists() == (filename == "aide.too")


def test_concurrent_init_has_one_winner(tmp_path):
    def create():
        try:
            init_script(typer.Context(TyperCommand("too"), info_name="too"), tmp_path)
            return True
        except ClickException:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(results) == [False, True]
    assert (tmp_path / "aide.too").read_text() == load_template("script").raw_text


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


@pytest.mark.parametrize("selector", ["_", "agic:_", "runnable:_"])
def test_explicit_unnamed_entry_selection(selector, monkeypatch):
    path = _source()
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main(["run", path, selector]) == 0
    assert captured[0]["runnable"].startswith("<entry:")


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("selector", ["_", "{kind}:_", "runnable:_"])
def test_underscore_selects_the_same_entry_as_omission(kind, selector, monkeypatch):
    path = _source(f"{kind}():\n  pass\n\nagic main():\n  Named.\n")
    captured = []
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: captured.append(kwargs) or 0
    )
    assert cli.main([path]) == 0
    assert cli.main([path, selector.format(kind=kind)]) == 0
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
    assert cli.main([path, f"{other}:_"]) == 1
    article = "an" if other == "agic" else "a"
    assert f"runnable is not {article} {other}" in capsys.readouterr().err
    Path(path).write_text("agic main():\n  Named.\n")
    assert cli.main([path, "_"]) != 0


@pytest.mark.parametrize(
    "selector", ["<entry>", "agic:<entry>", "runnable:<entry>", "<entry:1>", "entry"]
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
@pytest.mark.parametrize("selector", [[], ["_"]])
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


@pytest.mark.parametrize("arguments", [[], ["--help"], ["_", "--help"]])
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
    text = strip_ansi(capsys.readouterr().out)
    assert "too run demo.too" in text
    assert "Handle the request" in text


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
@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_static_run_help(executable, arguments, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [executable])
    assert cli.main(["run", *arguments]) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert f"{executable} run [OPTIONS] <FILE> [RUNNABLE] [ARGUMENTS]" in output
    assert "* FILE" in output
    assert "Path to a .too file" in output
    assert "RUNNABLE" in output
    assert "Execute a .too file." in output
    assert "Runnable arguments" in output
    assert "script" not in output.lower()
    assert "NAME=VALUE" not in output
    assert "local" not in output.lower()
    assert "ARGUMENTS..." not in output
    assert (
        "Omit run if RUNNABLE is not a Toolang command, e.g. "
        f"{executable} aide.too whats_for." in output
    )
    assert "Add --help after FILE or RUNNABLE for details." in output


@pytest.mark.parametrize("root_boundary", [[], ["--"]])
@pytest.mark.parametrize("file_boundary", [[], ["--"]])
@pytest.mark.parametrize("tail", [[], ["_"], ["--", "literal --help"]])
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


@pytest.mark.parametrize("tail", [["--help"], ["_", "--help"]])
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
        ["run", "demo.too", "_", "--help"],
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
        assert "_ agic <entry:1>" in " ".join(output.split())
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
    assert "serve" not in output
    assert "local" not in output.lower()
    assert "ARGUMENTS..." not in output


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
def test_target_first_run_points_to_serve(target, capsys):
    _source()
    assert cli.main([target, "run"]) == 2
    assert "serve TARGET" in capsys.readouterr().err


def test_script_cannot_select_a_synthetic_runtime_runnable(monkeypatch, capsys):
    path = _source()
    monkeypatch.setattr(
        script, "_run", lambda *args, **kwargs: pytest.fail("ran synthetic default")
    )
    assert cli.main(["run", path, "--", ":runnable agic:default\n\nHello."]) == 1
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
    assert f"_ {kind} <entry:1>" in " ".join(output.split())
    assert f"main {kind}" in " ".join(output.split())
