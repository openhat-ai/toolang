"""Real CLI agent processes and Talk handlers sharing one isolated Hub."""

import asyncio
from http.server import ThreadingHTTPServer
import json
import os
import re
import socket
import subprocess
import sys
import threading
from urllib.parse import quote

import httpx
import pytest
from valkey.asyncio import Valkey

from tests.integration.messaging.test_valkey import (
    running_hub as running_hub,
    valkey as valkey,
)
from tests.integration.up.test_docker_sandbox_lifecycle import (
    _LocalModelHandler,
    docker_wheel_dir as docker_wheel_dir,
)
from toolang.common.accounts import current_username
from toolang.common.place_codes import root_code
from toolang.teaming.backend.valkey.keys import Keys
from toolang.teaming.client import HubClient
from toolang.teaming.schemas import HubConnection, Message, stream_id

from tests.support.layout import root_file

pytestmark = pytest.mark.live_valkey


class _ReplyModel(_LocalModelHandler):
    def _write_json(self, payload):
        if isinstance(payload, dict) and "model_info" in payload:
            payload["model_info"]["test.context_length"] = 131072
        super()._write_json(payload)

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            return super().do_POST()
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = payload["messages"]
        delta = {"role": "assistant", "content": "Handled."}
        finish = "stop"
        batch = None
        for message in reversed(messages):
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            match = re.search(r'\{\s*"agent"\s*:', content)
            if match is not None:
                batch, _ = json.JSONDecoder().raw_decode(content[match.start() :])
                break
        assert batch is not None, [
            str(message.get("content"))[-1000:] for message in messages
        ]
        incoming = next(
            message
            for message in reversed(batch["conversations"][0]["messages"])
            if message["sender"] != batch["agent"]
        )
        # Each request carries its own continuation state, independently of roots.
        replied = any(
            call["function"]["name"] == "convo__write"
            and json.loads(call["function"]["arguments"])["reply_to"] == incoming["id"]
            for message in messages
            for call in message.get("tool_calls") or []
        )
        if not replied:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "reply",
                        "type": "function",
                        "function": {
                            "name": "convo__write",
                            "arguments": json.dumps(
                                {
                                    "target": batch["conversations"][0]["conversation"],
                                    "message": "reply:" + incoming["body"],
                                    "reply_to": incoming["id"],
                                }
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        if not payload.get("stream"):
            self._write_json(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-ollama:latest",
                    "choices": [
                        {"index": 0, "message": delta, "finish_reason": finish}
                    ],
                }
            )
            return
        chunks = [
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-ollama:latest",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-ollama:latest",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            },
        ]
        body = (
            b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
            + b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def reply_model():
    server = ThreadingHTTPServer(("0.0.0.0", 0), _ReplyModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_cli_root_selection_honors_flags_environment_default_and_aliases(
    valkey,
    running_hub,
    tmp_path,
):
    endpoint = running_hub.current().endpoint
    roots = [
        tmp_path / "flag-root",
        tmp_path / "env-root",
        tmp_path / "home" / ".toolang",
    ]
    for root in roots:
        root.mkdir(parents=True)
        (root_file(root, "config.toml")).write_text(f'[teaming]\nhub = "{endpoint}"\n')
    alias = tmp_path / "alias"
    alias.symlink_to(roots[0], target_is_directory=True)
    base = [sys.executable, "-m", "toolang.cli.toolang.main"]
    env = {
        **os.environ,
        "HOME": str(roots[2].parent),
        "LOGNAME": "stale-login",
        "USER": "stale-login",
        "TOOLANG_TMUX": "0",
    }
    for prefix, environment, name in (
        (["--root", str(alias)], {**env, "TOOLANG_ROOT": str(roots[1])}, "flag-only"),
        ([], {**env, "TOOLANG_ROOT": str(roots[1])}, "env-only"),
        (
            [],
            {key: value for key, value in env.items() if key != "TOOLANG_ROOT"},
            "default-only",
        ),
    ):
        result = subprocess.run(
            [*base, *prefix, "convo", "create", name],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    async def scenario():
        owner = "human:" + current_username()
        for root, expected in zip(roots, ("flag-only", "env-only", "default-only")):
            async with HubClient(
                HubConnection(endpoint, root=str(root)), actor=owner
            ) as client:
                rows = [row for row in await client.contacts() if row["name"] != "all"]
                assert [row["name"] for row in rows] == [expected]
                assert (
                    await client.conversation(rows[0]["conversation"])
                ).created_by == owner

    asyncio.run(scenario())

    from toolang.up.allocation import retained_roaming

    source = tmp_path / "probe.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    (tmp_path / "toolang.toml").write_text("[teaming]\nenabled = false\n")
    for root, prefix, environment in (
        (
            roots[0],
            ["--root", str(alias), str(source)],
            {**env, "TOOLANG_ROOT": str(roots[1])},
        ),
        (
            roots[1],
            ["-r", str(roots[1]), "run", str(source)],
            {**env, "TOOLANG_ROOT": str(roots[0])},
        ),
        (
            roots[2],
            [str(source)],
            {key: value for key, value in env.items() if key != "TOOLANG_ROOT"},
        ),
    ):
        result = subprocess.run(
            [*base, *prefix, "main", "--quiet", "value"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        homes = retained_roaming(source, toolang_root=root)
        assert len(homes) == 1 and homes[0].root == root


@pytest.mark.parametrize(
    "second_sandbox", ["host", pytest.param("docker", marks=pytest.mark.live_docker)]
)
def test_cli_same_name_agents_reply_in_their_root_and_recover_independently(
    valkey,
    running_hub,
    tmp_path,
    reply_model,
    second_sandbox,
    request,
):
    record = running_hub.current()
    endpoint = record.endpoint
    roots = [tmp_path / "one", tmp_path / "two"]
    owner = "human:" + current_username()
    env = {
        **os.environ,
        "LOGNAME": "stale-login",
        "USER": "stale-login",
        "TOOLANG_TMUX": "0",
    }
    for root in roots:
        home = root / "agents" / "alice"
        home.mkdir(parents=True)
        (home / "agent.too").write_text(
            "agic talk(_: Json):\n  recall = none\n  context = none\n  user: {{_}}\nflow local:\n  let result = Done\n"
        )
        (root_file(root, "config.toml")).write_text(
            f'[teaming]\nhub = "{endpoint}"\n[model]\nref = "ollama/test-ollama:latest"\n[allow]\nmodels = ["ollama/*"]\ntools = ["*"]\n'
        )
        (root / ".env").write_text(f"OLLAMA_HOST=http://127.0.0.1:{reply_model}\n")
        (root / ".alice.env").write_text("OLLAMA_HOST\n")

    def cli(root, *args, check=True, environment=False):
        command = [sys.executable, "-m", "toolang.cli.toolang.main"]
        command += [] if environment else ["--root", str(root)]
        result = subprocess.run(
            [*command, *args],
            env={**env, "TOOLANG_ROOT": str(root)},
            capture_output=True,
            text=True,
            timeout=180,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    dev = (
        request.getfixturevalue("docker_wheel_dir")
        if second_sandbox == "docker"
        else None
    )
    ports = [None, None]

    def start(index):
        ports[index] = _port()
        cli(
            roots[index],
            "alice",
            "start",
            "--sandbox",
            "host" if index == 0 else second_sandbox,
            "--enable",
            "chat,talk",
            "--port",
            str(ports[index]),
            *(["--dev", str(dev)] if index == 1 and dev else []),
        )

    async def await_reply(index, marker, *, direct=False):
        root = roots[index]
        connection = HubConnection(endpoint, root=str(root))
        async with (
            HubClient(connection, actor=owner) as client,
            Valkey.from_url(valkey.url, decode_responses=True) as raw,
        ):
            conversation = (
                await client.resolve(
                    "agent:alice" if direct else "all",
                    kind=None if direct else "system",
                )
            ).conversation
            cursor_key = Keys(root_code(str(root))).talk_key("agent:alice")
            async with asyncio.timeout(40):
                while True:
                    entries = [
                        (sid, Message.decode(data["data"]))
                        for sid, data in await client.read(conversation)
                    ]
                    messages = [message for _, message in entries]
                    assert not any(message.handler_error for message in messages), [
                        message.body for message in messages
                    ]
                    source_sid, source = next(
                        (sid, message)
                        for sid, message in entries
                        if message.body == marker
                    )
                    replies = [
                        message
                        for message in messages
                        if message.in_reply_to == source.id
                    ]
                    cursor = await raw.hget(cursor_key, conversation) or "0-0"
                    if replies and stream_id(cursor) >= stream_id(source_sid):
                        assert [message.body for message in replies] == [
                            "reply:" + marker
                        ]
                        break
                    await asyncio.sleep(0.1)
            assert all(
                ("two:" if index == 0 else "one:") not in message.body
                for message in messages
            )
            return conversation

    async def exchange(index, marker, *, direct=False, environment=False):
        await asyncio.to_thread(
            cli,
            roots[index],
            "talk",
            "alice" if direct else "all",
            marker,
            environment=environment,
        )
        return await await_reply(index, marker, direct=direct)

    async def scenario():
        systems = []
        async with asyncio.timeout(40):
            for root in roots:
                async with HubClient(
                    HubConnection(endpoint, root=str(root)), actor=owner
                ) as client:
                    while not any(
                        member["member"] == "agent:alice" and member["online"]
                        for member in await client.team()
                    ):
                        await asyncio.sleep(0.1)
                    assert await client.agents() == {"agent:alice": owner}
                    systems.append(
                        (await client.resolve("all", kind="system")).conversation
                    )
        # Message IDs and content can coincide across roots, just like agent names.
        message = Message.create(owner, "identical input")
        async with httpx.AsyncClient(trust_env=False) as http:
            for root, conversation in zip(roots, systems):
                response = await http.post(
                    endpoint + "/msg/messages",
                    headers={
                        "X-Toolang-Root": quote(str(root), safe=""),
                        "X-Toolang-Human": quote(owner, safe=""),
                    },
                    json={
                        "id": message.id,
                        "target": conversation,
                        "body": message.body,
                    },
                )
                response.raise_for_status()
        await asyncio.gather(*(await_reply(index, message.body) for index in range(2)))
        await asyncio.gather(
            exchange(0, "one:first"), exchange(1, "two:first", environment=True)
        )
        direct = await asyncio.gather(
            exchange(0, "one:direct", direct=True),
            exchange(1, "two:direct", direct=True),
        )
        assert direct[0] == direct[1]
        await asyncio.to_thread(cli, roots[0], "convo", "create", "one-only")
        listing = await asyncio.to_thread(cli, roots[1], "convo", "list")
        assert "one-only" not in listing.stdout
        await asyncio.to_thread(cli, tmp_path, "hub", "stop")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
            for port in ports:
                agent_endpoint = f"http://127.0.0.1:{port}/api/v0"
                created = await http.post(
                    agent_endpoint + "/runs/threads", json={"client_id": "root-matrix"}
                )
                created.raise_for_status()
                result = await http.post(
                    agent_endpoint + "/runs/authored/stream",
                    json={
                        "thread_id": created.json()["thread"]["id"],
                        "request_id": "local-without-hub",
                        "runnable": {"ref": "flow:local", "input": {"_": "local"}},
                        "model": None,
                        "policy": {"allow": [], "limits": {}},
                    },
                )
                assert result.status_code == 200, result.text
                assert any(
                    json.loads(line[6:]).get("status") == "succeeded"
                    for line in result.text.splitlines()
                    if line.startswith("data: ")
                )
        await asyncio.to_thread(
            cli, tmp_path, "hub", "start", "--port", str(record.port)
        )
        await asyncio.gather(exchange(0, "one:restart"), exchange(1, "two:restart"))
        await asyncio.to_thread(cli, roots[0], "alice", "stop")
        await exchange(1, "two:peer-stopped")
        await asyncio.to_thread(start, 0)
        await exchange(0, "one:agent-restart")

    try:
        # Exercise the CLI-owned daemon, whose parent exits after launch.
        running_hub.stop()
        cli(tmp_path, "hub", "start", "--port", str(record.port))
        start(0)
        start(1)
        asyncio.run(scenario())
    finally:
        for root in roots:
            cli(root, "alice", "stop", "--force", check=False)
