"""Every publication validates the same team record before mutating storage."""

import asyncio
import json

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_protocol import client, snapshot
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.backend.valkey.keys import TEAM
from toolang.teaming.backend.valkey.lua import TEAM_GUARD
from toolang.teaming.errors import EventProtocolError, StorageIntegrityError


@pytest.mark.parametrize(
    "operation,concurrent",
    [
        (operation, concurrent)
        for operation in ("activity", "commit", "stage", "abandon")
        for concurrent in (False, True)
    ]
    + [("capture", False)],
)
def test_publication_rejects_invalid_team_record_without_writes(
    operation, concurrent, monkeypatch
):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            backend = human.backend
            agent = "agent:alice"
            await backend.register(human.actor, agent=agent, token="lease")
            meta = await backend.events.initialize()
            op = dict(
                epoch=meta["epoch"],
                agent=agent,
                token="lease",
                id="initial",
                kind="incomplete",
                generation="generation",
                recovery="recovery",
                reason="initial",
            )
            await backend.events.commit(op)
            raw = backend._client
            original = await raw.hget(TEAM, agent)
            record = json.loads(original)
            record["owner"] = None
            await raw.hset(TEAM, agent, json.dumps(record))
            before = await snapshot(raw)
            injected = False
            if concurrent:
                await raw.hset(TEAM, agent, original)
                evaluate = backend._eval

                async def corrupt_after_validation(script, keys, args):
                    nonlocal injected
                    if script.startswith(TEAM_GUARD) and not injected:
                        injected = True
                        await raw.hset(TEAM, agent, json.dumps(record))
                    return await evaluate(script, keys, args)

                monkeypatch.setattr(backend, "_eval", corrupt_after_validation)
            error = (
                StorageIntegrityError if operation == "activity" else EventProtocolError
            )
            with pytest.raises(error):
                if operation == "activity":
                    await backend.activity.save(
                        agent,
                        "lease",
                        [
                            ActivitySnapshot(
                                agent=agent,
                                revision=1,
                                observed=1,
                                since="session",
                                recent=1800,
                            )
                        ],
                    )
                elif operation == "commit":
                    await backend.events.commit(
                        op | {"id": "changed", "reason": "changed"}
                    )
                elif operation == "stage":
                    await backend.events.stage(
                        op | {"entities": {}, "source": 0, "digest": "digest"}
                    )
                elif operation == "abandon":
                    await backend.events.abandon(agent, "generation", token="lease")
                else:
                    await backend.events.capture()
            assert await snapshot(raw) == before
            assert injected == concurrent

    asyncio.run(scenario())
