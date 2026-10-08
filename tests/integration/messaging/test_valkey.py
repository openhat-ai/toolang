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
from libtmux.constants import OptionScope
import pytest
from rich.color import Color
from rich.console import Console
from rich.text import Text

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
            # Keep erased live prompts out of tmux's history capture.
            tmux.set_option(
                "scroll-on-clear",
                "off",
                global_=True,
                scope=OptionScope.Window,
            )
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
                            if "Ctrl+Q quit" in line
                        ),
                        "",
                    )
                    if "dev" in footer and len(footer) == min(width, 120):
                        break
                    time.sleep(0.02)
                assert "dev" in footer and "gc_dev" not in footer, screen()
                assert len(footer) == min(width, 120)
                target.send_keys("resize draft", enter=False)
                for _ in range(300):
                    if "resize draft" in "\n".join(target.capture_pane()):
                        break
                    time.sleep(0.02)
                assert "resize draft" in screen(), screen()
                target.send_keys("C-u", enter=False)
                for _ in range(300):
                    current = "\n".join(target.capture_pane())
                    if "Write a message" in current and "resize draft" not in current:
                        break
                    time.sleep(0.02)
                assert "Write a message" in current and "resize draft" not in current
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
                    if "Write a message" in line.plain
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
                if "Connected · Sent" in screen():
                    break
                time.sleep(0.02)
            assert "Connected · Sent" in screen(), screen()

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

            async def prepare_dm():
                async with MessagingClient(valkey) as client:
                    await client.register("bob", "owner", "bob-token")
                    for sender in ("alice", "bob"):
                        await client.send(
                            "dm_alice_bob",
                            sender=sender,
                            agent=True,
                            body="agent greeting",
                        )

            asyncio.run(prepare_dm())
            with suppress(TmuxPlacementError):
                launcher.place_chat(
                    thread_id="dm_alice_bob",
                    argv=[*argv[:-1], "dm_alice_bob"],
                    directory=str(tmp_path),
                )
            observer = session.windows[-1].panes[0]

            def observed_screen():
                return "\n".join(observer.capture_pane(start=-200))

            for _ in range(300):
                if "Read-only · Connected" in observed_screen():
                    break
                time.sleep(0.02)
            observed = observed_screen()
            assert "@alice ↔ @bob · Read-only · Connected" in observed, observed
            assert "Write a message" not in observed and "Enter send" not in observed
            assert any(line.startswith("alice") for line in observed.splitlines())
            assert any(line.startswith("bob") for line in observed.splitlines())
            observer.send_keys("human cannot join this DM", enter=True)

            async def agent_reply():
                async with MessagingClient(valkey) as client:
                    await client.send(
                        "dm_alice_bob", sender="bob", agent=True, body="still receiving"
                    )

            asyncio.run(agent_reply())
            for _ in range(300):
                if "still receiving" in observed_screen():
                    break
                time.sleep(0.02)
            assert "still receiving" in observed_screen(), observed_screen()

            async def unchanged_participants():
                async with MessagingClient(valkey) as client:
                    entries = await client.history("dm_alice_bob")
                    assert [
                        Message.decode(fields["data"]).sender for _, fields in entries
                    ] == ["alice", "bob", "bob"]

            asyncio.run(unchanged_participants())
            observer.send_keys("C-q", enter=False)
            target.send_keys("C-q", enter=False)
        finally:
            tmux.kill()
