"""Host-root isolation through storage, Hub HTTP, and the Talk command."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json

from fakeredis import FakeAsyncValkey, FakeServer
import httpx
import pytest

from toolang.up.config import hub_config
from toolang.common.config_sources import read_config
from toolang.common.layout import AgentLayout
from toolang.common.place_codes import (
    PLACE_CODE_EPOCH_MS,
    root_code,
    roaming_place_code,
)
from toolang.teaming.api import create_app
from toolang.teaming.backend.valkey import ValkeyBackend
from toolang.teaming.client import HubClient
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import (
    EventProtocolError,
    MessagingError,
    StorageIntegrityError,
)
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.schemas import HUB_API_VERSION, HubConnection, Message


def client(server, config, actor="human:owner", token="lease"):
    return MessagingClient(
        config,
        actor=actor,
        token=token,
        backend=ValkeyBackend(
            config, client=FakeAsyncValkey(server=server, decode_responses=True)
        ),
    )


async def snapshot(raw):
    return {key: await raw.dump(key) for key in await raw.keys("*")}


def test_setup_canonicalizes_host_root_before_deriving_namespace(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    direct = hub_config(
        read_config(root / "config.toml"),
        source=root / "config.toml",
        root=str(root.resolve()),
    ).backend
    assert (
        hub_config({}, source=alias / "config.toml", root=str(alias.resolve())).backend
        == direct
    )
    assert direct.root == str(root.resolve())
    assert replace(direct, root=str(tmp_path / "other")).identity != direct.identity


def test_backend_namespace_matches_the_root_code_in_nonresident_paths(tmp_path):
    source = tmp_path / "source" / "agent.too"
    source.parent.mkdir()
    config = hub_config(
        {}, source=tmp_path / "config.toml", root=str(tmp_path.resolve())
    ).backend
    visiting = AgentLayout.visiting("owner/repo@main/agent.too", "alice", root=tmp_path)
    roaming = AgentLayout.roaming(
        source,
        root=tmp_path,
        place_code=roaming_place_code(str(source.resolve()), PLACE_CODE_EPOCH_MS + 1),
    )
    assert config.root_code == root_code(str(tmp_path.resolve()))
    assert len(config.root_code) == 8
    assert config.root_code == visiting.place.parent.name == roaming.place.parent.name
    backend = ValkeyBackend(config)
    assert backend.keys.prefix == f"toolang:{config.root_code}:v0"


def test_hub_rejects_configuration_bound_to_another_host_root(tmp_path):
    from toolang.teaming.errors import TeamingError
    from toolang.up.hub import serve

    with pytest.raises(TeamingError, match="another Toolang root"):
        serve(
            tmp_path / "other",
            hub_config(
                {}, source=tmp_path / "config.toml", root=str(tmp_path.resolve())
            ),
            port=7001,
        )
    assert not (tmp_path / "other").exists()


def test_concurrent_initialization_preserves_one_root_binding_and_dataset():
    async def scenario():
        server = FakeServer(server_type="valkey")
        config = BackendConfig("redis://shared", root="/one")
        one, two = client(server, config), client(server, config)
        try:
            await asyncio.gather(one.check_backend(), two.check_backend())
            before = await snapshot(one.backend._client)
            await asyncio.gather(one.check_backend(), two.check_backend())
            assert await snapshot(one.backend._client) == before
            assert await one.backend._client.get(one.backend.keys.root) == config.root
        finally:
            await one.close()
            await two.close()

    asyncio.run(scenario())


def test_shared_database_isolates_names_messages_leases_cursors_and_events():
    async def scenario():
        server = FakeServer(server_type="valkey")
        first = BackendConfig("redis://shared", root="/one")
        second = replace(first, root="/two")
        async with (
            client(server, first) as one,
            client(server, second) as two,
            client(server, first, "agent:alice") as alice_one,
            client(server, second, "agent:alice") as alice_two,
        ):
            await asyncio.gather(
                alice_one.register(one.actor), alice_two.register(two.actor)
            )
            sent_one = await one.send("alice", body="one")
            sent_two = await two.send("alice", body="two")
            convo = sent_one["conversation"]
            assert convo == sent_two["conversation"]
            assert [
                Message.decode(data["data"]).body for _, data in await one.read(convo)
            ] == ["one"]
            assert [
                Message.decode(data["data"]).body for _, data in await two.read(convo)
            ] == ["two"]
            await alice_one.advance_talk_cursor(convo, sent_one["stream_id"])
            assert await alice_one.talk_cursor(convo) == sent_one["stream_id"]
            assert await alice_two.talk_cursor(convo) == "0-0"
            meta_one = await one.backend.events.initialize()
            meta_two = await two.backend.events.initialize()
            assert meta_one["epoch"] != meta_two["epoch"]
            raw = one.backend._client
            for current, thread in ((one, "one"), (two, "two")):
                key = current.backend.keys.generation_key("agent:alice", "generation")
                await raw.hset(
                    key, "sample", json.dumps({"v": HUB_API_VERSION, "thread": thread})
                )
                projection = await current.backend.events.projection(
                    "agent:alice", "generation"
                )
                assert projection["sample"]["thread"] == thread
            await alice_one.unregister()
            assert await two.backend.online("agent:alice")
            prefixes = (one.backend.keys.prefix + ":", two.backend.keys.prefix + ":")
            assert prefixes[0] != prefixes[1]
            assert all(key.startswith(prefixes) for key in await raw.keys("*"))

    asyncio.run(scenario())


def test_colliding_root_codes_fail_without_adopting_or_mutating(monkeypatch):
    monkeypatch.setattr("toolang.teaming.config.root_code", lambda root: "00000000")

    async def scenario():
        server = FakeServer(server_type="valkey")
        first = BackendConfig("redis://shared", root="/one")
        second = replace(first, root="/two")
        assert first.identity != second.identity
        async with client(server, first) as owner:
            before = await snapshot(owner.backend._client)
            uninitialized = client(server, second)
            try:
                with pytest.raises(StorageIntegrityError, match="ownership"):
                    await uninitialized.backend.known_participant(owner.actor)
            finally:
                await uninitialized.close()
            with pytest.raises(StorageIntegrityError, match="ownership"):
                async with client(server, second):
                    pytest.fail("colliding root was adopted")
            assert await snapshot(owner.backend._client) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("read", ["participant", "projection"])
def test_reads_recheck_ownership_atomically_when_the_dataset_changes(monkeypatch, read):
    async def scenario():
        server = FakeServer(server_type="valkey")
        config = BackendConfig("redis://shared", root="/one")
        async with client(server, config) as human:
            raw = human.backend._client
            other = FakeAsyncValkey(server=server, decode_responses=True)
            key = (
                human.backend.keys.team_members
                if read == "participant"
                else human.backend.keys.generation_key("agent:alice", "generation")
            )
            execute = raw.execute_command
            changed = False

            async def replace_before_read(command, *args, **kwargs):
                nonlocal changed
                if key in args and not changed:
                    changed = True
                    await other.set(human.backend.keys.root, "/two")
                    if read == "projection":
                        await other.execute_command(
                            "HSET",
                            key,
                            "sample",
                            json.dumps({"v": HUB_API_VERSION, "thread": "two"}),
                        )
                return await execute(command, *args, **kwargs)

            monkeypatch.setattr(raw, "execute_command", replace_before_read)
            try:
                if read == "participant":
                    with pytest.raises(StorageIntegrityError, match="ownership"):
                        await human.backend.known_participant(human.actor)
                else:
                    with pytest.raises(EventProtocolError):
                        await human.backend.events.projection(
                            "agent:alice", "generation"
                        )
                assert changed
            finally:
                await other.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "different", "wrong_type"])
def test_root_ownership_damage_fails_before_messaging_or_event_writes(damage):
    async def scenario():
        server = FakeServer(server_type="valkey")
        config = BackendConfig("redis://shared", root="/one")
        async with client(server, config) as human:
            raw, key = human.backend._client, human.backend.keys.root
            await raw.delete(key)
            if damage == "different":
                await raw.set(key, "/two")
            elif damage == "wrong_type":
                await raw.hset(key, "root", config.root)
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError, match="ownership"):
                await human.register_human()
            with pytest.raises(EventProtocolError, match="ownership"):
                await human.backend.events.initialize()
            assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_fresh_area_ignores_legacy_and_other_root_keys_but_rejects_own_partial_data():
    async def scenario():
        server = FakeServer(server_type="valkey")
        config = BackendConfig("redis://shared", root="/one")
        human = client(server, config)
        raw = human.backend._client
        untouched = {
            "too:teaming:v0:convo:schema": "broken",
            "toolang:other:v0:root": "other",
        }
        for key, value in untouched.items():
            await raw.set(key, value)
        async with human:
            assert {key: await raw.get(key) for key in untouched} == untouched
            prefix = human.backend.keys.prefix
        second = client(server, config)
        raw = second.backend._client
        await raw.delete(*(await raw.keys(prefix + ":*")))
        await raw.set(prefix + ":root", config.root)
        before = await snapshot(raw)
        with pytest.raises(StorageIntegrityError, match="partial"):
            async with second:
                pytest.fail("partial dataset was adopted")
        assert await snapshot(raw) == before
        await raw.aclose()

    asyncio.run(scenario())


def test_hub_api_rejects_other_root_identity_on_the_same_backend_before_mutation():
    async def scenario():
        server = FakeServer(server_type="valkey")
        config = BackendConfig("redis://shared", root="/one")
        human = client(server, config)
        await human.check_backend()
        app = create_app(human.config, backend=human.backend, version="test")
        async with app.router.lifespan_context(app):
            before = await snapshot(human.backend._client)
            connection = HubConnection(
                "http://hub", replace(config, root="/two").identity, root=config.root
            )
            async with HubClient(
                connection, actor="human:owner", transport=httpx.ASGITransport(app)
            ) as other:
                with pytest.raises(MessagingError, match="identity changed"):
                    await other.create_conversation("wrong root")
            assert await snapshot(human.backend._client) == before

    asyncio.run(scenario())


def test_talk_command_uses_the_selected_roots_namespace(tmp_path, monkeypatch):
    from toolang.cli.toolang import main as cli
    from toolang.cli.toolang.commands import talk

    server = FakeServer(server_type="valkey")
    configs = [
        BackendConfig("redis://shared", root=str(tmp_path / name))
        for name in ("one", "two")
    ]
    connections = {
        c.root: HubConnection("http://hub", c.identity, root=c.root) for c in configs
    }
    by_identity = {c.identity: c for c in configs}

    async def register():
        for config in configs:
            async with client(server, config, "agent:alice") as agent:
                await agent.register("human:owner")

    asyncio.run(register())
    monkeypatch.setattr(
        talk, "settings", lambda root: (connections[str(root)], "human:owner")
    )

    @asynccontextmanager
    async def hub(connection, *, actor=None):
        human = client(server, by_identity[connection.identity])
        await human.check_backend()
        app = create_app(human.config, backend=human.backend, version="test")
        async with app.router.lifespan_context(app):
            async with HubClient(
                connection, actor="human:owner", transport=httpx.ASGITransport(app)
            ) as remote:
                yield remote

    monkeypatch.setattr(talk, "HubClient", hub)
    for config, body in zip(configs, ("one", "two"), strict=True):
        assert cli.main(["--root", config.root, "talk", "alice", body]) == 0

    async def verify():
        for config, body in zip(configs, ("one", "two"), strict=True):
            async with client(server, config) as human:
                convo = await human.resolve("alice")
                assert [
                    Message.decode(data["data"]).body
                    for _, data in await human.read(convo.conversation)
                ] == [body]

    asyncio.run(verify())
