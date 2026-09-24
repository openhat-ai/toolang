"""Offline checks for repeatable catalog updates and stale preferences."""

import copy
import json

import pytest

from scripts import update_model_catalog as update
from toolang.common.json import dumps


def catalog():
    return {
        "test": {
            "models": {
                name: {
                    "id": name,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "tool_call": True,
                }
                for name in ("z-default", "c-new", "a-old")
            }
        }
    }


def test_preferences_only_reorder_and_keep_new_and_deprecated_models():
    source = catalog()
    source["test"]["models"]["a-old"]["status"] = "deprecated"
    original = copy.deepcopy(source)
    result = update.reorder_catalog(source, {"test": ["z-default", "c-new"]})
    assert list(result["test"]["models"]) == ["z-default", "c-new", "a-old"]
    assert result == source == original
    # Same source data in a different map order must produce identical bytes.
    source["test"]["models"] = dict(reversed(list(source["test"]["models"].items())))
    repeated = update.reorder_catalog(source, {"test": ["z-default", "c-new"]})
    assert dumps(result, sort_keys=False) == dumps(repeated, sort_keys=False)


@pytest.mark.parametrize(
    "preferences",
    [
        {},
        {"missing": ["z-default"]},
        {"test": []},
        {"test": ["missing"]},
        {"test": ["z-default", "z-default"]},
    ],
)
def test_stale_or_invalid_preferences_fail(preferences):
    with pytest.raises(ValueError):
        update.reorder_catalog(catalog(), preferences)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "deprecated"),
        ("tool_call", False),
        ("modalities", {"input": ["text"], "output": ["image"]}),
    ],
)
def test_unsuitable_preferred_model_fails(field, value):
    source = catalog()
    source["test"]["models"]["z-default"][field] = value
    with pytest.raises(ValueError):
        update.reorder_catalog(source, {"test": ["z-default"]})


def test_text_provider_without_tools_can_have_a_default():
    source = catalog()
    for model in source["test"]["models"].values():
        model["tool_call"] = False
    result = update.reorder_catalog(source, {"test": ["z-default"]})
    assert next(iter(result["test"]["models"])) == "z-default"


def test_update_is_repeatable_and_failed_validation_never_replaces_output(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.json"
    source.write_text("{}")
    preferences = tmp_path / "preferences.json"
    preferences.write_text(json.dumps({"test": ["z-default"]}))
    output = tmp_path / "catalog.json"
    monkeypatch.setattr(update, "export_catalog", lambda *_: catalog())
    args = [
        "--source",
        str(source),
        "--preferences",
        str(preferences),
        "--output",
        str(output),
    ]
    assert update.main(args) == 0
    content = output.read_bytes()
    assert update.main([*args, "--check"]) == 0
    assert update.main(args) == 0
    assert output.read_bytes() == content
    preferences.write_text(json.dumps({"test": ["missing"]}))
    with pytest.raises(SystemExit) as error:
        update.main(args)
    assert error.value.code == 2
    assert output.read_bytes() == content
    preferences.write_text(json.dumps({"test": ["c-new"]}))
    with pytest.raises(SystemExit) as error:
        update.main([*args, "--check"])
    assert error.value.code == 1
    assert output.read_bytes() == content


def test_bundled_preferences_are_valid_and_reproducible():
    bundled = json.loads(update.OUTPUT.read_text())
    preferences = json.loads(update.PREFERENCES.read_text())
    assert (
        dumps(update.reorder_catalog(bundled, preferences), sort_keys=False)
        == update.OUTPUT.read_text()
    )


def test_oversized_export_leaves_existing_catalog_intact(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}")
    preferences = tmp_path / "preferences.json"
    preferences.write_text(json.dumps({"test": ["z-default"]}))
    output = tmp_path / "catalog.json"
    output.write_text("previous catalog")
    monkeypatch.setattr(update, "export_catalog", lambda *_: catalog())
    monkeypatch.setattr(update, "DEFAULT_MAX_CATALOG_BYTES", 1)
    with pytest.raises(SystemExit) as error:
        update.main(
            [
                "--source",
                str(source),
                "--preferences",
                str(preferences),
                "--output",
                str(output),
            ]
        )
    assert error.value.code == 2
    assert output.read_text() == "previous catalog"
