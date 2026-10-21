import asyncio
from dataclasses import replace

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.contract import require_compatible, startup_contract
from toolang.up.instance import update_instance
from toolang.up.sandbox import resolve_launch


def test_normalized_bindings_reuse_and_report_only_changed_field_names(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={
                "TOOLANG_ROOT": str(tmp_path),
                "TOOLANG_DEFAULT_MODEL": "private-value",
                "PROVIDER_API_KEY": "secret-key",
            },
            drivers=frozenset({"chat"}),
        )
    )
    saved = startup_contract(spec)
    assert "private-value" not in str(saved)
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=saved),
    )
    require_compatible(
        layout,
        replace(
            spec,
            serve=replace(spec.serve, lifetime="foreground", launch_id="replacement"),
            environ={
                **spec.environ,
                "TOOLANG_HOST_LAUNCH": "private-parent-generation",
            },
        ),
    )
    with pytest.raises(ValueError, match="startup parameters differ: drivers"):
        require_compatible(
            layout,
            replace(
                spec, serve=replace(spec.serve, drivers=frozenset({"chat", "talk"}))
            ),
        )
    with pytest.raises(ValueError, match="environ"):
        require_compatible(
            layout,
            replace(
                spec,
                environ={
                    "TOOLANG_ROOT": str(tmp_path),
                    "TOOLANG_DEFAULT_MODEL": "another-private-value",
                },
            ),
        )
    with pytest.raises(ValueError, match="environ"):
        require_compatible(
            layout,
            replace(spec, environ={**spec.environ, "PROVIDER_API_KEY": "changed"}),
        )


def test_missing_startup_bindings_are_not_adopted(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(layout=layout, sandbox="host", port=8123, environ={})
    )
    with pytest.raises(ValueError, match="unverifiable"):
        require_compatible(layout, spec)


def test_effective_environment_is_independent_of_overridden_dotenv_defaults(tmp_path):
    layout = AgentLayout.resident(tmp_path, "aid")
    spec = asyncio.run(
        resolve_launch(
            layout=layout,
            sandbox="host",
            port=8123,
            environ={"PROVIDER_API_KEY": "effective"},
        )
    )
    assert startup_contract(spec) == startup_contract(
        replace(spec, dotenv_envs={"PROVIDER_API_KEY": "overridden"})
    )
