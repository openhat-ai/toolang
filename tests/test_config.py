from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from toolang.config.toml import load_optional_toml


def test_load_optional_toml_returns_empty_mapping_for_missing_file(
    tmp_path: Path,
) -> None:
    assert load_optional_toml(tmp_path / "missing.toml") == {}


def test_load_optional_toml_loads_nested_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[web]\nui_base_url = "https://example.com"\n', encoding="utf-8")

    assert load_optional_toml(path) == {
        "web": {"ui_base_url": "https://example.com"}
    }


def test_load_optional_toml_propagates_decode_errors(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[invalid", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_optional_toml(path)
