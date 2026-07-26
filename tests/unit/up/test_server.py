from __future__ import annotations

from pathlib import Path

from toolang.common.layout import AgentLayout
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
        log_spec="toolang.up=debug",
    )

    argv = build_serve_argv(
        spec,
        root=Path("/root/.toolang"),
        host="0.0.0.0",
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
    assert argv[-2:] == ("--log", "toolang.up=debug")
