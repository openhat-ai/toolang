from __future__ import annotations

from pathlib import Path

from toolang.cli import build_parser, main


def test_cli_has_expected_subcommands() -> None:
    parser = build_parser()
    namespace = parser.parse_args(["check", "alice"])

    assert namespace.command == "check"
    assert namespace.agent == "alice"


def test_cli_check_resolves_resident_agent_from_toolang_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "sample.too"
    (home / "alice.too").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    exit_code = main(["check", "alice"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "ok"
