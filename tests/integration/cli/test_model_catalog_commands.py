from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
import json
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console
from rich.text import Text

from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
import toolang.cli.toolang.main as cli
import toolang.cli.toolang.commands.model_catalog as model_catalog_commands
from toolang.plugin.models.catalog import parse_model_catalog_data
from toolang.plugin.models.local import LlamaCppModelCatalog, OllamaModelCatalog


runner = CliRunner()


def test_model_catalog_override_is_scoped_to_consuming_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    stdout = strip_ansi(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert "models" in stdout
    assert "providers" in stdout
    assert "adapters" in stdout
    assert "--catalog" not in stdout
    assert "--models" not in stdout
    assert "List available models" in stdout
    assert "--model-catalog" not in stdout

    for command in (
        ["info", "--help"],
        ["run", "--help"],
        ["start", "--help"],
        ["chat", "alice", "--help"],
        ["retry", "alice", "--help"],
        ["rerun", "alice", "--help"],
        ["models", "--help"],
        ["providers", "--help"],
        ["serve", "--help"],
    ):
        command_result = runner.invoke(cli.app, command)
        assert command_result.exit_code == 0, command_result.stderr
        command_help = strip_ansi(command_result.stdout)
        assert "--catalog" in command_help
        assert "--models" not in command_help

    for command in (["list", "--help"], ["adapters", "--help"]):
        command_result = runner.invoke(cli.app, command)
        assert command_result.exit_code == 0, command_result.stderr
        command_help = strip_ansi(command_result.stdout)
        assert "--catalog" not in command_help
        assert "--models" not in command_help

    unsupported = runner.invoke(cli.app, ["--catalog", "catalog.json", "list"])
    assert unsupported.exit_code == 2
    assert "No such option: --catalog" in strip_ansi(unsupported.stderr)

    removed = runner.invoke(cli.app, ["models", "--models", "models.json"])
    assert removed.exit_code == 2
    assert "No such option: --models" in strip_ansi(removed.stderr)


def test_models_is_a_leaf_command_without_file_output_options() -> None:
    models_result = runner.invoke(cli.app, ["models", "--help"])
    providers_result = runner.invoke(cli.app, ["providers", "--help"])

    assert models_result.exit_code == 0, models_result.stderr
    assert providers_result.exit_code == 0, providers_result.stderr
    models_help = " ".join(strip_ansi(models_result.stdout).replace("│", "").split())
    assert "--query" in models_help
    assert "--query-help" not in models_help
    assert "--query-schema" not in models_help
    assert "too query" in models_help
    assert "models'" in models_help
    assert "--json" in models_help
    assert "Write filtered models as JSON" in models_help
    assert "--output" not in models_help
    assert "--force" not in models_help
    assert "Write catalog providers as JSON" in strip_ansi(providers_result.stdout)

    for subcommand in ("inspect", "update"):
        result = runner.invoke(cli.app, ["models", subcommand])
        assert result.exit_code != 0
        assert "unexpected extra argument" in strip_ansi(result.stderr).lower()


def test_models_query_exports_a_valid_complete_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "api.json"
    catalog.write_text(json.dumps(_catalog_data()), encoding="utf-8")
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "test/two[reasoning=false]",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout, parse_float=Decimal)
    providers = parse_model_catalog_data(data)
    assert tuple(providers) == ("test",)
    assert tuple(providers["test"].models) == ("two",)


def test_models_query_accepts_combined_models_dev_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "models": {
                    "test/one": {
                        "id": "test/one",
                        "name": "One",
                    }
                },
                "providers": _catalog_data(),
            }
        ),
        encoding="utf-8",
    )
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "test/one",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    providers = parse_model_catalog_data(json.loads(result.stdout, parse_float=Decimal))
    assert tuple(providers) == ("test",)
    assert tuple(providers["test"].models) == ("one",)


def test_models_rejects_provider_agnostic_models_dev_file_without_a_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "swiss-ai/apertus-8b": {
                    "id": "swiss-ai/apertus-8b",
                    "name": "Apertus 8B",
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 65_536, "output": 8_192},
                }
            }
        ),
        encoding="utf-8",
    )
    _disable_local_discovery(monkeypatch)

    exit_code = cli.main(
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "models.dev models.json contains provider-agnostic metadata" in captured.err
    assert "https://models.dev/catalog.json" in captured.err
    assert "https://models.dev/api.json" in captured.err
    assert "Traceback" not in captured.err


def test_models_table_reports_invalid_query_without_a_traceback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(_catalog_data()), encoding="utf-8")
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "*[missing=value]",
        ],
        env={},
    )

    assert result.exit_code == 1
    assert "unknown models query field 'missing'" in result.stderr
    assert "Traceback" not in result.stderr


def test_models_accepts_month_precision_catalog_dates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data = _catalog_data()
    models = cast(dict[str, object], cast(dict[str, object], data["test"])["models"])
    model = cast(dict[str, object], models["one"])
    model["release_date"] = "2025-04"
    model["last_updated"] = "2026-01"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(data), encoding="utf-8")
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "test/one[release_date=2025-04-01;last_updated=2026-01-01]",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    exported = json.loads(result.stdout)
    assert exported["test"]["models"]["one"]["release_date"] == "2025-04"
    assert exported["test"]["models"]["one"]["last_updated"] == "2026-01"


def test_models_table_splits_profile_fields(tmp_path: Path, monkeypatch) -> None:
    data = _catalog_data()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(data), encoding="utf-8")
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "test/one",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = strip_ansi(result.stdout)
    header = next(line for line in stdout.splitlines() if "CONTEXT" in line)
    row = next(line for line in stdout.splitlines() if "test/one" in line)
    assert all(
        label in header
        for label in (
            "AVAILABLE",
            "CONTEXT",
            "OUTPUT",
            "INPUT",
            "CAPABILITIES",
            "PRICE ($/1M)",
        )
    )
    values = (
        "test/one",
        "no",
        "1_000_000",
        "100_000",
        "text,image",
        "tool_call,reasoning,temperature,structured_output",
        "$1.26 / $0.00",
    )
    assert [row.index(value) for value in values] == sorted(
        row.index(value) for value in values
    )
    for header_value, row_value in (
        ("CONTEXT", "1_000_000"),
        ("OUTPUT", "100_000"),
        ("PRICE ($/1M)", "$1.26 / $0.00"),
    ):
        assert header.index(header_value) + len(header_value) == row.index(
            row_value
        ) + len(row_value)
    assert "PROFILE" not in stdout
    assert "per 1m" not in stdout
    assert "1 model from 1 catalog: models.dev 1" in stdout


def test_models_explicit_missing_catalog_does_not_fall_back(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(tmp_path / "missing.json"),
        ],
        env={},
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert "explicit model catalog" in str(result.exception)


def test_models_ignores_implicit_models_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "models.json").write_text(
        json.dumps(_catalog_data()),
        encoding="utf-8",
    )
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["--root", str(root), "models", "--json"],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    assert "test" not in json.loads(result.stdout)


def test_models_summary_counts_local_catalogs_and_providers_diagnose_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    async def ollama_snapshot(_source) -> ModelCatalogSnapshot:
        model = Model(
            provider_id="ollama",
            id="local",
            name="local",
            modalities={"input": ("text",), "output": ("text",)},
            cost={"input": 0, "output": 0},
            local=True,
        )
        provider = Provider(
            id="ollama",
            name="Ollama",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://ollama.test/v1",
            models={model.id: model},
            extra={"runtime": {"status": "ready"}},
            local=True,
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision="runtime:ollama",
        )

    async def llama_snapshot(_source) -> ModelCatalogSnapshot:
        model = Model(
            provider_id="llama_cpp",
            id="offline",
            name="offline",
            modalities={"input": ("text",), "output": ("text",)},
            cost={"input": 0, "output": 0},
            local=True,
        )
        provider = Provider(
            id="llama_cpp",
            name="llama.cpp",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://llama.test/v1",
            models={model.id: model},
            extra={"runtime": {"status": "offline"}},
            local=True,
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision="runtime:llama_cpp",
        )

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", ollama_snapshot)
    monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", llama_snapshot)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = strip_ansi(result.stdout)
    assert "llama_cpp/offline" in stdout
    assert "4 models from 3 catalogs: models.dev 2, ollama 1, llama_cpp 1" in stdout

    captured_headers: tuple[str, ...] = ()
    captured_rows: list[tuple[str | Text, ...]] = []

    def capture_table(
        headers: Sequence[str],
        rows: Sequence[Sequence[str | Text]],
        *,
        justify: object | None = None,
    ) -> None:
        del justify
        nonlocal captured_headers, captured_rows
        captured_headers = tuple(headers)
        captured_rows = [tuple(row) for row in rows]

    monkeypatch.setattr(model_catalog_commands, "echo_table", capture_table)
    providers_result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--catalog",
            str(catalog),
        ],
        env={},
    )

    assert providers_result.exit_code == 0, providers_result.stderr
    assert (
        "3 providers from 3 catalogs: models.dev 1, ollama 1, llama_cpp 1"
        in providers_result.stdout
    )
    assert captured_headers == (
        "PROVIDER",
        "NAME",
        "AVAILABLE",
        "ADAPTERS",
        "API",
        "ENV",
    )
    by_provider = {str(row[0]): row for row in captured_rows}
    assert by_provider["ollama"][2] == "1/1"
    assert by_provider["llama_cpp"][2] == "0/1"
    llama_adapters = by_provider["llama_cpp"][3]
    assert isinstance(llama_adapters, Text)
    assert llama_adapters.plain == "chat_completions"
    assert not _is_dim(llama_adapters, 0)
    llama_api = by_provider["llama_cpp"][4]
    assert isinstance(llama_api, Text)
    assert llama_api.plain == "http://llama.test/v1"
    assert _is_dim(llama_api, 0)


def test_providers_lists_resolved_api_and_model_adapters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    data = _catalog_data()
    provider_data = cast(dict[str, object], data["test"])
    provider_data["npm"] = "@ai-sdk/anthropic"
    provider_data["env"] = ["TEST_API_KEY", "TEST_ALT_API_KEY"]
    models = cast(dict[str, object], provider_data["models"])
    model = cast(dict[str, object], models["two"])
    model["provider"] = {"npm": "@ai-sdk/openai-compatible"}
    catalog.write_text(json.dumps(data), encoding="utf-8")
    _disable_local_discovery(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--catalog",
            str(catalog),
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = strip_ansi(result.stdout)
    header = next(line for line in stdout.splitlines() if "API" in line)
    row = next(line for line in stdout.splitlines() if "https://api.test/v1" in line)
    assert [header.index(label) for label in ("ADAPTERS", "API", "ENV")] == sorted(
        header.index(label) for label in ("ADAPTERS", "API", "ENV")
    )
    assert "https://api.test/v1" in row
    assert "messages" in row
    assert "TEST_API_KEY, TEST_ALT_API_KEY" in row
    assert "1 provider from 1 catalog: models.dev 1" in stdout

    filtered = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--query",
            "*[adapter=messages]",
            "--json",
        ],
        env={},
    )
    assert filtered.exit_code == 0, filtered.stderr
    filtered_data = json.loads(filtered.stdout)
    assert tuple(filtered_data["test"]["models"]) == ("one",)

    captured_rows: list[tuple[str | Text, ...]] = []

    def capture_table(
        headers: Sequence[str],
        rows: Sequence[Sequence[str | Text]],
        *,
        justify: object | None = None,
    ) -> None:
        del headers, justify
        captured_rows.extend(tuple(row) for row in rows)

    monkeypatch.setattr(model_catalog_commands, "echo_table", capture_table)
    styled_result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--catalog",
            str(catalog),
        ],
        env={},
    )

    assert styled_result.exit_code == 0, styled_result.stderr
    styled_row = next(row for row in captured_rows if row[0] == "test")
    adapters = styled_row[3]
    endpoint = styled_row[4]
    env = styled_row[5]
    assert isinstance(adapters, Text)
    assert adapters.plain == "chat_completions,messages"
    assert not _is_dim(adapters, 0)
    assert isinstance(endpoint, Text)
    assert not _is_dim(endpoint, 0)
    assert isinstance(env, Text)
    assert env.plain == "TEST_API_KEY, TEST_ALT_API_KEY"
    assert _is_dim(env, 0)
    assert not _is_dim(env, env.plain.index(","))
    assert _is_dim(env, env.plain.index("TEST_ALT_API_KEY"))

    json_result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--catalog",
            str(catalog),
            "--json",
        ],
        env={},
    )

    assert json_result.exit_code == 0, json_result.stderr
    provider = json.loads(json_result.stdout)["test"]
    assert provider["api"] == "https://api.test/v1"
    assert provider["npm"] == "@ai-sdk/anthropic"
    assert "resolved" not in provider


@pytest.mark.parametrize("target", [[], ["alice"]])
@pytest.mark.parametrize("colored", [False, True])
def test_models_help_describes_optional_agent_without_loading(
    tmp_path: Path, monkeypatch, capsys, target: list[str], colored: bool
) -> None:
    def unexpected_load(*args, **kwargs):
        pytest.fail("help must not load model catalogs")

    monkeypatch.setattr(
        model_catalog_commands, "load_catalog_inspection", unexpected_load
    )
    monkeypatch.setattr(
        model_catalog_commands, "load_matching_catalog_inspection", unexpected_load
    )
    monkeypatch.setenv("TERM", "xterm-256color")
    if colored:
        monkeypatch.setenv("FORCE_COLOR", "1")
    else:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    result = cli.main(["--root", str(tmp_path), *target, "models", "--help"])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert ("\x1b[" in output.out) is colored
    assert "[AGENT] models [OPTIONS]" in stdout
    assert "model catalog and configuration" in stdout
    assert "--catalog" in stdout
    assert "--query" in stdout
    assert "--json" in stdout
    assert not output.err
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("json_output", [False, True])
def test_models_uses_isolated_resident_catalogs(
    tmp_path: Path, monkeypatch, capsys, json_output: bool
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data(("one",))))
    for name in ("alice", "models", "default"):
        home = _resident_home(tmp_path, name)
        (home / "catalog.json").write_text(json.dumps(_catalog_data(("two",))))

    # Repeat cross-context hits and misses after warming the catalog caches.
    for target, expected in (
        ([], ("one",)),
        (["alice"], ("two",)),
        (["agent:models"], ("two",)),
        ([], ("one",)),
        (["agent:alice"], ("two",)),
    ):
        for model in ("one", "two"):
            result = cli.main(
                [
                    "-r",
                    str(tmp_path),
                    *target,
                    "models",
                    "-q",
                    f"test/{model}",
                    *(["--json"] if json_output else []),
                ]
            )
            output = capsys.readouterr()
            assert result == 0, output.err
            assert not output.err
            if json_output:
                providers = parse_model_catalog_data(
                    json.loads(output.out, parse_float=Decimal)
                )
                actual = tuple(providers["test"].models) if providers else ()
                assert actual == ((model,) if model in expected else ())
            elif model in expected:
                assert f"test/{model}" in output.out
                assert "AVAILABLE" in output.out
            else:
                assert output.out.strip() == "No models matched query."


@pytest.mark.parametrize("json_output", [False, True])
def test_models_uses_agent_provider_config_and_environment(
    tmp_path: Path, monkeypatch, capsys, json_output: bool
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    monkeypatch.delenv("TEST_AGENT_MODEL_KEY", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data()))
    home = _resident_home(tmp_path, "alice")
    (home / "config.toml").write_text(
        '[models.providers.test]\nadapter = "messages"\n'
        'key_env = "TEST_AGENT_MODEL_KEY"\n'
    )
    (home / ".env").write_text("TEST_AGENT_MODEL_KEY=synthetic-agent-key\n")
    _resident_home(tmp_path, "bob")

    for target, key_override, available in (
        (["alice"], None, True),
        ([], None, False),
        (["bob"], None, False),
        (["alice"], "", False),
        (["alice"], None, True),
    ):
        if key_override is None:
            monkeypatch.delenv("TEST_AGENT_MODEL_KEY", raising=False)
        else:
            monkeypatch.setenv("TEST_AGENT_MODEL_KEY", key_override)
        result = cli.main(
            [
                "--root",
                str(tmp_path),
                *target,
                "models",
                "-q",
                "test/one[adapter=messages;available=true]",
                "-q",
                "test/two[adapter=messages;available=true]",
                *(["--json"] if json_output else []),
            ]
        )
        output = capsys.readouterr()
        assert result == 0, output.err
        assert not output.err
        assert "synthetic-agent-key" not in output.out
        if json_output:
            providers = parse_model_catalog_data(
                json.loads(output.out, parse_float=Decimal)
            )
            assert tuple(providers) == (("test",) if available else ())
            if available:
                assert tuple(providers["test"].models) == ("one", "two")
                assert providers["test"].npm == "@ai-sdk/openai-compatible"
                assert "resolved" not in output.out
        else:
            assert ("test/one" in output.out) is available
            assert ("test/two" in output.out) is available


@pytest.mark.parametrize("source", ["agent", "environment", "explicit"])
def test_models_agent_catalog_override_precedence(
    tmp_path: Path, monkeypatch, capsys, source: str
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    home = _resident_home(tmp_path, "alice")
    paths = {
        "root": tmp_path / "catalog.json",
        "agent": home / "catalog.json",
        "environment": tmp_path / "environment.json",
        "explicit": tmp_path / "explicit.json",
    }
    for name, path in paths.items():
        data = _catalog_data()
        cast(dict[str, object], data["test"])["name"] = name
        path.write_text(json.dumps(data))
    if source != "agent":
        (home / ".env").write_text(f"TOOLANG_MODEL_CATALOG={paths['environment']}\n")
    options = ["--catalog", str(paths["explicit"])] if source == "explicit" else []

    result = cli.main(["--root", str(tmp_path), "alice", "models", *options, "--json"])
    output = capsys.readouterr()

    assert result == 0, output.err
    assert json.loads(output.out)["test"]["name"] == source


@pytest.mark.parametrize("options", [[], ["-q", "test/*"], ["--json"]])
def test_models_missing_agent_does_not_fall_back_or_create_a_home(
    tmp_path: Path, capsys, options: list[str]
) -> None:
    result = cli.main(["--root", str(tmp_path), "missing", "models", *options])
    output = capsys.readouterr()

    assert result == 1
    assert "Agent missing not found" in output.err
    assert "Traceback" not in output.err
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("invalid_input", ["config", "catalog"])
@pytest.mark.parametrize(
    "options", [[], ["-q", "test/*"], ["--json"], ["-q", "test/*", "--json"]]
)
def test_models_reports_agent_input_type_errors_without_a_traceback(
    tmp_path: Path, monkeypatch, capsys, invalid_input: str, options: list[str]
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data()))
    home = _resident_home(tmp_path, "alice")
    if invalid_input == "config":
        (home / "config.toml").write_text("[models]\nproviders = []\n")
        message = "models providers config must be a table"
    else:
        (home / "catalog.json").write_text('{"test": 42}')
        message = "provider 'test' must be an object"

    result = cli.main(["--root", str(tmp_path), "alice", "models", *options])
    output = capsys.readouterr()

    assert result == 1
    assert message in output.err
    assert "Traceback" not in output.err
    assert not output.out


def _resident_home(root: Path, name: str) -> Path:
    home = root / "agents" / name
    home.mkdir(parents=True)
    # Inspection needs a resident home, not a parsed or running program.
    (home / "agent.too").write_text("not a valid Toolang program\n")
    return home


def _disable_local_discovery(monkeypatch) -> None:
    async def empty_snapshot(_source) -> ModelCatalogSnapshot:
        return ModelCatalogSnapshot(providers={}, models=(), revision="runtime:test")

    monkeypatch.setattr(OllamaModelCatalog, "snapshot", empty_snapshot)
    monkeypatch.setattr(LlamaCppModelCatalog, "snapshot", empty_snapshot)


def _is_dim(text: Text, offset: int) -> bool:
    return bool(text.get_style_at_offset(Console(color_system="standard"), offset).dim)


def _catalog_data(model_ids: Sequence[str] = ("one", "two")) -> dict[str, object]:
    return {
        "test": {
            "id": "test",
            "name": "Test",
            "env": ["TEST_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.test/v1",
            "models": {
                model_id: {
                    "id": model_id,
                    "name": model_id.title(),
                    "attachment": False,
                    "reasoning": reasoning,
                    "tool_call": True,
                    "structured_output": True,
                    "temperature": True,
                    "release_date": "2026-01-01",
                    "last_updated": "2026-01-01",
                    "modalities": {
                        "input": ["text", "image"],
                        "output": ["text"],
                    },
                    "open_weights": False,
                    "limit": {"context": 1_000_000, "output": 100_000},
                    "cost": {"input": 1.256, "output": 0},
                }
                for model_id, reasoning in (("one", True), ("two", False))
                if model_id in model_ids
            },
        }
    }
