"""Opt-in wire and terminal checks; never touch the user's Valkey or tmux."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import libtmux
import httpx
from toolang.setup.teaming import load_teaming_root
from libtmux.constants import OptionScope
import pytest
from rich.color import Color
from rich.console import Console
from rich.text import Text

from toolang.teaming.messaging import MessagingClient
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.backend.valkey.keys import Keys
from toolang.teaming.client import HubClient
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import MessagingError, StorageIntegrityError
from valkey.asyncio import Valkey
from toolang.teaming.schemas import Message, HubConnection, HubPartitionInfo
from toolang.up.hub import HubProcess

from tests.support.layout import root_file

pytestmark = pytest.mark.live_valkey


def test_root_namespace_isolation_on_wire(valkey):
    async def scenario():
        other = replace(valkey, root=valkey.root + "-other")
        async with (
            MessagingClient(valkey, actor="human:owner") as one,
            MessagingClient(other, actor="human:owner") as two,
            MessagingClient(valkey, actor="agent:alice", token="one") as alice_one,
            MessagingClient(other, actor="agent:alice", token="two") as alice_two,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            await asyncio.gather(
                alice_one.register(one.actor), alice_two.register(two.actor)
            )
            sent_one = await one.send("alice", body="one")
            sent_two = await two.send("alice", body="two")
            assert sent_one["conversation"] == sent_two["conversation"]
            for human, sent, body in ((one, sent_one, "one"), (two, sent_two, "two")):
                messages = await human.read(sent["conversation"])
                assert [Message.decode(data["data"]).body for _, data in messages] == [
                    body
                ]
            await raw.set(Keys(valkey.root_code).root, "another root")
            before = {key: await raw.dump(key) for key in await raw.keys("*")}
            with pytest.raises(StorageIntegrityError, match="ownership"):
                await one.create_conversation("must not write")
            assert {key: await raw.dump(key) for key in await raw.keys("*")} == before
            assert await two.backend.online("agent:alice")

    asyncio.run(scenario())


def test_root_discovery_ignores_foreign_binary_key_names(valkey):
    async def scenario():
        async with (
            MessagingClient(valkey, actor="human:owner") as human,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            key = b"toolang:\xffaaaaaaa:v0:root"
            await raw.set(key, "/unrelated")
            assert await human.backend.roots() == [valkey.root]
            assert await raw.exists(key)

    asyncio.run(scenario())


@pytest.fixture(params=["valkey-server", "redis-server"])
def valkey(request, tmp_path):
    executable = shutil.which(request.param)
    if executable is None:
        pytest.skip(f"{request.param} is not installed")
    with tempfile.TemporaryDirectory(prefix="too-valkey-", dir="/tmp") as directory:
        socket = Path(directory) / "socket"
        with (Path(directory) / "server.log").open("w") as log:
            process = subprocess.Popen(
                [
                    executable,
                    "--port",
                    "0",
                    "--unixsocket",
                    str(socket),
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--dir",
                    directory,
                ],
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(200):
                    if socket.exists():
                        break
                    assert process.poll() is None, (
                        Path(directory) / "server.log"
                    ).read_text()
                    time.sleep(0.01)
                assert socket.exists()
                yield BackendConfig(f"unix://{socket}", root=str(tmp_path.resolve()))
            finally:
                process.terminate()
                process.wait(timeout=5)


def test_standard_backend_registration_streams_and_expiry(valkey):
    async def scenario():
        async with (
            MessagingClient(valkey, actor="agent:alice", token="old") as alice,
            MessagingClient(valkey, actor="agent:bob") as bob,
            MessagingClient(valkey, actor="human:owner") as human,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            await asyncio.gather(
                alice.register("human:owner"), bob.register("human:owner")
            )
            groups = await asyncio.gather(
                alice.resolve("agent:bob", create=True),
                bob.resolve("agent:alice", create=True),
            )
            assert groups[0] == groups[1]
            group = groups[0].conversation
            receipt = await alice.send(group, run="run_wire", body="hello")
            assert await alice.read(group) == await bob.read(group)
            assert (
                Message.decode((await bob.history(group))[0][1]["data"]).id
                == receipt["message"]["id"]
            )
            await raw.zadd(Keys(valkey.root_code).presence, {"agent:alice": 1})
            assert not await alice.backend.online("agent:alice")
            with pytest.raises(MessagingError, match="lease lost"):
                await alice.send(group, body="stale")
            own = (await human.send("agent:alice", body="queued while offline"))[
                "conversation"
            ]
            async with MessagingClient(
                valkey, actor="agent:alice", token="new"
            ) as replacement:
                await replacement.register("human:owner")
                assert len(await replacement.history(own)) == 1
                await alice.unregister()
                assert (
                    json.loads(
                        await raw.execute_command(
                            "HGET", Keys(valkey.root_code).team_members, "agent:alice"
                        )
                    )["lease"]["token"]
                    == "new"
                )
                dev = (await replacement.create_conversation("dev")).id
                await bob.join_conversation(dev)
                await bob.send(dev, body="joined")
                await bob.leave_conversation(dev)
                with pytest.raises(MessagingError, match="not a member"):
                    await bob.read(dev)

    asyncio.run(scenario())


def hub_connection(root):
    from urllib.parse import quote

    endpoint = load_teaming_root(root).hub
    with httpx.Client(trust_env=False, timeout=5) as http:
        response = http.get(
            endpoint + "/info", headers={"X-Toolang-Root": quote(str(root), safe="")}
        )
        response.raise_for_status()
    info = HubPartitionInfo.model_validate(response.json())
    return HubConnection(endpoint, info.identity, root=str(root))


@pytest.fixture
def running_hub(valkey, tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (root_file(tmp_path, "config.toml")).write_text(
        f'[teaming]\nhuman = "owner"\nhub = "http://127.0.0.1:{port}"\n[hub.backend]\nurl = "{valkey.url}"\n'
    )
    hub = HubProcess(tmp_path)
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
        "--root",
        str(tmp_path),
        "hub",
        "serve",
        "--port",
        str(port),
    ]
    record = hub.start(command)
    try:
        assert record.port == port and record.status == "running" and hub.ready(record)
        yield hub
    finally:
        hub.stop(force=True)


def test_hub_fails_closed_after_backend_data_loss(valkey, running_hub, tmp_path):
    original = running_hub.current()

    async def scenario():
        async with (
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
            HubClient(hub_connection(tmp_path), actor="human:owner") as client,
        ):
            system = next(
                c["conversation"] for c in await client.contacts() if c["name"] == "all"
            )
            await client.send(system, body="before reset")
            # Only the fixture's isolated Unix-socket backend is cleared.
            await raw.flushdb()
            with pytest.raises(StorageIntegrityError):
                await client.create_conversation("must not recreate storage")
            assert await raw.dbsize() == 0

    asyncio.run(scenario())
    assert running_hub.current() == original


def test_one_hub_keeps_cli_tools_and_executor_conversations_in_the_client_root(
    valkey, running_hub, tmp_path
):
    from toolang.base.types.tool import ToolContext
    from toolang.common.layout import AgentLayout
    from toolang.execution.executor import conversations
    from toolang.plugin.toolsets.convo import ConvoToolset

    endpoint = load_teaming_root(tmp_path).hub
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root_file(root, "config.toml")).write_text(
            f'[teaming]\nhuman = "owner"\nhub = "{endpoint}"\n'
        )

    async def scenario():
        for root in roots:
            connection = load_teaming_root(root).connection(root=str(root))
            async with (
                AgentClient(root, connection=connection, actor="agent:alice") as agent,
                HubClient(connection, actor="human:owner") as human,
            ):
                await agent.register("human:owner")
                system = next(
                    row["conversation"]
                    for row in await human.contacts()
                    if row["name"] == "all"
                )
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "toolang.cli.toolang.main",
                        "--root",
                        str(root),
                        "talk",
                        "all",
                        root.name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stderr
                assert [
                    Message.decode(data["data"]).body
                    for _, data in await agent.read(system)
                ] == [root.name]
                await agent.send(system, body="reply " + root.name)
                await human.set_rules(system, root.name + " rules", revision="0")
                layout = AgentLayout.resident(root, "alice")
                access = await conversations.load_target(
                    layout, system, connection=connection
                )
                assert access.document.content == root.name + " rules"
                toolset = ConvoToolset({"root": str(root), "hub": endpoint})
                rules = await toolset.tools()["get_rules"].invoke(
                    {"target": system}, ToolContext(layout.home, root)
                )
                assert rules.error is None
                assert rules.output["content"] == root.name + " rules"
                assert [
                    Message.decode(data["data"]).body
                    for _, data in await human.read(system)
                ] == [root.name, "reply " + root.name]
        from httpx_sse import aconnect_sse
        from urllib.parse import quote

        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx.AsyncClient(base_url=endpoint, timeout=10, trust_env=False)
            )
            humans, feeds = [], []
            for root in roots:
                human = await stack.enter_async_context(
                    HubClient(
                        load_teaming_root(root).connection(root=str(root)),
                        actor="human:owner",
                    )
                )
                stream = await stack.enter_async_context(
                    aconnect_sse(
                        http,
                        "GET",
                        "/team/events",
                        headers={
                            "X-Toolang-Root": quote(str(root), safe=""),
                            "X-Toolang-Human": "human%3Aowner",
                            "X-Toolang-Backend": human.config.identity,
                        },
                    )
                )
                stream.response.raise_for_status()
                events = stream.aiter_sse()
                assert (await anext(events)).event == "checkpoint"
                humans.append(human)
                feeds.append(events)
            created = await asyncio.gather(
                *(
                    human.create_conversation(root.name)
                    for human, root in zip(humans, roots)
                )
            )
            async with asyncio.timeout(10):
                for events, conversation in zip(feeds, created):
                    event = await anext(events)
                    assert event.event == "change"
                    assert json.loads(event.data)["conversation"] == conversation.id
        async with Valkey.from_url(valkey.url, decode_responses=True) as raw:
            for root in roots:
                config = replace(valkey, root=str(root))
                assert await raw.get(Keys(config.root_code).root) == str(root)
            assert not await raw.exists(Keys(valkey.root_code).root)

    asyncio.run(scenario())


def test_hub_cli_lifecycle_with_separate_client_root(valkey, tmp_path, monkeypatch):
    monkeypatch.delenv("TOOLANG_HUB_PORT", raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = tmp_path / "server"
    client = tmp_path / "client"
    server.mkdir()
    client.mkdir()
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
    ]

    def run(root, *args):
        return subprocess.run(
            [*command, "--root", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    (root_file(server, "config.toml")).write_text(
        f'[hub]\nhost = "127.0.0.1"\nport = {port}\n[hub.backend]\nurl = "{valkey.url}"\n'
    )
    (root_file(client, "config.toml")).write_text(
        f'[teaming]\nhuman = "owner"\nhub = "http://127.0.0.1:{port}"\n'
    )
    unstarted = client / "agents" / "unstarted"
    unstarted.mkdir(parents=True)
    (unstarted / "agent.too").write_text("flow main:\n  pass\n")
    hub = HubProcess(server)
    try:
        result = run(server, "hub", "start")
        assert result.returncode == 0, result.stderr + hub.log.read_text()
        assert f"127.0.0.1:{port}" in result.stdout
        record = json.loads(hub.path.read_text())
        assert set(record) == {"pid", "created", "host", "port", "status"}
        status = run(server, "hub", "status")
        assert status.stdout.startswith(f"hub 127.0.0.1:{port} running ")
        assert status.stdout.endswith(f", pid={record['pid']}\n")
        result = run(client, "ps")
        assert result.returncode == 0
        assert result.stdout.splitlines()[-1].startswith(
            f"hub http://127.0.0.1:{port}, available, "
        )
        assert run(client, "hub", "status").stdout == (
            "hub 127.0.0.1:7001 not running\n"
        )
        assert "unstarted" in result.stdout
        assert not HubProcess(client).path.exists()
        assert not (server / "agents").exists()
        assert run(server, "hub", "start").returncode != 0

        async def exchange():
            connection = load_teaming_root(client).connection(root=str(client))
            async with HubClient(connection, actor="human:owner") as human:
                assert not any(
                    row["member"].startswith("agent:") for row in await human.team()
                )
                async with AgentClient(
                    client, connection=connection, actor="agent:alice", token="first"
                ) as agent:
                    await agent.register("human:owner")
                    sent = await asyncio.to_thread(
                        run, client, "talk", "alice", "via Hub"
                    )
                    assert sent.returncode == 0, sent.stderr
                    direct = (await agent.resolve("human:owner")).conversation
                    assert (
                        Message.decode((await agent.history(direct))[0][1]["data"]).body
                        == "via Hub"
                    )
                    await agent.send(direct, body="reply via HTTP")
                    await agent.unregister()
                member = next(
                    row for row in await human.team() if row["member"] == "agent:alice"
                )
                assert not member.get("online")
                await human.send(direct, body="queued while offline")
                async with AgentClient(
                    client, connection=connection, actor="agent:alice", token="second"
                ) as agent:
                    await agent.register("human:owner")
                    assert [
                        Message.decode(row[1]["data"]).body
                        for row in await agent.history(direct)
                    ] == ["via Hub", "reply via HTTP", "queued while offline"]
                    await agent.unregister()
                return direct

        direct = asyncio.run(exchange())
        assert run(server, "hub", "stop").returncode == 0
        assert not hub.path.exists()
        assert run(client, "ps").stdout.splitlines()[-1] == (
            f"hub http://127.0.0.1:{port}, unavailable"
        )
        assert run(client, "convo", "list").returncode != 0
        assert run(server, "hub", "start").returncode == 0

        async def verify():
            async with HubClient(
                load_teaming_root(client).connection(root=str(client)),
                actor="human:owner",
            ) as human:
                assert len(await human.history(direct)) == 3

        asyncio.run(verify())
        assert run(server, "hub", "stop").returncode == 0
        assert not hub.path.exists()
        assert not HubProcess(client).path.exists()
    finally:
        hub.stop(force=True)


def test_real_talk_terminal_sends_and_reads_in_the_invoking_pane(
    valkey, tmp_path, running_hub
):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")

    async def register():
        async with (
            MessagingClient(valkey, actor="agent:alice") as agent,
            MessagingClient(valkey, actor="human:owner") as human,
        ):
            await agent.register("human:owner")
            dev = (await human.create_conversation("dev")).id
            await human.send(dev, body="retained greeting")
            return dev, (await human.resolve("all", kind="name")).conversation

    dev, system = asyncio.run(register())
    with tempfile.TemporaryDirectory(prefix="too-talk-tmux-", dir="/tmp") as directory:
        tmux = libtmux.Server(
            socket_path=f"{directory}/socket", config_file="/dev/null"
        )
        source = tmux.new_session(
            session_name="origin", attach=False, window_command="sleep 60"
        )
        try:
            # Keep erased live prompts out of tmux's history capture.
            tmux.set_option(
                "scroll-on-clear",
                "off",
                global_=True,
                scope=OptionScope.Window,
            )
            pane = source.active_window.panes[0]
            argv = [
                "env",
                "TERM=xterm-256color",
                "NO_COLOR=",
                "PROMPT_TOOLKIT_COLOR_DEPTH=DEPTH_24_BIT",
                "TOOLANG_COLOR_SCHEME=dark",
                "TOOLANG_PROGRESS_MAX_WIDTH=120",
                sys.executable,
                "-m",
                "toolang.cli.toolang.main",
                "--root",
                str(tmp_path),
                "talk",
                dev,
            ]
            pane.respawn(
                shell=shlex.join(argv), start_directory=str(tmp_path), kill=True
            )
            session = source
            window = source.active_window
            target = pane

            def screen():
                return "\n".join(target.capture_pane(start=-200))

            for _ in range(300):
                if "retained greeting" in screen():
                    break
                time.sleep(0.02)
            assert "retained greeting" in screen(), screen()
            lines = target.capture_pane(start=-200)
            assert lines[0] == "" and lines[1].startswith("╭─ Talk "), repr(lines)
            for width in (200, 60, 180):
                window.resize(width=width, height=30)
                for _ in range(300):
                    footer = next(
                        (
                            line
                            for line in reversed(target.capture_pane())
                            if dev in line and "owner" in line
                        ),
                        "",
                    )
                    if len(footer.rstrip()) == min(width, 120) - 2:
                        break
                    time.sleep(0.02)
                assert footer.startswith("  #dev(1/1)") and dev in footer, screen()
                assert len(footer.rstrip()) == min(width, 120) - 2
                target.send_keys("resize draft", enter=False)
                for _ in range(300):
                    if "resize draft" in "\n".join(target.capture_pane()):
                        break
                    time.sleep(0.02)
                assert "resize draft" in screen(), screen()
                target.send_keys("C-u", enter=False)
                for _ in range(300):
                    current = "\n".join(target.capture_pane())
                    if "Type a message" in current and "resize draft" not in current:
                        break
                    time.sleep(0.02)
                assert "Type a message" in current and "resize draft" not in current
                painted = Text.from_ansi(
                    "\n".join(
                        target.capture_pane(
                            escape_sequences=True, preserve_trailing=True
                        )
                    )
                ).split("\n")
                input_row = next(
                    index
                    for index, line in enumerate(painted)
                    if "Type a message" in line.plain
                )
                console = Console()
                gap = painted[input_row - 2]
                assert not gap.plain.strip()
                assert gap.get_style_at_offset(console, 1).bgcolor is None
                for line in painted[input_row - 1 : input_row + 2]:
                    for column in range(1, min(width, 120)):
                        assert line.get_style_at_offset(
                            console, column
                        ).bgcolor == Color.parse("#1f1f1f")
            target.send_keys("terminal reply", enter=True)
            for _ in range(300):
                sent = screen()
                if "terminal reply" in sent and "Type a message" in sent:
                    break
                time.sleep(0.02)
            assert "terminal reply" in sent and "Type a message" in sent, sent

            async def check():
                async with MessagingClient(valkey, actor="human:owner") as client:
                    messages = [
                        Message.decode(fields["data"])
                        for _, fields in await client.history(dev)
                    ]
                    assert [m.body for m in messages] == [
                        "retained greeting",
                        "terminal reply",
                    ]

            asyncio.run(check())
            assert len(session.windows) == 1 and len(window.panes) == 1
            # Users choose where each additional conversation runs.
            session.new_window(
                window_shell=shlex.join([*argv[:-1], system]), attach=False
            )

            async def prepare_dm():
                async with (
                    MessagingClient(valkey, actor="agent:alice") as alice,
                    MessagingClient(valkey, actor="agent:bob") as bob,
                ):
                    await alice.register("human:owner")
                    await bob.register("human:owner")
                    group = (await alice.resolve("agent:bob", create=True)).conversation
                    for agent in (alice, bob):
                        await agent.send(group, body="agent greeting")
                    return group

            direct = asyncio.run(prepare_dm())
            observer = session.new_window(
                window_shell=shlex.join([*argv[:-1], direct]), attach=False
            ).panes[0]

            def observed_screen():
                return "\n".join(observer.capture_pane(start=-200))

            # The footer renders before asynchronous history delivery. Wait for
            # the messages before asserting the read-only observer's contents.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                observed = observed_screen()
                if all(
                    value in observed
                    for value in ("@alice,bob", "owner", "• alice", "• bob")
                ):
                    break
                time.sleep(0.02)
            assert "@alice,bob" in observed and "owner" in observed, observed
            assert "Type a message" not in observed and "Enter send" not in observed
            headers = {line.strip() for line in observed.splitlines()}
            assert {"• alice", "• bob"} <= headers, observed
            observer.send_keys("human cannot join this DM", enter=True)

            async def agent_reply():
                async with MessagingClient(valkey, actor="agent:bob") as client:
                    await client.send(direct, body="still receiving")

            asyncio.run(agent_reply())
            for _ in range(300):
                if "still receiving" in observed_screen():
                    break
                time.sleep(0.02)
            assert "still receiving" in observed_screen(), observed_screen()

            async def unchanged_participants():
                async with MessagingClient(valkey, actor="human:owner") as client:
                    entries = await client.history(direct)
                    assert [
                        Message.decode(fields["data"]).sender for _, fields in entries
                    ] == ["agent:alice", "agent:bob", "agent:bob"]

            asyncio.run(unchanged_participants())
            observer.send_keys("C-q", enter=False)
            target.send_keys("C-q", enter=False)
        finally:
            tmux.kill()


@pytest.mark.parametrize("concurrent", [False, True])
def test_protocol_validation_prevents_partial_membership_writes(
    valkey, monkeypatch, concurrent
):
    from toolang.teaming.backend.valkey import ValkeyBackend, scripts

    async def scenario():
        async with (
            MessagingClient(valkey, actor="human:owner") as human,
            MessagingClient(valkey, actor="agent:alice", token="lease") as alice,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            await alice.register(human.actor)
            convo = await human.create_conversation("Original")
            events = await raw.xlen(Keys(valkey.root_code).team_events)

            async def corrupt():
                record = json.loads(
                    await raw.hget(Keys(valkey.root_code).convos, convo.id)
                )
                record["name"] = " invalid "
                await raw.hset(
                    Keys(valkey.root_code).convos, convo.id, json.dumps(record)
                )

            if concurrent:
                assert isinstance(alice.backend, ValkeyBackend)
                evaluate = alice.backend._eval
                changed = False

                async def corrupt_before_commit(script, keys, args):
                    nonlocal changed
                    if script == scripts.EDIT and not changed:
                        changed = True
                        await corrupt()
                    return await evaluate(script, keys, args)

                monkeypatch.setattr(alice.backend, "_eval", corrupt_before_commit)
            else:
                await corrupt()
            with pytest.raises(StorageIntegrityError):
                await alice.join_conversation(convo.id)
            assert not await raw.sismember(
                Keys(valkey.root_code).convo_key(convo.id, "members"), alice.actor
            )
            assert await raw.xlen(Keys(valkey.root_code).team_events) == events

    asyncio.run(scenario())


def test_healthy_initialization_does_not_scan_namespace(valkey):
    async def scenario():
        async with (
            MessagingClient(valkey, actor="human:owner") as human,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            before = await raw.info("commandstats")
            scan_calls = before.get("cmdstat_scan", {}).get("calls", 0)
            await human.check_backend()
            after = await raw.info("commandstats")
            assert after.get("cmdstat_scan", {}).get("calls", 0) == scan_calls

    asyncio.run(scenario())
