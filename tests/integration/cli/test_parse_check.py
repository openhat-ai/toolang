"""Batch source validation is quiet, offline, and independent of formatting."""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolang.cli.toolang.main import app

runner = CliRunner()


def test_parse_check_reports_each_file_once_and_never_writes(tmp_path):
    good = tmp_path / "good.too"
    good.write_text("agic good():\n    Hello\n")
    bad = tmp_path / "bad.too"
    bad.write_text("# Heading\nagic bad():\n  {{_typo}}\n")
    second = tmp_path / "second.too"
    second.write_text("agic bad() -> Missing:\n  Hello\n")
    before = {p: p.read_bytes() for p in (good, bad, second)}
    result = runner.invoke(app, ["parse", str(tmp_path), str(bad), "--check"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.count(f"{bad}:3:") == 1
    assert f"{second}:1:" in result.stderr
    assert {p: p.read_bytes() for p in before} == before


def test_parse_check_accepts_unformatted_stdin_and_labels_errors():
    good = runner.invoke(
        app, ["parse", "-", "--check", "--ast"], input="agic work():\n    Hi"
    )
    assert good.exit_code == 0 and good.output == ""
    bad = runner.invoke(
        app,
        ["parse", "-", "--check", "--stdin-filepath", "work.too"],
        input="agic work():\n  {{_bad}}\n",
    )
    assert bad.exit_code == 1
    assert bad.stdout == ""
    assert "work.too:2:" in bad.stderr


@pytest.mark.parametrize("option", ["--cst", "--json", "--compact"])
def test_parse_check_rejects_tree_output_options(option):
    result = runner.invoke(app, ["parse", "-", "--check", option], input="")
    assert result.exit_code == 2


def test_parse_check_continues_after_read_failure(tmp_path):
    bad = tmp_path / "bytes.too"
    bad.write_bytes(b"\xff")
    other = tmp_path / "other.too"
    other.write_text("agic bad():\n  {{_bad}}\n")
    result = runner.invoke(app, ["parse", str(bad), str(other), "--check"])
    assert result.exit_code == 1
    assert str(bad) in result.stderr and str(other) in result.stderr


def test_regular_parse_still_requires_one_file(tmp_path):
    result = runner.invoke(app, ["parse", str(tmp_path), str(tmp_path)])
    assert result.exit_code == 2


def test_parse_check_continues_after_missing_path_and_ignores_non_sources(tmp_path):
    missing = tmp_path / "missing.too"
    source = tmp_path / "nested" / "bad.too"
    source.parent.mkdir()
    source.write_text("agic bad():\n  {{missing}}\n")
    (source.parent / "ignore.txt").write_text("not Toolang")
    result = runner.invoke(app, ["parse", str(missing), str(tmp_path), "--check"])
    assert result.exit_code == 1
    assert f"{missing}:1:1:" in result.stderr
    assert f"{source}:2:3:" in result.stderr
    assert "ignore.txt" not in result.stderr


def test_parse_check_deduplicates_symlinks(tmp_path):
    source = tmp_path / "bad.too"
    source.write_text("agic bad():\n  {{missing}}\n")
    alias = tmp_path / "alias.too"
    alias.symlink_to(source)
    result = runner.invoke(app, ["parse", str(source), str(alias), "--check"])
    assert result.exit_code == 1
    assert result.stderr.count("template input is missing") == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["-", "file.too", "--check"],
        ["file.too", "--check", "--stdin-filepath", "label.too"],
    ],
)
def test_parse_check_rejects_mixed_stdin_and_files(arguments):
    result = runner.invoke(app, ["parse", *arguments], input="")
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "reference", ["_²", "_" + "9" * 5000], ids=["unicode", "large"]
)
def test_parse_check_continues_after_invalid_history_reference(tmp_path, reference):
    first = tmp_path / "first.too"
    first.write_text(f"flow main:\n  repeat 1 time:\n    run: {{{{{reference}}}}}\n")
    second = tmp_path / "second.too"
    second.write_text("agic bad():\n  {{missing}}\n")
    result = runner.invoke(app, ["parse", str(first), str(second), "--check"])
    assert result.exit_code == 1
    assert f"{first}:3:" in result.stderr
    assert "outside the active window" in result.stderr
    assert f"{second}:2:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("nested", [False, True])
def test_parse_check_reports_unreadable_directories_and_continues(
    tmp_path, monkeypatch, nested
):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.too").write_text("agic hidden():\n  {{missing}}\n")
    other = tmp_path / "other.too"
    other.write_text("agic other():\n  {{missing}}\n")
    scandir = os.scandir

    def scan(path):
        if Path(path) == locked:
            raise PermissionError(13, "Permission denied", str(locked))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", scan)
    paths = [str(tmp_path)] if nested else [str(locked), str(other)]
    result = runner.invoke(app, ["parse", *paths, "--check"])
    assert result.exit_code == 1
    assert f"{locked}:1:1:" in result.stderr
    assert "Permission denied" in result.stderr
    assert f"{other}:2:" in result.stderr


def test_parse_check_reports_home_expansion_failure_and_continues(
    tmp_path, monkeypatch
):
    missing = Path("~unknown/source.too")
    other = tmp_path / "other.too"
    other.write_text("agic other():\n  {{missing}}\n")
    expanduser = Path.expanduser

    def expand(path):
        if path == missing:
            raise RuntimeError("Could not determine home directory.")
        return expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expand)
    result = runner.invoke(app, ["parse", str(missing), str(other), "--check"])
    assert result.exit_code == 1
    assert f"{missing}:1:1:" in result.stderr
    assert "Could not determine home directory" in result.stderr
    assert f"{other}:2:" in result.stderr


def test_parse_check_does_not_expand_discovered_literal_tilde_names(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "~notes.too").write_text("agic work():\n  Hello\n")
    result = runner.invoke(app, ["parse", ".", "--check"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


@pytest.mark.parametrize("command", [["parse"], ["fmt", "--stdout"], ["highlight"]])
def test_source_output_commands_report_home_expansion_failure(monkeypatch, command):
    original = Path.expanduser

    def expand(path):
        if str(path) == "~unknown/source.too":
            raise RuntimeError("Could not determine home directory.")
        return original(path)

    monkeypatch.setattr(Path, "expanduser", expand)
    result = runner.invoke(app, [*command, "~unknown/source.too"])
    assert result.exit_code == 1
    assert "Could not determine home directory" in result.stderr
    assert not isinstance(result.exception, RuntimeError)
