"""Top views and source recovery through real Hub and agent HTTP services."""

import asyncio
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

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

            async def register(agent, token, endpoint=""):
                async with httpx.AsyncClient(trust_env=False) as http:
                    response = await http.put(
                        connection.endpoint + f"/agents/{agent}/lease",
                        headers={"X-Toolang-Agent-Lease": token},
                        json={"endpoint": endpoint, "managed": False},
                    )
                    response.raise_for_status()

            await register("agent:alice", "wire", endpoint)
            await register("agent:bob", "idle")

            async def heartbeat():
                while True:
                    await driver.lease("agent:alice", "wire", 15)
                    await driver.lease("agent:alice", "new", 15)
                    await driver.lease("agent:bob", "idle", 15)
                    await asyncio.sleep(3)

            beating = asyncio.create_task(heartbeat())
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                await asyncio.to_thread(session.wait_for, "2/2 online", "$0.25")
                for options, labels in [
                    (("--view", "agent"), ("AGENT", "alice", "bob")),
                    (("--view", "thread"), ("THREAD", "1 active")),
                    (
                        ("--view", "execution", "--tree"),
                        ("STEP", "math__double", "└─"),
                    ),
                    (
                        ("--view", "execution", "--sort", "cost", "--since", "all"),
                        ("RUN", "SPEND↓", "TIME+"),
                    ),
                    (
                        ("--filter", "MATH__DOUBLE", "--active", "--recent", "all"),
                        ("Filter: MATH__DOUBLE", "Activity: all"),
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
                await asyncio.to_thread(session.wait_for, "1/2 online")
                async with asyncio.timeout(3):
                    while True:
                        offline = (await pages())["agent:alice"]
                        if offline["stale"] and not offline["paths"]:
                            break
                        await asyncio.sleep(0.1)
                assert offline["stats"]["model"] == 2
                await register("agent:alice", "new", endpoint)
                session.data.clear()
                await asyncio.to_thread(session.wait_for, "2/2 online")
                async with asyncio.timeout(3):
                    while True:
                        recovered = (await pages())["agent:alice"]
                        if not recovered["stale"] and recovered["paths"]:
                            break
                        await asyncio.sleep(0.1)
                assert recovered["stats"]["model"] == 2
                # Restart the actual Hub while the same terminal and agent keep
                # running. Backend identity and committed counts must survive.
                await asyncio.to_thread(running_hub.stop, force=True)
                session.data.clear()
                await asyncio.to_thread(session.wait_for, "Reconnecting")
                session.send(b"e\x1b[B\r")
                await asyncio.to_thread(session.wait_for, "Unavailable")
                await asyncio.to_thread(
                    running_hub.start,
                    [
                        sys.executable,
                        "-m",
                        "toolang.cli.toolang.main",
                        "--root",
                        str(tmp_path),
                        "hub",
                        "serve",
                        "--port",
                        str(urlsplit(connection.endpoint).port),
                    ],
                )
                session.data.clear()
                await asyncio.to_thread(session.wait_for, "2/2 online", "$0.25")
                assert (await pages())["agent:alice"]["stats"]["model"] == 2
                await asyncio.to_thread(session.wait_for, "\nAlready complete")
                session.send(b"\x1b")
                await asyncio.to_thread(session._read, timeout=0.2)
                session.send(b"\x1b[Ae\x1b[15~")
                await asyncio.to_thread(session.wait_for, "STEP", "math__double")
                (tmp_path / "release-tool").touch()
                await asyncio.to_thread(session.wait_for, "test/scripted", "preview:")
                (tmp_path / "release-model").touch()
                session.data.clear()
                await asyncio.to_thread(
                    session.wait_for,
                    "succeeded · Already complete",
                    "succeeded · flow:review",
                )
                session.send(b"q")
                assert await asyncio.to_thread(session.wait_for_exit) == 0
        finally:
            if "beating" in locals():
                beating.cancel()
                await asyncio.gather(beating, return_exceptions=True)
            await driver.close()

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_hub_reconciles_removed_and_recreated_agent(valkey, running_hub, tmp_path):
    """Production scans remove vanished homes while keeping historical activity."""
    import shutil
    from toolang.common.layout import AgentLayout
    from toolang.execution.schemas import ActivitySnapshot
    from toolang.teaming.agent_client import AgentClient
    from toolang.teaming.backend import PREFIX
    from toolang.teaming.errors import EventRecoveryRequired

    async def scenario():
        layout = AgentLayout.resident(tmp_path, "alice")
        layout.home.mkdir(parents=True)
        connection = running_hub.connection()
        async with (
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="old",
                connection=lambda: connection,
            ) as old,
            httpx.AsyncClient(base_url=connection.endpoint, trust_env=False) as http,
        ):
            await old.register(connection.human)
            await old.publish_activity(
                [
                    ActivitySnapshot(
                        agent=old.actor,
                        revision=1,
                        observed=10,
                        since="session",
                        recent=1800,
                    )
                ]
            )
            shutil.rmtree(layout.home)
            await old.unregister()
            async with asyncio.timeout(15):
                while True:
                    response = await http.get("/activity")
                    response.raise_for_status()
                    if not response.json():
                        break
                    await asyncio.sleep(0.2)
            driver = Backend(valkey)
            try:
                assert await driver._call("TTL", f"{PREFIX}:activity:agent:alice") == -1
            finally:
                await driver.close()
            layout.home.mkdir()
            async with AgentClient(
                tmp_path,
                actor="agent:alice",
                token="new",
                connection=lambda: connection,
            ) as new:
                await new.register(connection.human)
                with pytest.raises(EventRecoveryRequired):
                    await old.publish_activity(
                        [
                            ActivitySnapshot(
                                agent=old.actor,
                                revision=999,
                                observed=20,
                                since="session",
                                recent=1800,
                            )
                        ]
                    )
                await asyncio.sleep(0.6)  # Let the snapshot cache expire.
                pages = (await http.get("/activity")).json()
                assert len(pages) == 1 and pages[0]["presence"] == "online"
                assert pages[0]["observed"] == 10 and pages[0]["stale"]
                await new.unregister()

    asyncio.run(scenario())


def test_hub_and_stopped_local_agent_read_the_same_history(
    valkey, running_hub, tmp_path
):
    """The production Hub uses local records, and agent top survives Hub shutdown."""
    from contextlib import closing
    import time
    from toolang.common.layout import AgentLayout
    from toolang.execution import statistics
    from toolang.execution.store import RunStore
    from toolang.execution.types import Output
    from tests.support.execution_fixtures import project_run_end
    from tests.unit.execution.test_activity import root, model, at

    layout = AgentLayout.resident(tmp_path, "alice")
    with closing(RunStore(layout.run_store)) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store)
        project_run_end(
            store,
            run_id="run_root",
            finished_at=at(120),
            output=Output("# Saved result", "_"),
        )
        statistics.checkpoint(store, "one", at(120), end=True)
    layout.program.write_text("agic main(_: Text) -> Text:\n  user: {{_}}\n")
    endpoint = running_hub.connection().endpoint
    with httpx.Client(base_url=endpoint, trust_env=False) as http:
        deadline = time.monotonic() + 10
        while True:
            response = http.get(
                "/activity", params={"since": "all", "all_recent": "true"}
            )
            response.raise_for_status()
            pages = response.json()
            if pages:
                break
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert len(pages) == 1 and pages[0]["presence"] == "offline"
        assert pages[0]["stats"]["model"] == 1
        assert pages[0]["stats"]["time"] == 120 and pages[0]["stats"]["cost"] == 0.5
        result = http.get(
            "/activity/result", params={"agent": "agent:alice", "ref": "run_root"}
        )
        assert result.status_code == 200 and result.json()["text"] == "# Saved result"
    assert running_hub.stop()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolang.cli.toolang.main",
            "--root",
            str(tmp_path),
            "alice",
            "top",
            "--once",
            "--since",
            "all",
            "--recent",
            "all",
            "--view",
            "execution",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "COLUMNS": "180", "TOOLANG_TMUX": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "Agent alice  offline  2m00s" in result.stdout
    assert "$0.50" in result.stdout and "run_root" in result.stdout
