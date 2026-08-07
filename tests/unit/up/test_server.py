from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from toolang.base.types.run import RunLimits
from toolang.common.layout import AgentLayout
from toolang.execution.executor import CeilingSpec
from toolang.up.server import build_serve_argv, resolve_serve


def test_serve_argv_contains_only_server_inputs(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    spec = resolve_serve(
        layout=layout,
        host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        models=("openai/gpt-5",),
        tools=("shell",),
        caps=("skill:search",),
        limits=RunLimits(
            agic_model_calls=25,
            tokens=1000,
            cost=Decimal("1.5"),
            time=60,
        ),
        log_spec="toolang.up=debug",
    )

    argv = build_serve_argv(
        spec,
        root=Path("/root/.toolang"),
        host="0.0.0.0",
    )

    assert spec.ceiling == CeilingSpec(
        models=("openai/gpt-5",),
        tools=("shell",),
        caps=("skill:search",),
    )
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
    limit_index = argv.index("--limit")
    assert argv[limit_index + 1] == (
        "agic_model_calls=25,agic_tool_calls=none,tokens=1000,cost=1.5,time=60"
    )
    assert argv[-2:] == ("--log", "toolang.up=debug")
