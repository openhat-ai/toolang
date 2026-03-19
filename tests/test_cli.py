from toolang.cli import build_parser


def test_cli_has_expected_subcommands() -> None:
    parser = build_parser()
    namespace = parser.parse_args(["check", "tests/fixtures/sample.too"])

    assert namespace.command == "check"
    assert namespace.program.endswith("sample.too")
