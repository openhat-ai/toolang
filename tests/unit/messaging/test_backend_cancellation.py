"""Backend calls preserve cancellation when the transport swallows it."""

import asyncio

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_protocol import client


@pytest.mark.parametrize("cancel_during_command", [False, True])
def test_backend_preserves_new_cancellation(monkeypatch, cancel_during_command):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            original = human.backend._client.execute_command

            async def swallow_cancellation():
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    pass

            async def execute(command, *args):
                result = await original(command, *args)
                if cancel_during_command:
                    await swallow_cancellation()
                return result

            monkeypatch.setattr(human.backend._client, "execute_command", execute)

            async def ping():
                # An earlier, deliberately handled cancellation stays handled.
                await swallow_cancellation()
                await human.backend.ping()

            task = asyncio.create_task(ping())
            if cancel_during_command:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert task.cancelled()
            else:
                await asyncio.wait_for(task, 1)
                assert not task.cancelled()

    asyncio.run(scenario())
