from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console
from rich.text import Text

from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
)
import toolang.cli.toolang.main as cli
import toolang.cli.toolang.commands.model_catalog as model_catalog_commands
from toolang.plugin.catalogs.models_dev.parsing import parse_model_catalog_data
from toolang.plugin.catalogs._local import LOCAL_ZERO_COST
from toolang.plugin.catalogs.llama_cpp import LlamaCppModelCatalog
from toolang.plugin.catalogs.ollama import OllamaModelCatalog

runner = CliRunner()


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("command", ["models", "providers"])
@pytest.mark.parametrize("all_", [False, True])
def test_empty_model_allow_keeps_complete_diagnostic_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: bool,
    command: str,
    all_: bool,
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "synthetic-key")
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data()))
    home = tmp_path / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n")
    (tmp_path / "config.toml").write_text('[allow]\nmodels = ["*"]\n')
    scope = home if agent else tmp_path
    (scope / "config.toml").write_text("[allow]\nmodels = []\n")

    exit_code = cli.main(
        [
            "--root",
            str(tmp_path),
            *(("alice",) if agent else ()),
            command,
            *(("--all",) if all_ else ()),
        ],
    )

    result = capsys.readouterr()
    assert exit_code == 0, result.err
    if not all_:
        assert result.out.strip() == f"0 {command}"
    elif command == "models":
        assert "STATUS" in result.out
        for ref in ("test/one", "test/two"):
            row = next(line for line in result.out.splitlines() if ref in line)
            assert row.split()[-1] == "blocked"
    else:
        assert "MODELS" in result.out
        assert "REASON" not in result.out
        assert "2/2" not in result.out
        assert "0/2" in result.out


def test_model_catalog_override_is_scoped_to_consuming_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    stdout = strip_ansi(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert "models" in stdout
    assert "providers" in stdout
    assert "adapters" not in stdout
    more_stdout = strip_ansi(runner.invoke(cli.app, ["more"]).stdout)
    assert "adapters" in more_stdout
    assert "--catalog" not in stdout
    assert "--models" not in stdout
    assert "List available models" in stdout
    assert "--model-catalog" not in stdout

    for command in (
        ["info", "--help"],
        ["serve", "--help"],
        ["start", "--help"],
        ["chat", "alice", "--help"],
        ["retry", "alice", "--help"],
        ["rerun", "alice", "--help"],
        ["models", "--help"],
        ["providers", "--help"],
        ["_serve", "--help"],
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
    assert "--all" in models_help
    assert "--all" in strip_ansi(providers_result.stdout)
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
            "--all",
            "--catalog",
            str(catalog),
            "--query",
            "test/two[reasoning=false]",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout, parse_float=float)
    providers, models = parse_model_catalog_data(data)
    assert tuple(providers) == ("test",)
    assert tuple(model.id for model in models if model._toolang.provider == "test") == (
        "two",
    )


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
            "--all",
            "--catalog",
            str(catalog),
            "--query",
            "test/one",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    providers, models = parse_model_catalog_data(
        json.loads(result.stdout, parse_float=float)
    )
    assert tuple(providers) == ("test",)
    assert tuple(model.id for model in models if model._toolang.provider == "test") == (
        "one",
    )


def test_models_loads_catalog_limits_that_models_dev_reports_as_zero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    providers = _catalog_data()
    provider_models = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], providers["test"])["models"],
    )
    provider_models["one"]["limit"] = {"context": 0, "output": 8192}
    catalog.write_text(
        json.dumps(
            {
                "models": {"test/one": {"id": "test/one", "name": "One"}},
                "providers": providers,
            }
        ),
        encoding="utf-8",
    )
    _disable_local_discovery(monkeypatch)

    table = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--all",
            "--catalog",
            str(catalog),
            "--query",
            "test/one",
        ],
        env={},
    )

    assert table.exit_code == 0, table.stderr
    stdout = strip_ansi(table.stdout)
    header = next(line for line in stdout.splitlines() if "CONTEXT" in line)
    row = next(line for line in stdout.splitlines() if "test/one" in line)
    context = header.index("CONTEXT")
    output = header.index("OUTPUT")
    assert row[context : context + len("CONTEXT")].strip() == "-"
    assert row[output : output + len("OUTPUT")].strip() == "8_192"

    exported = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--all",
            "--catalog",
            str(catalog),
            "--query",
            "test/one",
            "--json",
        ],
        env={},
    )

    assert exported.exit_code == 0, exported.stderr
    data = json.loads(exported.stdout, parse_float=float)
    assert data["test"]["models"]["one"]["limit"] == {"output": 8192}


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
            "--all",
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
            "--all",
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
            "STATUS",
            "CONTEXT",
            "OUTPUT",
            "INPUT",
            "CAPABILITIES",
            "PRICE ($/1M)",
        )
    )
    values = (
        "test/one",
        "1_000_000",
        "100_000",
        "text,image",
        "tool_call,reasoning,temperature,structured_output",
        "1.26 / 0.00",
        "unready (Missing env)",
    )
    assert [row.index(value) for value in values] == sorted(
        row.index(value) for value in values
    )
    for header_value, row_value in (
        ("CONTEXT", "1_000_000"),
        ("OUTPUT", "100_000"),
        ("PRICE ($/1M)", "1.26 / 0.00"),
    ):
        assert header.index(header_value) + len(header_value) == row.index(
            row_value
        ) + len(row_value)
    assert "PROFILE" not in stdout
    assert "per 1m" not in stdout
    assert "1 model" in stdout


@pytest.mark.parametrize("all_option", [None, "-a"])
def test_models_render_aligned_prices_and_group_summary(
    tmp_path: Path, monkeypatch, all_option
) -> None:
    data = _catalog_data()
    models = cast(
        dict[str, dict[str, object]], cast(dict[str, object], data["test"])["models"]
    )
    models["one"]["cost"] = {"input": 0.43, "output": 0.87}
    models["two"]["cost"] = {"input": 1.25, "output": 10}
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    monkeypatch.setenv("TEST_API_KEY", "synthetic-key")
    _disable_local_discovery(monkeypatch)
    result = runner.invoke(
        cli.app,
        ["--root", str(tmp_path), "models", *((all_option,) if all_option else ())],
    )
    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "test/" in line]
    assert len(lines) == 2
    assert "0.43 /  0.87" in lines[0]
    assert "1.25 / 10.00" in lines[1]
    assert lines[0].rindex("/") == lines[1].rindex("/")
    assert ("STATUS" in result.stdout) == bool(all_option)
    assert "AVAILABLE" not in result.stdout
    assert result.stdout.strip().endswith("2 models, 1 provider")


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


def test_models_summary_counts_local_catalogs_and_providers_show_availability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    async def ollama_snapshot(_source) -> ModelCatalogSnapshot:
        model = Model(
            id="local",
            name="local",
            _toolang=ModelToolang(provider="ollama", ready=True),
            modalities={"input": ("text",), "output": ("text",)},
            cost=dict(LOCAL_ZERO_COST),
        )
        provider = Provider(
            id="ollama",
            name="Ollama",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://ollama.test/v1",
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision="runtime:ollama",
            local=True,
        )

    async def llama_snapshot(_source) -> ModelCatalogSnapshot:
        model = Model(
            id="second",
            name="second",
            _toolang=ModelToolang(provider="llama_cpp", ready=True),
            modalities={"input": ("text",), "output": ("text",)},
            cost=dict(LOCAL_ZERO_COST),
        )
        provider = Provider(
            id="llama_cpp",
            name="llama.cpp",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://llama.test/v1",
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(model,),
            revision="runtime:llama_cpp",
            local=True,
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
    assert "llama_cpp/second" in stdout
    assert "2 models" in stdout

    exported = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--catalog",
            str(catalog),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.stderr
    assert set(json.loads(exported.stdout)) == {"ollama", "llama_cpp"}
    assert "_toolang" not in exported.stdout

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
    assert "2 providers" in providers_result.stdout
    assert captured_headers == (
        "PROVIDER",
        "MODELS",
        "ADAPTERS",
        "DEFAULT API",
        "ENV",
    )
    by_provider = {str(row[0]): row for row in captured_rows}
    ollama_available = by_provider["ollama"][1]
    llama_available = by_provider["llama_cpp"][1]
    assert isinstance(ollama_available, Text)
    assert ollama_available.plain == "1"
    assert not _is_red(ollama_available, 0)
    assert isinstance(llama_available, Text)
    assert llama_available.plain == "1"
    assert not _is_red(llama_available, 0)
    llama_adapters = by_provider["llama_cpp"][2]
    assert isinstance(llama_adapters, Text)
    assert llama_adapters.plain == "chat_completions"
    assert not _is_dim(llama_adapters, 0)
    llama_api = by_provider["llama_cpp"][3]
    assert isinstance(llama_api, Text)
    assert llama_api.plain == "http://llama.test/v1"
    assert not _is_red(llama_api, 0)


@pytest.mark.parametrize("configured", [False, True])
def test_providers_lists_resolved_api_and_model_adapters(
    tmp_path: Path,
    monkeypatch,
    configured: bool,
) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.delenv("TEST_ALT_API_KEY", raising=False)
    if configured:
        monkeypatch.setenv("TEST_API_KEY", "test-key")
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
            "--all",
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
    assert "1 provider" in stdout

    filtered = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "models",
            "--all",
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
            "--all",
            "--catalog",
            str(catalog),
        ],
        env={},
    )

    assert styled_result.exit_code == 0, styled_result.stderr
    styled_row = next(row for row in captured_rows if row[0] == "test")
    available = styled_row[1]
    assert isinstance(available, Text)
    assert available.plain == ("2/2" if configured else "0/2")
    assert _is_red(available, 0) is not configured
    adapters = styled_row[2]
    endpoint = styled_row[3]
    env = styled_row[4]
    assert isinstance(adapters, Text)
    assert adapters.plain == "chat_completions,messages"
    assert not _is_dim(adapters, 0)
    assert isinstance(endpoint, Text)
    assert not _is_red(endpoint, 0)
    assert isinstance(env, Text)
    assert env.plain == "TEST_API_KEY, TEST_ALT_API_KEY"
    assert _is_red(env, 0) is not configured
    assert not _is_red(env, env.plain.index(","))
    assert _is_red(env, env.plain.index("TEST_ALT_API_KEY")) is not configured

    json_result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--all",
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


@pytest.mark.parametrize("command", ["models", "providers"])
def test_catalog_reasons_only_appear_inside_model_status(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    data = _catalog_data()
    provider = cast(dict[str, object], data["test"])
    provider["npm"] = "@missing/adapter"
    provider["api"] = "${TEST_MISSING_API}"
    models = cast(dict[str, dict[str, object]], provider["models"])
    models["one"]["provider"] = {"api": "https://one.test/v1"}
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.delenv("TEST_MISSING_API", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    _disable_local_discovery(monkeypatch)
    rows: list[Sequence[str | Text]] = []
    monkeypatch.setattr(
        model_catalog_commands,
        "echo_table",
        lambda headers, values, **kwargs: rows.extend(values),
    )

    result = runner.invoke(cli.app, ["--root", str(tmp_path), command, "-a"])

    assert result.exit_code == 0, result.stderr
    reasons = {row[0]: row[-1] for row in rows}
    if command == "models":
        assert reasons == {
            "test/one": "unready (No adapter; Missing env)",
            "test/two": "unready (No adapter; No API URL; Missing env)",
        }
    else:
        assert all(len(row) == 5 for row in rows)
        assert all("No adapter" not in str(row) for row in rows)


@pytest.mark.parametrize("available_models", [0, 1])
def test_provider_api_and_counts_use_independent_availability(
    tmp_path: Path, monkeypatch, available_models: int
) -> None:
    catalog = tmp_path / "catalog.json"
    data = _catalog_data()
    provider = cast(dict[str, object], data["test"])
    provider["api"] = "${TEST_MISSING_API}"
    monkeypatch.delenv("TEST_MISSING_API", raising=False)
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    if available_models:
        models = cast(dict[str, dict[str, object]], provider["models"])
        models["one"]["provider"] = {"api": "https://one.test/v1"}
    catalog.write_text(json.dumps(data), encoding="utf-8")
    _disable_local_discovery(monkeypatch)
    rows: list[Sequence[str | Text]] = []
    monkeypatch.setattr(
        model_catalog_commands,
        "echo_table",
        lambda headers, values: rows.extend(values),
    )

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "providers",
            "--all",
            "--catalog",
            str(catalog),
        ],
    )

    assert result.exit_code == 0, result.stderr
    available, api, env = rows[0][1], rows[0][3], rows[0][4]
    assert isinstance(available, Text)
    assert available.plain == f"{available_models}/2"
    assert _is_red(available, 0) is (available_models == 0)
    assert isinstance(api, Text)
    assert api.plain == ("- (model overrides)" if available_models else "-")
    assert _is_red(api, 0)
    assert isinstance(env, Text)
    assert env.plain == "TEST_API_KEY"
    assert not _is_red(env, 0)
    assert len(rows[0]) == 5

    default_result = runner.invoke(
        cli.app,
        ["--root", str(tmp_path / "root"), "providers", "--catalog", str(catalog)],
    )
    assert default_result.exit_code == 0, default_result.stderr
    if available_models:
        assert len(rows[-1]) == 5


@pytest.mark.parametrize("target", [[], ["alice"]])
@pytest.mark.parametrize("colored", [False, True])
@pytest.mark.parametrize("command", ["models", "providers"])
def test_catalog_help_describes_optional_agent_without_loading(
    tmp_path: Path, monkeypatch, capsys, target: list[str], colored: bool, command: str
) -> None:
    def unexpected_load(*args, **kwargs):
        pytest.fail("help must not load model catalogs")

    monkeypatch.setattr(model_catalog_commands, "load_setup", unexpected_load)
    monkeypatch.setenv("TERM", "xterm-256color")
    if colored:
        monkeypatch.setenv("FORCE_COLOR", "1")
    else:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    result = cli.main(["--root", str(tmp_path), *target, command, "--help"])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert ("\x1b[" in output.out) is colored
    assert f"[AGENT] {command} [OPTIONS]" in stdout
    assert "Local agent name; omit for root configuration" in stdout
    assert "--catalog" in stdout
    assert ("--query" in stdout) is (command == "models")
    assert "--all" in stdout
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
                    "--all",
                    "-q",
                    f"test/{model}",
                    *(["--json"] if json_output else []),
                ]
            )
            output = capsys.readouterr()
            assert result == 0, output.err
            assert not output.err
            if json_output:
                providers, models = parse_model_catalog_data(
                    json.loads(output.out, parse_float=float)
                )
                actual = (
                    tuple(
                        model.id
                        for model in models
                        if model._toolang.provider == "test"
                    )
                    if providers
                    else ()
                )
                assert actual == ((model,) if model in expected else ())
            elif model in expected:
                assert f"test/{model}" in output.out
                assert "STATUS" in output.out
            else:
                assert output.out.strip() == "0 models"


@pytest.mark.parametrize("json_output", [False, True])
def test_models_uses_agent_provider_config_and_environment(
    tmp_path: Path, monkeypatch, capsys, json_output: bool
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    monkeypatch.delenv("TEST_AGENT_MODEL_KEY", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data()))
    data = _catalog_data()
    cast(dict[str, object], data["test"])["env"] = ["TEST_AGENT_MODEL_KEY"]
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    home = _resident_home(tmp_path, "alice")
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
                "test/one[adapter=chat_completions;available=true]",
                "-q",
                "test/two[adapter=chat_completions;available=true]",
                *(["--json"] if json_output else []),
            ]
        )
        output = capsys.readouterr()
        assert result == 0, output.err
        assert not output.err
        assert "synthetic-agent-key" not in output.out
        if json_output:
            providers, models = parse_model_catalog_data(
                json.loads(output.out, parse_float=float)
            )
            assert tuple(providers) == (("test",) if available else ())
            if available:
                assert tuple(
                    model.id for model in models if model._toolang.provider == "test"
                ) == ("one", "two")
                assert providers["test"].npm == "@ai-sdk/openai-compatible"
                assert "resolved" not in output.out
        else:
            assert ("test/one" in output.out) is available
            assert ("test/two" in output.out) is available


@pytest.mark.parametrize("command", ["models", "providers"])
@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize(
    "source", ["builtin", "root", "agent", "environment", "explicit"]
)
def test_model_resources_use_one_catalog_in_scope_precedence(
    tmp_path: Path, monkeypatch, capsys, command: str, agent: bool, source: str
) -> None:
    from toolang.plugin.catalogs.models_dev import path as catalog_path

    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TOOLANG_MODEL_CATALOG", raising=False)
    home = _resident_home(tmp_path, "alice")
    default_home = _resident_home(tmp_path, "default")
    (default_home / "catalog.json").write_text("invalid JSON")
    (default_home / "config.toml").write_text("invalid TOML [")
    paths = {
        "builtin": tmp_path / "builtin.json",
        "root": tmp_path / "catalog.json",
        "agent": home / "catalog.json",
        "environment": tmp_path / "environment.json",
        "explicit": tmp_path / "explicit.json",
    }
    priorities = tuple(paths)
    for name in priorities[: priorities.index(source) + 1]:
        data = _catalog_data()
        provider = cast(dict[str, object], data.pop("test"))
        provider["id"] = name
        provider["name"] = name
        paths[name].write_text(json.dumps({name: provider}))
    monkeypatch.setattr(catalog_path, "PACKAGED_MODEL_CATALOG", paths["builtin"])
    if source in ("environment", "explicit"):
        monkeypatch.setenv("TOOLANG_MODEL_CATALOG", str(paths["environment"]))
    options = ["--catalog", str(paths["explicit"])] if source == "explicit" else []

    result = cli.main(
        [
            "--root",
            str(tmp_path),
            *(["alice"] if agent else []),
            command,
            "--all",
            *options,
            "--json",
        ]
    )
    output = capsys.readouterr()

    assert result == 0, output.err
    expected = "root" if source == "agent" and not agent else source
    assert set(json.loads(output.out)) == {expected}


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
        message = "[models.providers.*] is not supported"
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


def _is_red(text: Text, offset: int) -> bool:
    style = text.get_style_at_offset(Console(color_system="standard"), offset)
    assert not style.bold and not style.dim
    return style.color is not None and style.color.name == "red"


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


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("command", ["models", "providers"])
def test_catalog_commands_share_default_and_complete_views(
    tmp_path: Path, monkeypatch, capsys, agent: bool, command: str
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "synthetic-key")
    monkeypatch.delenv("MISSING_KEY", raising=False)
    data = _catalog_data()
    provider = cast(dict[str, object], data["test"])
    data["offline"] = {**provider, "id": "offline", "env": ["MISSING_KEY"]}
    data["empty"] = {**provider, "id": "empty", "models": {}}
    data["excluded"] = {**provider, "id": "excluded"}
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    config_home = _resident_home(tmp_path, "alice") if agent else tmp_path
    (config_home / "config.toml").write_text(
        '[allow]\nmodels = ["test/one", "offline/*"]\n'
    )
    args = ["--root", str(tmp_path), *(["alice"] if agent else []), command]

    def invoke(options: list[str]) -> str:
        result = cli.main([*args, *options])
        output = capsys.readouterr()
        assert result == 0, output.err
        return output.out

    for all_ in (False, True):
        options = ["--all"] if all_ else []
        output = invoke([*options, "--json"])
        exported = json.loads(output)
        expected = {"test", "offline", "excluded"} if all_ else {"test"}
        if command == "providers" and all_:
            expected.add("empty")
        assert set(exported) == expected
        assert set(exported["test"]["models"]) == ({"one", "two"} if all_ else {"one"})
        assert "_toolang" not in output
        assert "synthetic-key" not in output
        table = invoke(options)
        assert ("offline" in table) is all_
        assert ("excluded" in table) is all_
        assert ("empty" in table) is (all_ and command == "providers")
        if command == "models":
            for query, expected_providers in (
                ("*[available=false]", {"offline"} if all_ else set()),
                ("test/two[available]", {"test"} if all_ else set()),
            ):
                queried = invoke([*options, "--query", query, "--json"])
                assert set(json.loads(queried)) == expected_providers


@pytest.mark.parametrize("all_", [False, True])
def test_models_json_uses_the_published_version_without_rereading_source(
    tmp_path: Path, monkeypatch, all_: bool
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "synthetic-key")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_catalog_data()))
    original_setup = model_catalog_commands._setup

    def setup_then_remove_source(*args, **kwargs):
        setup = original_setup(*args, **kwargs)
        path.unlink()
        return setup

    monkeypatch.setattr(model_catalog_commands, "_setup", setup_then_remove_source)
    result = runner.invoke(
        cli.app,
        ["--root", str(tmp_path), "models", *(["--all"] if all_ else []), "--json"],
    )
    assert result.exit_code == 0, result.stderr
    assert set(json.loads(result.stdout)["test"]["models"]) == {"one", "two"}


@pytest.mark.parametrize("command", ["models", "providers"])
@pytest.mark.parametrize("setting", ["default", "compact"])
def test_full_catalog_can_inspect_unready_configured_models(
    tmp_path, monkeypatch, command, setting
):
    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    (tmp_path / "catalog.json").write_text(json.dumps(_catalog_data()))
    (tmp_path / "config.toml").write_text(f'[{setting}]\nmodel = "test/one"\n')
    result = runner.invoke(
        cli.app, ["--root", str(tmp_path), command, "--all", "--json"]
    )
    assert result.exit_code == 0, result.exception
    assert set(json.loads(result.stdout)["test"]["models"]) == {"one", "two"}


@pytest.mark.parametrize("command", ["models", "providers"])
def test_catalog_cli_reports_published_route_failures_without_resolving_again(
    tmp_path,
    monkeypatch,
    command,
):
    import asyncio

    from toolang.common.layout import AgentLayout
    from toolang.setup import routes
    from toolang.setup.watcher import load_setup

    _disable_local_discovery(monkeypatch)
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    source = tmp_path / "catalog.json"
    source.write_text(json.dumps(_catalog_data()))
    setup = asyncio.run(
        load_setup(
            AgentLayout.resident(tmp_path, "default"),
            agent_context=False,
            validate_defaults=False,
        )
    )
    monkeypatch.setattr(model_catalog_commands, "_setup", lambda *args, **kwargs: setup)
    monkeypatch.setenv("TEST_API_KEY", "added-after-publication")
    source.unlink()

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("CLI must consume published routes")

    monkeypatch.setattr(routes, "resolve_provider", unexpected_resolution)
    rows = []
    monkeypatch.setattr(
        model_catalog_commands,
        "echo_table",
        lambda headers, values, **kwargs: rows.extend(values),
    )
    result = runner.invoke(cli.app, ["--root", str(tmp_path), command, "--all"])
    assert result.exit_code == 0, result.exception
    assert rows
    if command == "models":
        assert all(row[-1] == "unready (Missing env)" for row in rows)
    else:
        assert all(len(row) == 5 for row in rows)
    assert all("No adapter" not in str(row[-1]) for row in rows)
    assert all("added-after-publication" not in str(row) for row in rows)


@pytest.mark.parametrize("command", ["models", "providers"])
def test_cli_keeps_invalid_modes_in_full_catalog_only(tmp_path, monkeypatch, command):
    _disable_local_discovery(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "secret")
    data = json.loads(json.dumps(_catalog_data()))
    data["test"]["models"]["two"]["provider"] = {"mode": "missing"}
    (tmp_path / "catalog.json").write_text(json.dumps(data))

    for all_ in (False, True):
        result = runner.invoke(
            cli.app,
            ["--root", str(tmp_path), command, *(["--all"] if all_ else []), "--json"],
        )
        assert result.exit_code == 0, result.exception
        models = json.loads(result.stdout)["test"]["models"]
        assert set(models) == ({"one", "two"} if all_ else {"one"})
        if all_:
            assert models["two"]["provider"] == {"mode": "missing"}


@pytest.mark.parametrize("json_output", [True, False])
def test_adapters_lists_installed_metadata_without_loading_setup_or_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool
) -> None:
    from types import SimpleNamespace

    from toolang.plugin import loading

    def unexpected_load(*args: object, **kwargs: object) -> None:
        pytest.fail("Installed plugin inspection must not load setup or plugins")

    entries = [
        SimpleNamespace(
            name="unloadable_adapter",
            dist=SimpleNamespace(metadata={"Name": "external-package"}),
            load=unexpected_load,
        )
    ]
    monkeypatch.setattr(
        loading,
        "entry_points",
        lambda *, group: entries if group == "toolang.model_adapter" else [],
    )
    monkeypatch.setattr("toolang.setup.watcher.SetupWatcher.refresh", unexpected_load)
    (tmp_path / "config.toml").write_text("not valid TOML [")
    (tmp_path / "catalog.json").write_text("not valid JSON")
    result = runner.invoke(
        cli.app,
        ["--root", str(tmp_path), "adapters", *(["--json"] if json_output else [])],
    )
    assert result.exit_code == 0, result.exception
    if json_output:
        assert json.loads(result.stdout) == [
            {"id": "unloadable_adapter", "source": "external"}
        ]
    else:
        assert "unloadable_adapter" in result.stdout
        assert "external" in result.stdout


@pytest.mark.parametrize("command", ["models", "providers"])
@pytest.mark.parametrize("all_", [False, True])
def test_model_status_columns_separate_readiness_and_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, all_: bool
) -> None:
    _disable_local_discovery(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "synthetic-key")
    monkeypatch.delenv("INSPECTION_MISSING_KEY", raising=False)
    data = _catalog_data()
    provider = cast(dict[str, object], data["test"])
    data["offline"] = {**provider, "id": "offline", "env": ["INSPECTION_MISSING_KEY"]}
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    (tmp_path / "config.toml").write_text(
        '[allow]\nmodels = ["test/one", "offline/one"]\n'
    )
    rows: list[dict[str, object]] = []

    def capture(headers, values, **kwargs):
        rows.extend(dict(zip(headers, row, strict=True)) for row in values)

    monkeypatch.setattr(model_catalog_commands, "echo_table", capture)
    result = runner.invoke(
        cli.app, ["--root", str(tmp_path), command, *(("--all",) if all_ else ())]
    )

    assert result.exit_code == 0, result.stderr
    if command == "models":
        by_id = {str(row["MODEL"]): row for row in rows}
        assert set(by_id) == (
            {"test/one", "test/two", "offline/one", "offline/two"}
            if all_
            else {"test/one"}
        )
        if all_:
            assert by_id["test/one"]["STATUS"] == "ok"
            assert by_id["test/two"]["STATUS"] == "blocked"
            assert by_id["offline/one"]["STATUS"] == "unready (Missing env)"
            assert by_id["offline/two"]["STATUS"] == "blocked, unready (Missing env)"
            assert tuple(by_id["test/one"])[-1] == "STATUS"
            assert "AVAILABLE" not in by_id["test/one"]
            assert "REASON" not in by_id["offline/two"]
        else:
            assert "STATUS" not in by_id["test/one"]
    else:
        by_id = {str(row["PROVIDER"]): row for row in rows}
        assert set(by_id) == ({"test", "offline"} if all_ else {"test"})
        assert all(
            tuple(row) == ("PROVIDER", "MODELS", "ADAPTERS", "DEFAULT API", "ENV")
            for row in by_id.values()
        )
        if all_:
            assert str(by_id["test"]["MODELS"]) == "1/2"
            assert str(by_id["offline"]["MODELS"]) == "0/2"
        else:
            assert str(by_id["test"]["MODELS"]) == "1"
            assert "MODELS (OK/ALL)" not in by_id["test"]
