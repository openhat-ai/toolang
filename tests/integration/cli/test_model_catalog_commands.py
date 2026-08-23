from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from toolang.base.types.model import ModelCatalogSnapshot
import toolang.cli.toolang.main as cli
from toolang.plugin.models.catalog import parse_model_catalog_data
from toolang.plugin.models.local import LlamaCppModels, OllamaModels


runner = CliRunner()


def test_plural_model_commands_are_public_resources() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.stderr
    assert "models" in result.stdout
    assert "providers" in result.stdout
    assert "adapters" in result.stdout


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
            "--model-catalog",
            str(catalog),
            "models",
            "--filter",
            "test/two[reasoning:false]",
            "--json",
        ],
        env={},
    )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    providers = parse_model_catalog_data(data)
    assert tuple(providers) == ("test",)
    assert tuple(providers["test"].models) == ("two",)


def test_models_explicit_missing_catalog_does_not_fall_back(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "--root",
            str(tmp_path / "root"),
            "--model-catalog",
            str(tmp_path / "missing.json"),
            "models",
        ],
        env={},
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert "explicit model catalog" in str(result.exception)


def _disable_local_discovery(monkeypatch) -> None:
    async def empty_snapshot(_source) -> ModelCatalogSnapshot:
        return ModelCatalogSnapshot(providers={}, models=(), revision="runtime:test")

    monkeypatch.setattr(OllamaModels, "snapshot", empty_snapshot)
    monkeypatch.setattr(LlamaCppModels, "snapshot", empty_snapshot)


def _catalog_data() -> dict[str, object]:
    return {
        "test": {
            "id": "test",
            "name": "Test",
            "env": [],
            "npm": "@ai-sdk/openai-compatible",
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
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "open_weights": False,
                    "limit": {"context": 1000, "output": 100},
                    "cost": {"input": 1, "output": 2},
                }
                for model_id, reasoning in (("one", True), ("two", False))
            },
        }
    }
