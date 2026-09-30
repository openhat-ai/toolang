"""Direct Script execution through an AgentServer run client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from toolang.api.app import create_app
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.cli.toolang.commands import script
from toolang.execution.records import RunControlPayload
from toolang.execution.types import RunOverride
from toolang.lang.input import CallInput
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
agic echo(_: Part[]) -> Part[]:
  recall = none
  context = none
  instruct = none
  user: {{_}}
"""


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


@pytest.mark.parametrize("runnable", ["agic:echo", "agic:<entry>", "flow:<entry>"])
def test_remote_script_uses_a_script_thread_and_native_progress(
    tmp_path: Path,
    capsys,
    runnable: str,
) -> None:
    source = _SOURCE
    if runnable == "agic:<entry>":
        source = source.replace("agic echo", "agic")
    elif runnable == "flow:<entry>":
        source += "\nflow(_: Part[]) -> Part[]:\n  run echo\n"
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
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
                runnable=runnable,
                override=RunOverride(),
                input=CallInput({"_": "hello"}),
                raw_named=CallInput({}),
                session_override=RunOverride(),
                quiet=False,
                transport=httpx.ASGITransport(app=app),
            )
        )
        threads = core.store.list_threads()
        control = core.store.get_run_control(run_id=record.id, index=0)

        assert record.status == "succeeded"
        assert str(record.thread) == threads[0].id
        assert threads[0].id.startswith("script_")
        assert threads[0].origin == "script"
        assert core.store.run_output(run_id=record.id) == (TextPart("remote result"),)
        assert control is not None
        assert isinstance(control.payload, RunControlPayload)
        if runnable == "agic:echo":
            assert control.payload.runnable == "agic:echo"
        elif runnable == "agic:<entry>":
            assert control.payload.runnable.startswith("agent::agic:<entry:")
        else:
            assert control.payload.runnable.startswith("agent::flow:<entry:")
        output = capsys.readouterr()
        assert output.out == ""
        assert "• remote result" in output.err
        assert f"▪︎ {record.id}" in output.err
    finally:
        asyncio.run(core.close())


@pytest.mark.parametrize(
    "source_input",
    ["@note.txt", "$attachment -", "@@literal\n```\n@ignored.txt\n```\n@note.txt"],
)
def test_remote_script_captures_client_files_before_server_resolution(
    tmp_path, monkeypatch, source_input
):
    source = _SOURCE + "\nprompt attachment:\n  @note.txt\n"
    harness = ExecutionHarness.create(
        tmp_path / "server",
        source=source,
        prepare_state=True,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    harness.setup.layout.home.mkdir(parents=True, exist_ok=True)
    (harness.setup.layout.home / "note.txt").write_text("server bytes")
    procdir = tmp_path / "client"
    procdir.mkdir()
    (procdir / "note.txt").write_text("client bytes\n@not-recursive.txt")
    monkeypatch.chdir(procdir)
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)
    agents.write_runtime_state(
        core.layout,
        endpoint="http://runtime.test:7001",
        started_at="2026-09-30T00:00:00Z",
        pid=123,
        sandbox_description="Test OS",
    )
    app = create_app(core, CapsManager(core.layout), JobsManager(core.layout))
    try:
        record = asyncio.run(
            script._execute_remote(
                layout=core.layout,
                endpoint="http://runtime.test:7001",
                sandbox="host",
                runnable="agic:echo",
                override=RunOverride(),
                input=CallInput({"_": source_input}),
                raw_named=CallInput({}),
                session_override=RunOverride(),
                quiet=True,
                transport=httpx.ASGITransport(app=app),
            )
        )
        assert record.status == "succeeded", record.error
        invocation = harness.adapter.invocations[-1]
        texts = "\n".join(
            part.text
            for message in invocation.call.messages
            for part in message.parts
            if isinstance(part, TextPart)
        )
        assert "client bytes" in texts
        assert "@not-recursive.txt" in texts
        assert "server bytes" not in texts
    finally:
        asyncio.run(core.close())
