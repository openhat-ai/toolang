"""Startup identity, responsive presentation, and optional Hub metadata."""

import asyncio
from io import StringIO
from unittest.mock import AsyncMock

import httpx
import pytest
from rich.console import Console
from rich.text import Text

from tests.support.conversations import conversation_record
from tests.unit.cli.test_talk_layout import talk_app
from toolang.cli.common.banner import Banner
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.toolang.commands.talk import _hub_version, tui
from toolang.cli.toolang.commands.talk.header import startup_header
from toolang.teaming.client import HubClient
from toolang.teaming.schemas import HubConnection, Message, PendingDM


def render(header, width=100, *, color=False):
    output = StringIO()
    console = Console(
        file=output, width=width, color_system="standard" if color else None
    )
    console.print(header)
    return output.getvalue(), list(console.render(header))


@pytest.mark.parametrize("kind", ["dm", "gc", "pending"])
@pytest.mark.parametrize("observer", [False, True])
def test_header_has_exact_conversation_and_user_values(kind, observer):
    participants = ("agent:alice", "agent:bob" if observer else "human:bryan")
    info = (
        PendingDM("dm_00000001", participants)
        if kind == "pending"
        else conversation_record(
            "dm_00000001" if kind == "dm" else "gc_00000001",
            kind,
            participants,
            name="Do not display this name",
        )
    )
    output, segments = render(
        startup_header(
            client_version="0.4.0-local",
            hub_version="0.4.0-server*",
            conversation=info,
            human="human:bryan",
        )
    )
    lines = output.splitlines()
    assert "Talk v0.4.0-local" in lines[0]
    for key, value in [
        ("hub", "v0.4.0-server*"),
        ("convo", info.id),
        ("user", "bryan · view only" if observer else "bryan"),
    ]:
        line = next(line for line in lines if key in line)
        assert line.split(key, 1)[1].strip("│ ") == value
    assert "Do not display" not in output and "alice" not in output
    assert " new" not in output and "client" not in output
    if observer:
        suffix = [segment for segment in segments if "view only" in segment.text]
        assert suffix and all(
            not segment.style or not segment.style.dim for segment in suffix
        )


@pytest.mark.parametrize("width", [2, 8, 16, 20, 30, 40, 68, 69, 120])
def test_complete_caption_and_unicode_values_fit_at_every_width(width):
    caption = "Talk v0.4.0a2-25-g7297ecfd*"
    header = Banner(
        caption,
        (
            ("hub", Text("unknown")),
            ("convo", Text("gc_00000001")),
            ("user", Text("开发 · view only")),
        ),
    )
    output, _ = render(header, width)
    assert all(display_width(line) <= width for line in output.splitlines())
    unwrapped = output.replace("\n", "").replace("│", "").replace(" ", "")
    assert unwrapped.count(caption.replace(" ", "")) == 1
    assert "开发·viewonly" in unwrapped and "gc_00000001" in unwrapped
    if width >= len(caption) + 4:
        assert caption in output.splitlines()[0]
    elif width >= 20:
        assert caption not in output.splitlines()[0]


def test_banner_removes_terminal_controls_and_keeps_literal_text_and_styles():
    field = "\x1b]52;c;secret\x07[red]bryan[/red]\n\t· view only"
    output, segments = render(Banner("Talk v1", (("user", field),)), color=True)
    assert "secret" not in output and "[red]bryan[/red] · view only" in output
    value = [segment for segment in segments if "view only" in segment.text]
    assert value and all(
        not segment.style or not segment.style.dim for segment in value
    )
    title = [segment for segment in segments if "Talk v1" in segment.text]
    assert title and all(
        segment.style and not segment.style.bold and not segment.style.dim
        for segment in title
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "0.4.0-hub", "future": True},
        {"version": "unknown"},
        {},
        {"version": ""},
        {"version": None},
        {"version": 4},
        {"version": "a\nb"},
        {"version": " a"},
        {"version": "\x1b[31m"},
        [],
    ],
)
def test_hub_version_accepts_additive_fields_and_handles_invalid_metadata(payload):
    async def scenario():
        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=payload)

        async with HubClient(
            HubConnection("http://hub", "human:bryan", "dataset"),
            transport=httpx.MockTransport(respond),
        ) as client:
            version = await _hub_version(client)
        expected = (
            payload["version"]
            if isinstance(payload, dict)
            and payload.get("version") in {"0.4.0-hub", "unknown"}
            else "unknown"
        )
        assert version == expected
        assert calls == ["/info"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [404, 503, "transport", "json"])
def test_unavailable_hub_metadata_is_presentation_only(failure):
    async def scenario():
        def respond(request):
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            if failure == "json":
                return httpx.Response(200, text="invalid")
            return httpx.Response(failure, json={"detail": "unavailable"})

        async with HubClient(
            HubConnection("http://hub", "human:bryan", "dataset"),
            transport=httpx.MockTransport(respond),
        ) as client:
            assert await _hub_version(client) == "unknown"

    asyncio.run(scenario())


def test_version_deadline_is_total_and_does_not_retry(monkeypatch):
    original_timeout = asyncio.timeout
    deadlines = []

    def immediate_timeout(delay):
        deadlines.append(delay)
        return original_timeout(0)

    monkeypatch.setattr(asyncio, "timeout", immediate_timeout)

    async def slow():
        await asyncio.Event().wait()

    client = AsyncMock()
    client.info.side_effect = slow
    assert asyncio.run(_hub_version(client)) == "unknown"
    assert deadlines == [2]
    client.info.assert_awaited_once()


def test_talk_prints_header_once_before_history_and_clear(tmp_path, monkeypatch):
    async def scenario():
        async with talk_app(tmp_path) as (ui, _):
            output = StringIO()
            console = Console(file=output, width=80, color_system=None)
            monkeypatch.setattr(tui, "terminal_console", lambda **_: console)

            async def in_terminal(write):
                write()

            monkeypatch.setattr(tui, "run_in_terminal", in_terminal)

            async def run_app(**_):
                assert output.getvalue().endswith("╯\n\n")
                await ui.show(
                    [
                        (
                            "1-0",
                            {
                                "data": Message.create(
                                    "agent:alice", "retained body"
                                ).encode()
                            },
                        )
                    ]
                )
                ui._handle_clear()
                ui.invalidate()

            monkeypatch.setattr(ui.app, "run_async", run_app)
            await ui.run()
            text = output.getvalue()
            assert text.count("Talk unknown") == 1
            assert text.index("Talk unknown") < text.index("retained body")

    asyncio.run(scenario())
