"""Direct Script execution through an AgentServer run client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from toolang.api.app import create_app
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.cli.toolang.commands import script
from toolang.execution.records import RunControlPayload
from toolang.lang.input import RunnableInputRaw
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
agic echo(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


def test_remote_script_uses_a_script_thread_and_native_progress(
    tmp_path: Path,
    capsys,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_SOURCE,
        responses=[ModelCallResult(message=Message.assistant("remote result"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)
    agents.write_runtime_state(
        core.layout,
        endpoint="http://runtime.test:7001",
        started_at="2026-08-26T00:00:00Z",
        pid=123,
        sandbox_description="Test OS 1.0 arm64",
    )
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        record = asyncio.run(
            script._execute_remote(
                layout=core.layout,
                endpoint="http://runtime.test:7001",
                sandbox="host",
                runnable="echo",
                commands=(),
                input=RunnableInputRaw(primary="hello"),
                raw_named=(),
                allow_options=(),
                default_options=(),
                limit_options=(),
                quiet=False,
                transport=httpx.ASGITransport(app=app),
            )
        )
        threads = core.store.list_threads()
        control = core.store.get_run_control(run_id=record.id, index=0)

        assert record.status == "succeeded"
        assert record.thread == threads[0].thread_id
        assert threads[0].thread_id.startswith("script_")
        assert threads[0].origin == "script"
        assert core.store.run_output(run_id=record.id) == (TextPart("remote result"),)
        assert control is not None
        assert isinstance(control.payload, RunControlPayload)
        assert control.payload.runnable == "agic:echo"
        output = capsys.readouterr()
        assert output.out == ""
        assert "• remote result" in output.err
        assert f"∎ {record.id}" in output.err
    finally:
        asyncio.run(core.close())
