"""Opt-in wire and terminal checks; never touch the user's Valkey or tmux."""

import asyncio
import json
from contextlib import suppress
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, cast

import libtmux
from libtmux.constants import OptionScope
import pytest
from rich.color import Color
from rich.console import Console
from rich.text import Text

from toolang.cli.common.tmux import Launcher
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.backend.valkey.keys import PRESENCE, TEAM
from toolang.teaming.client import HubClient
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import MessagingError, StorageIntegrityError
from valkey.asyncio import Valkey
from toolang.teaming.schemas import Message
from toolang.up.hub import HubProcess

pytestmark = pytest.mark.live_valkey


@pytest.fixture(params=["valkey-server", "redis-server"])
def valkey(request):
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
                yield BackendConfig(f"unix://{socket}")
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
            await raw.zadd(PRESENCE, {"agent:alice": 1})
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
                    json.loads(await raw.execute_command("HGET", TEAM, "agent:alice"))[
                        "lease"
                    ]["token"]
                    == "new"
                )
                dev = (await replacement.create_conversation("dev")).id
                await bob.join_conversation(dev)
                await bob.send(dev, body="joined")
                await bob.leave_conversation(dev)
                with pytest.raises(MessagingError, match="not a member"):
                    await bob.read(dev)

    asyncio.run(scenario())


@pytest.fixture
def running_hub(valkey, tmp_path):
    (tmp_path / "config.toml").write_text(
        f'[teaming]\nhuman = "owner"\n[teaming.backend]\nurl = "{valkey.url}"\n'
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
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


def test_hub_fails_closed_after_backend_data_loss(valkey, running_hub):
    original = running_hub.current()

    async def scenario():
        async with (
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
            HubClient(running_hub.connection()) as client,
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


def test_hub_cli_lifecycle_and_backend_independence(valkey, tmp_path):
    (tmp_path / "config.toml").write_text(
        f'[teaming]\nhuman = "owner"\n[teaming.backend]\nurl = "{valkey.url}"\n'
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
        "--root",
        str(tmp_path),
    ]

    def run(*args):
        return subprocess.run(
            [*command, *args], capture_output=True, text=True, timeout=30
        )

    async def register():
        async with MessagingClient(valkey, actor="agent:alice") as agent:
            await agent.register("human:owner")

    asyncio.run(register())
    hub = HubProcess(tmp_path)
    try:
        result = run("hub", "start", "--port", str(port))
        assert result.returncode == 0, result.stderr + hub.log.read_text()
        assert f"127.0.0.1:{port}" in result.stdout
        result = run("hub", "status")
        assert result.returncode == 0 and "Hub running" in result.stdout
        assert run("hub", "start", "--port", str(port)).returncode != 0

        async def system_id():
            async with HubClient(hub.connection()) as client:
                return next(
                    c["conversation"]
                    for c in await client.contacts()
                    if c["name"] == "all"
                )

        system = asyncio.run(system_id())
        assert run("talk", system, "via Hub").returncode == 0
        assert run("hub", "stop").returncode == 0
        assert "Hub stopped" in run("hub", "status").stdout
        assert run("talk", system, "requires Hub").returncode != 0

        async def verify():
            async with MessagingClient(valkey, actor="agent:alice") as agent:
                await agent.renew()
                rows = await agent.history(system)
                assert len(rows) == 1
                assert Message.decode(rows[0][1]["data"]).body == "via Hub"
                # Agent messaging still works after Hub has stopped.
                await agent.send(system, body="without Hub")

        asyncio.run(verify())
    finally:
        hub.stop(force=True)


def test_real_talk_terminal_sends_reads_and_reuses_tmux(valkey, tmp_path, running_hub):
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
    (tmp_path / "config.toml").write_text(
        f'[teaming]\nhuman = "owner"\n[teaming.backend]\nurl = "{valkey.url}"\n'
    )
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
            launcher = Launcher(
                agent="isolated-talk",
                _server=cast(Any, tmux),
                _pane=cast(Any, pane),
                session_mark="@toolang_talk",
                window_mark="@toolang_convo",
                pad_kind="talk",
                shared_session="talk",
            )
            # A detached test server has no client to switch; placement remains real.
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
            from toolang.cli.common.errors import TmuxPlacementError

            with suppress(TmuxPlacementError):
                launcher.place_chat(thread_id=dev, argv=argv, directory=str(tmp_path))
            session = next(s for s in tmux.sessions if s.session_name == "talk")
            window = session.windows[0]
            target = window.panes[0]

            def screen():
                return "\n".join(target.capture_pane(start=-200))

            for _ in range(300):
                if "retained greeting" in screen():
                    break
                time.sleep(0.02)
            assert "retained greeting" in screen(), screen()
            assert not screen().startswith("\n"), repr(
                target.capture_pane(start=-200, escape_sequences=True)
            )
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
                assert footer.startswith("  #dev(1)") and dev in footer, screen()
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
            with suppress(TmuxPlacementError):
                launcher.place_chat(thread_id=dev, argv=argv, directory=str(tmp_path))
            assert len(session.windows) == 1 and len(window.panes) == 1
            # Another conversation owns a separate live draft and input history.
            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id=system,
                    argv=[*argv[:-1], system],
                    directory=str(tmp_path),
                )
            assert len(session.windows) == 2

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
            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id=direct,
                    argv=[*argv[:-1], direct],
                    directory=str(tmp_path),
                )
            observer = session.windows[-1].panes[0]

            def observed_screen():
                return "\n".join(observer.capture_pane(start=-200))

            for _ in range(300):
                if "@alice,bob" in observed_screen() and "owner" in observed_screen():
                    break
                time.sleep(0.02)
            observed = observed_screen()
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
    from toolang.teaming.backend.valkey.keys import CONVOS, TEAM_EVENTS, convo_key

    async def scenario():
        async with (
            MessagingClient(valkey, actor="human:owner") as human,
            MessagingClient(valkey, actor="agent:alice", token="lease") as alice,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            await alice.register(human.actor)
            convo = await human.create_conversation("Original")
            events = await raw.xlen(TEAM_EVENTS)

            async def corrupt():
                record = json.loads(await raw.hget(CONVOS, convo.id))
                record["name"] = " invalid "
                await raw.hset(CONVOS, convo.id, json.dumps(record))

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
            assert not await raw.sismember(convo_key(convo.id, "members"), alice.actor)
            assert await raw.xlen(TEAM_EVENTS) == events

    asyncio.run(scenario())
