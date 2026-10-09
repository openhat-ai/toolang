"""Top views and source recovery through real Hub and agent HTTP services."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from tests.integration.messaging.test_valkey import (
    valkey as valkey,
    running_hub as running_hub,
)
from tests.support.chat_tui_pty import ChatTuiPtySession
from toolang.teaming.backend import Backend

pytestmark = pytest.mark.live_valkey


def test_top_hub_modes_and_source_recovery(valkey, running_hub, tmp_path):
    connection = running_hub.connection()
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e",
        tmp_path,
        connection.endpoint,
        connection.identity,
        columns=180,
    )

    async def scenario():
        driver = Backend(valkey)
        try:
            async with asyncio.timeout(10):
                while not (tmp_path / "activity-endpoint.json").exists():
                    await asyncio.sleep(0.02)
            endpoint = json.loads((tmp_path / "activity-endpoint.json").read_text())[
                "endpoint"
            ]
            await driver.register(
                "human:owner", agent="agent:alice", token="wire", endpoint=endpoint
            )
            await driver.register("human:owner", agent="agent:bob", token="idle")
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                await asyncio.to_thread(session.wait_for, "2 online / 2", "$0.25")
                for options, labels in [
                    (("--view", "agent"), ("View Agent", "alice", "bob")),
                    (("--view", "thread"), ("View Thread", "THREAD", "1 active")),
                    (
                        ("--view", "execution", "--tree"),
                        ("Layout Tree", "math__double", "└─"),
                    ),
                    (
                        ("--view", "execution", "--sort", "cost", "--since", "all"),
                        ("Layout List", "Sort cost", "TIME*"),
                    ),
                    (
                        ("--filter", "MATH__DOUBLE", "--active", "--recent", "all"),
                        ("1/2 matched/eligible", "Recent all"),
                    ),
                    (("--filter", "not-found"), ("No matching activity",)),
                ]:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            "-m",
                            "toolang.cli.toolang.main",
                            "--root",
                            str(tmp_path),
                            "top",
                            "--once",
                            *options,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        env={**os.environ, "COLUMNS": "180", "TOOLANG_TMUX": "0"},
                    )
                    assert result.returncode == 0, result.stderr
                    assert isinstance(result.stdout, str)
                    assert all(label in result.stdout for label in labels), (
                        result.stdout
                    )
                    # Drain the observer's PTY while other clients query the Hub.
                    await asyncio.to_thread(session._read, timeout=0)

                headers = {"X-Toolang-Backend": connection.identity}

                async def pages():
                    response = await http.get(
                        connection.endpoint + "/activity", headers=headers
                    )
                    response.raise_for_status()
                    return {page["agent"]: page for page in response.json()}

                live = (await pages())["agent:alice"]
                local = (await http.get(endpoint + "/api/v1/activity/batch")).json()[0]
                for collection in ("roots", "paths"):
                    fields = ("id", "root", "parent", "status", "title", "summary")
                    assert [
                        tuple(n[field] for field in fields) for n in live[collection]
                    ] == [
                        tuple(n[field] for field in fields) for n in local[collection]
                    ]
                for field in ("model", "tool", "cost"):
                    assert live["stats"][field] == local["stats"][field]
                assert live["paths"]
                assert await driver.lease("agent:alice", "wire", 0)
                session.data.clear()
                await asyncio.to_thread(session.wait_for, "offline", "last seen")
                offline = (await pages())["agent:alice"]
                assert offline["stale"] and not offline["paths"]
                assert offline["stats"]["model"] == 2
                await driver.register(
                    "human:owner", agent="agent:alice", token="new", endpoint=endpoint
                )
                session.data.clear()
                await asyncio.to_thread(session.wait_for, "2 online / 2")
                recovered = (await pages())["agent:alice"]
                assert not recovered["stale"] and recovered["paths"]
                assert recovered["stats"]["model"] == 2
                session.send(b"e\x1b[15~")
                await asyncio.to_thread(session.wait_for, "Layout Tree", "math__double")
                (tmp_path / "release-tool").touch()
                await asyncio.to_thread(session.wait_for, "test/scripted", "preview:")
                (tmp_path / "release-model").touch()
                session.data.clear()
                await asyncio.to_thread(
                    session.wait_for, "0 active", "succeeded · flow:review"
                )
                session.send(b"q")
                assert await asyncio.to_thread(session.wait_for_exit) == 0
        finally:
            await driver.close()

    try:
        asyncio.run(scenario())
    finally:
        session.close()
