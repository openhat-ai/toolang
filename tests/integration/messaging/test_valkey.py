"""Opt-in wire and terminal checks; never touch the user's Valkey or tmux."""

import asyncio
from contextlib import suppress
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, cast

import libtmux
import pytest

from toolang.cli.common.tmux import Launcher
from toolang.messaging.client import MessagingClient, online_key
from toolang.messaging.config import MessagingConfig
from toolang.messaging.schemas import Message

pytestmark = pytest.mark.live_valkey


@pytest.fixture
def valkey():
    executable = shutil.which("valkey-server")
    if executable is None:
        pytest.skip("valkey-server is not installed")
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
                yield MessagingConfig(f"unix://{socket}", ("gc_dev",))
            finally:
                process.terminate()
                process.wait(timeout=5)


def test_standard_valkey_registration_streams_and_expiry(valkey):
    async def scenario():
        async with MessagingClient(valkey) as alice, MessagingClient(valkey) as bob:
            await asyncio.gather(
                alice.register("alice", "owner", "a"), bob.register("bob", "owner", "b")
            )
            receipt = await alice.send(
                "dm_alice_bob", sender="alice", agent=True, run="run_wire", body="hello"
            )
            assert await alice.read("dm_alice_bob") == await bob.read("dm_alice_bob")
            assert (
                Message.decode((await bob.history("dm_alice_bob"))[0][1]["data"]).id
                == receipt["message"]["id"]
            )
            await alice.redis.pexpire(online_key("alice"), 10)
            for _ in range(100):
                if not await alice.redis.exists(online_key("alice")):
                    break
                await asyncio.sleep(0.01)
            assert not await alice.redis.exists(online_key("alice"))
            assert await bob.resolve("alice") == "dm_alice"
            await bob.send("dm_alice", sender="owner", body="queued while offline")
            await alice.register("alice", "owner", "new")
            assert len(await alice.history("dm_alice")) == 1
            await alice.unregister("alice", "a")
            assert await alice.redis.get(online_key("alice")) == "new"

    asyncio.run(scenario())


def test_real_text_terminal_sends_reads_and_reuses_tmux(valkey, tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")

    async def register():
        async with MessagingClient(valkey) as client:
            await client.register("alice", "owner", "token")
            await client.send("gc_dev", sender="owner", body="retained greeting")

    asyncio.run(register())
    (tmp_path / "config.toml").write_text(
        f'[human]\nname = "owner"\n[messaging]\nurl = "{valkey.url}"\n'
    )
    with tempfile.TemporaryDirectory(prefix="too-text-tmux-", dir="/tmp") as directory:
        tmux = libtmux.Server(
            socket_path=f"{directory}/socket", config_file="/dev/null"
        )
        source = tmux.new_session(
            session_name="origin", attach=False, window_command="sleep 60"
        )
        try:
            pane = source.active_window.panes[0]
            launcher = Launcher(
                agent="isolated-text",
                _server=cast(Any, tmux),
                _pane=cast(Any, pane),
                session_mark="@toolang_text",
                window_mark="@toolang_group",
                pad_kind="text",
                session_name="text-owner",
            )
            # A detached test server has no client to switch; placement remains real.
            argv = [
                "env",
                "TOOLANG_COLOR_SCHEME=dark",
                sys.executable,
                "-m",
                "toolang.cli.toolang.main",
                "--root",
                str(tmp_path),
                "text",
                "gc_dev",
            ]
            from toolang.cli.common.errors import TmuxPlacementError

            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id="gc_dev", argv=argv, directory=str(tmp_path)
                )
            session = next(s for s in tmux.sessions if s.session_name == "text-owner")
            window = session.windows[0]
            target = window.panes[0]

            def screen():
                return "\n".join(target.capture_pane(start=-200))

            for _ in range(300):
                if "retained greeting" in screen():
                    break
                time.sleep(0.02)
            assert "retained greeting" in screen(), screen()
            target.send_keys("terminal reply", enter=True)
            for _ in range(300):
                if "Sent " in screen():
                    break
                time.sleep(0.02)
            assert "Sent " in screen(), screen()

            async def check():
                async with MessagingClient(valkey) as client:
                    messages = [
                        Message.decode(fields["data"])
                        for _, fields in await client.history("gc_dev")
                    ]
                    assert [m.body for m in messages] == [
                        "retained greeting",
                        "terminal reply",
                    ]

            asyncio.run(check())
            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id="gc_dev", argv=argv, directory=str(tmp_path)
                )
            assert len(session.windows) == 1 and len(window.panes) == 1
            # Another group owns a separate live draft and input history.
            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id="all", argv=[*argv[:-1], "all"], directory=str(tmp_path)
                )
            assert len(session.windows) == 2
            target.send_keys("C-q", enter=False)
        finally:
            with suppress(Exception):
                tmux.kill_server()
