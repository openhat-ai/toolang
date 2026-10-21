import asyncio
from dataclasses import replace

import pytest

from toolang.base.types.model import ModelOverride
from toolang.common.layout import AgentLayout
from toolang.up.contract import require_compatible, startup_contract
from toolang.up.instance import update_instance
from toolang.up.sandbox import resolve_launch


def test_explicit_settings_match_without_repeating_unrelated_bindings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={"PROVIDER_API_KEY": "secret-key"},
            drivers=frozenset({"chat"}),
            default_overrides={"model": ModelOverride(identity="test/private-model")},
            limit_overrides={"tokens": 1000, "time": 60},
            workspace_additions={"repo": "/one", "other": "/two"},
        )
    )
    saved = startup_contract(spec)
    assert "secret-key" not in str(saved)
    assert "private-model" not in str(saved)
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(layout, {})
    require_compatible(layout, {"port": 8123})
    require_compatible(layout, {"limit_overrides": {"tokens": 1000}})
    require_compatible(layout, {"workspace_additions": {"repo": "/one"}})
    require_compatible(layout, {"port": 8123}, {"limit_overrides": {"tokens": 1000}})
    with pytest.raises(ValueError, match="startup parameters differ: port.*restart"):
        require_compatible(layout, {"port": 8124})
    with pytest.raises(ValueError, match="startup parameters differ: port.*restart"):
        require_compatible(layout, {"port": 8124}, {"port": 8123})
    with pytest.raises(ValueError, match="workspace bindings"):
        require_compatible(layout, {"workspace_additions": {"repo": "/elsewhere"}})
    with pytest.raises(ValueError, match="run limits"):
        require_compatible(layout, {"limit_overrides": {"tokens": 2000}})


def test_only_explicit_requirements_need_saved_startup_bindings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    require_compatible(layout, {})
    with pytest.raises(ValueError, match="unverifiable"):
        require_compatible(layout, {"port": 8123})


@pytest.mark.parametrize(
    "model, compatible",
    [
        ("max_output=128", True),
        ("effort=low", True),
        ("effort=low max_output=128", True),
        ("max_output=256", False),
        ("effort=auto", False),
        ("test/model", False),
        ("test/model effort=low max_output=128", True),
        ("test/other effort=low max_output=128", False),
    ],
)
def test_parameter_only_model_requirements_retain_composed_startup_defaults(
    tmp_path, model, compatible
):
    from toolang.cli.common.startup import (
        resolve_attachment_requirements,
        resolve_runtime_launch,
    )

    layout = AgentLayout.resident(tmp_path, "aid")
    spec = resolve_runtime_launch(
        layout,
        toolang_root=tmp_path,
        environ={"TOOLANG_DEFAULT_MODEL": "test/model effort=low"},
        sandbox="host",
        dev=None,
        lifetime="persistent",
        background=True,
        port=8123,
        defaults=["model=max_output=128"],
    )
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=startup_contract(spec)),
    )
    requested = resolve_attachment_requirements(
        sandbox="host", defaults=[f"model={model}"]
    )
    if compatible:
        require_compatible(layout, requested)
    else:
        with pytest.raises(ValueError, match="run defaults.*restart"):
            require_compatible(layout, requested)


def test_environment_and_launch_bookkeeping_do_not_change_settings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={"PROVIDER_API_KEY": "original", "PWD": "/one"},
        )
    )
    assert startup_contract(spec) == startup_contract(
        replace(
            spec,
            serve=replace(spec.serve, lifetime="foreground", launch_id="replacement"),
            environ={"PROVIDER_API_KEY": "changed", "PWD": "/two", "TERM": "other"},
            dotenv_envs={"OTHER": "changed"},
        )
    )


def test_legacy_model_bindings_require_an_exact_operation(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    model = ModelOverride(identity="test/model", max_output=128)
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={},
            default_overrides={"model": model},
        )
    )
    saved = {
        key: value
        for key, value in startup_contract(spec).items()
        if key.count(".") <= 1
    }
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(layout, {"default_overrides": {"model": model}})
    with pytest.raises(ValueError, match="run defaults.*restart"):
        require_compatible(
            layout,
            {"default_overrides": {"model": ModelOverride(max_output=128)}},
        )


def test_legacy_aggregate_bindings_still_verify_exact_explicit_settings(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={},
            workspace_additions={"repo": "/one", "other": "/two"},
        )
    )
    saved = {
        key: value for key, value in startup_contract(spec).items() if "." not in key
    }
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(layout, {})
    require_compatible(
        layout, {"workspace_additions": {"repo": "/one", "other": "/two"}}
    )
    with pytest.raises(ValueError, match="workspace bindings.*restart"):
        require_compatible(layout, {"workspace_additions": {"repo": "/one"}})


@pytest.mark.parametrize("process_override", [False, True])
def test_explicit_catalog_matches_effective_dotenv_and_process_inputs(
    tmp_path, process_override
):
    from toolang.cli.common.startup import resolve_attachment_requirements

    layout = AgentLayout.resident(tmp_path, "aid")
    dotenv_catalog = tmp_path / "dotenv.json"
    process_catalog = tmp_path / "process.json"
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ=(
                {"TOOLANG_MODEL_CATALOG": str(process_catalog)}
                if process_override
                else {}
            ),
        )
    )
    spec = replace(spec, dotenv_envs={"TOOLANG_MODEL_CATALOG": str(dotenv_catalog)})
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=startup_contract(spec)),
    )
    selected = process_catalog if process_override else dotenv_catalog
    require_compatible(
        layout,
        resolve_attachment_requirements(sandbox="host", model_catalog=selected),
    )
    other = dotenv_catalog if process_override else process_catalog
    with pytest.raises(ValueError, match="model catalog"):
        require_compatible(
            layout,
            resolve_attachment_requirements(sandbox="host", model_catalog=other),
        )
