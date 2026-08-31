from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.common.layout import AgentLayout
from toolang.up.server import build_serve_argv, resolve_serve


def test_serve_argv_contains_only_server_inputs(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    spec = resolve_serve(
        layout=layout,
        host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        ceiling_overrides={
            "models": ("openai/gpt-5",),
            "tools": (),
            "skills": None,
        },
        default_overrides={"model": "openai/gpt-5", "runnable": None},
        limit_overrides={
            "agic_model_calls": 25,
            "tokens": 1000,
            "cost": Decimal("1.5"),
            "time": None,
        },
        log_spec="toolang.up=debug",
    )

    argv = build_serve_argv(
        spec,
        root=Path("/root/.toolang"),
        host="0.0.0.0",
    )

    assert spec.ceiling_overrides == {
        "models": ("openai/gpt-5",),
        "tools": (),
        "skills": None,
    }
    assert argv[:9] == (
        "--root",
        "/root/.toolang",
        "serve",
        "alice",
        "--host",
        "0.0.0.0",
        "--endpoint-host",
        "localhost",
        "--port",
    )
    assert "--sandbox" not in argv
    assert "--sandbox-child" not in argv
    assert _option_values(argv, "--allow") == [
        "models=openai/gpt-5",
        "tools=none",
        "skills=all",
    ]
    assert _option_values(argv, "--default") == [
        "model=openai/gpt-5",
        "runnable=none",
    ]
    assert _option_values(argv, "--limit") == [
        "agic_model_calls=25",
        "tokens=1000",
        "cost=1.5",
        "time=none",
    ]
    assert resolve_ceiling_overrides({}, _option_values(argv, "--allow")) == dict(
        spec.ceiling_overrides
    )
    assert resolve_default_overrides({}, _option_values(argv, "--default")) == dict(
        spec.default_overrides
    )
    assert resolve_limit_overrides({}, _option_values(argv, "--limit")) == dict(
        spec.limit_overrides
    )
    assert argv[-2:] == ("--log", "toolang.up=debug")


def _option_values(argv: tuple[str, ...], name: str) -> list[str]:
    return [argv[index + 1] for index, item in enumerate(argv) if item == name]
