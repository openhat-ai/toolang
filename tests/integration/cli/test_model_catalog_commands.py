from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
import json
from pathlib import Path
from typing import cast

from click import unstyle
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
import toolang.cli.toolang.main as cli
import toolang.cli.toolang.commands.model_catalog as model_catalog_commands
from toolang.plugin.models.catalog import parse_model_catalog_data
from toolang.plugin.models.local import LlamaCppModels, OllamaModels


runner = CliRunner()


def test_plural_model_commands_are_public_resources() -> None:
    result = runner.invoke(cli.app, ["--help"])
    stdout = unstyle(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert "models" in stdout
    assert "providers" in stdout
    assert "adapters" in stdout
    assert "--models" in stdout
    assert "Use a specified model catalog." in stdout
    assert "Inspect models." in stdout
    assert "--model-catalog" not in stdout


def test_model_commands_use_catalog_vocabulary() -> None:
    inspect_result = runner.invoke(cli.app, ["models", "inspect", "--help"])
    providers_result = runner.invoke(cli.app, ["providers", "--help"])

    assert inspect_result.exit_code == 0, inspect_result.stderr
    assert providers_result.exit_code == 0, providers_result.stderr
    assert "Inspect model catalog entries and availability." in unstyle(
        inspect_result.stdout
    )
    assert "Write catalog providers as JSON." in unstyle(providers_result.stdout)


def test_models_filter_exports_a_valid_complete_catalog(
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
            "--models",
            str(catalog),
            "models",
            "--filter",
            "test/two[reasoning:false]",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout, parse_float=Decimal)
    providers = parse_model_catalog_data(data)
    assert tuple(providers) == ("test",)
    assert tuple(providers["test"].models) == ("two",)


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
            "--models",
            str(catalog),
            "models",
            "--filter",
            "test/one",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = unstyle(result.stdout)
    header = next(line for line in stdout.splitlines() if "CONTEXT" in line)
    row = next(line for line in stdout.splitlines() if "test/one" in line)
    assert all(
        label in header
        for label in (
            "AVAILABLE",
            "CONTEXT",
            "OUTPUT",
            "INPUT",
            "CAPABILITY",
            "PRICE ($/1M)",
        )
    )
    values = (
        "test/one",
        "no",
        "1,000,000",
        "100,000",
        "text,image",
        "tool_call,reasoning,temperature,structured",
        "$1.26 / $0.00",
    )
    assert [row.index(value) for value in values] == sorted(
        row.index(value) for value in values
    )
    for header_value, row_value in (
        ("CONTEXT", "1,000,000"),
        ("OUTPUT", "100,000"),
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
            "--models",
            str(tmp_path / "missing.json"),
            "models",
        ],
        env={},
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert "explicit model catalog" in str(result.exception)


def test_models_inspect_json_labels_catalog_entries(
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
            "--models",
            str(catalog),
            "models",
            "inspect",
            "test/one",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert tuple(data["models"][0]) == (
        "adapter",
        "available",
        "catalog",
        "identity",
    )


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
        provider = Provider(
            id="llama_cpp",
            name="llama.cpp",
            env=(),
            npm="@ai-sdk/openai-compatible",
            api="http://llama.test/v1",
            models={},
            extra={"runtime": {"status": "offline"}},
            local=True,
        )
        return ModelCatalogSnapshot(
            providers={provider.id: provider},
            models=(),
            revision="runtime:llama_cpp",
        )

    monkeypatch.setattr(OllamaModels, "snapshot", ollama_snapshot)
    monkeypatch.setattr(LlamaCppModels, "snapshot", llama_snapshot)

    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "--models",
            str(catalog),
            "models",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = unstyle(result.stdout)
    assert "3 models from 3 catalogs: models.dev 2, ollama 1, llama_cpp 0" in stdout

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
            "--models",
            str(catalog),
            "providers",
        ],
        env={},
    )

    assert providers_result.exit_code == 0, providers_result.stderr
    assert captured_headers == (
        "PROVIDER",
        "NAME",
        "AVAILABLE",
        "ADAPTERS",
        "ENDPOINT",
        "ENV",
    )
    by_provider = {str(row[0]): row for row in captured_rows}
    assert by_provider["ollama"][2] == "1/1"
    assert by_provider["llama_cpp"][2] == "0"
    llama_adapters = by_provider["llama_cpp"][3]
    assert isinstance(llama_adapters, Text)
    assert llama_adapters.plain == "chat_completions"
    assert _color_name(llama_adapters, 0) is None
    llama_endpoint = by_provider["llama_cpp"][4]
    assert isinstance(llama_endpoint, Text)
    assert llama_endpoint.plain == "http://llama.test/v1"
    assert _color_name(llama_endpoint, 0) == "red"


def test_providers_lists_effective_endpoint_and_model_adapters(
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
            "--models",
            str(catalog),
            "providers",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    stdout = unstyle(result.stdout)
    header = next(line for line in stdout.splitlines() if "ENDPOINT" in line)
    row = next(line for line in stdout.splitlines() if "https://api.test/v1" in line)
    assert [header.index(label) for label in ("ADAPTERS", "ENDPOINT", "ENV")] == sorted(
        header.index(label) for label in ("ADAPTERS", "ENDPOINT", "ENV")
    )
    assert "https://api.test/v1" in row
    assert "chat_completions,messages" in row
    assert "TEST_API_KEY | TEST_ALT_API_KEY" in row

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
            "--models",
            str(catalog),
            "providers",
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
    assert _color_name(adapters, adapters.plain.index(",")) is None
    assert _color_name(adapters, adapters.plain.index("messages")) == "red"
    assert isinstance(endpoint, Text)
    assert _color_name(endpoint, 0) is None
    assert isinstance(env, Text)
    assert env.plain == "TEST_API_KEY | TEST_ALT_API_KEY"
    assert _color_name(env, 0) == "red"
    assert _color_name(env, env.plain.index("|")) is None
    assert _color_name(env, env.plain.index("TEST_ALT_API_KEY")) == "red"

    json_result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "--models",
            str(catalog),
            "providers",
            "--json",
        ],
        env={},
    )

    assert json_result.exit_code == 0, json_result.stderr
    provider = json.loads(json_result.stdout)["test"]
    assert provider["endpoint"] == "https://api.test/v1"
    assert provider["adapters"] == ["chat_completions", "messages"]
    assert "adapter" not in provider


def _disable_local_discovery(monkeypatch) -> None:
    async def empty_snapshot(_source) -> ModelCatalogSnapshot:
        return ModelCatalogSnapshot(providers={}, models=(), revision="runtime:test")

    monkeypatch.setattr(OllamaModels, "snapshot", empty_snapshot)
    monkeypatch.setattr(LlamaCppModels, "snapshot", empty_snapshot)


def _color_name(text: Text, offset: int) -> str | None:
    color = text.get_style_at_offset(Console(color_system="standard"), offset).color
    return color.name if color is not None else None


def _catalog_data() -> dict[str, object]:
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
            },
        }
    }
