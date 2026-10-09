"""Event publication, recovery, and Hub reduction with an isolated backend."""

import asyncio
import json
from uuid import uuid4

from fakeredis import FakeAsyncValkey, FakeServer
import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import TextPart
from toolang.execution.schemas import StreamFrame
from toolang.execution.types import ThreadPrefix
from toolang.teaming.backend import Backend
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import (
    BackendUnavailable,
    EventProtocolError,
    EventRecoveryRequired,
)
from toolang.teaming.event_backend import EventBackend, META, STREAM
from toolang.teaming.events import (
    HubCursor,
    HubScope,
)
from toolang.teaming.exporter import EventExporter
from toolang.teaming.stream_client import HubStreamState
from toolang.teaming.subscriptions import HubSubscription


def backend(server):
    return Backend(
        BackendConfig("redis://test"),
        client=FakeAsyncValkey(server=server, decode_responses=True),
    )


async def prefix(subscription):
    result = []
    while not result or result[-1].event != "stream_checkpoint":
        result.append(await asyncio.wait_for(subscription.receive(), 2))
    return result


async def run(harness):
    thread = harness.threads.create(prefix=ThreadPrefix.TERM)
    return await harness.executor.run(
        harness.run_spec(
            thread=thread, runnable="example", primary=(TextPart("input"),)
        )
    )


async def publish(exporter):
    tail = exporter.source.tail
    while exporter.highwater.seq < tail.seq:
        batch = await asyncio.wait_for(exporter.reader.receive(), 2)
        for frame in batch.events:
            await exporter.publish(frame)


@pytest.mark.parametrize("recover", [False, True])
def test_real_execution_exports_final_and_resumes_hub_scope(tmp_path, recover):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            try:
                await exporter.recover("initial")
                initial = HubSubscription(service, HubScope())
                await initial.prepare()
                before = (await prefix(initial))[-1].id
                initial.close()
                record = await run(harness)
                if recover:
                    await exporter.recover("source_gap")
                else:
                    await publish(exporter)
                scope = HubScope("agent:alice", run=record.id)
                sub = HubSubscription(service, scope, before)
                await sub.prepare()
                frames = await prefix(sub)
                client = HubStreamState(scope)
                for frame in frames:
                    client.feed(frame)
                assert client.agents["agent:alice"].complete(record.id)
                assert client.status["agent:alice"]["complete"]
                assert client.cursor == frames[-1].id
                assert any(frame.event == "run_end" for frame in frames)
                with pytest.raises(StopAsyncIteration):
                    await sub.receive()
                sub.close()
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_uncertain_event_commit_is_deduplicated_and_lease_fences(tmp_path, monkeypatch):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            try:
                await exporter.recover("initial")
                await run(harness)
                original = service.commit
                lost = False

                async def uncertain(op):
                    nonlocal lost
                    result = await original(op)
                    if op["kind"] == "event" and not lost:
                        lost = True
                        raise BackendUnavailable("reply lost")
                    return result

                monkeypatch.setattr(service, "commit", uncertain)
                await publish(exporter)
                rows = await driver._call("XRANGE", STREAM, "-", "+")
                sources = [
                    json.loads(values["data"])["source_cursor"]
                    for _, values in rows
                    if values["kind"] == "event"
                ]
                assert lost and len(sources) == len(set(sources))
                await driver.lease("agent:alice", "lease", 0)
                await run(harness)
                with pytest.raises(EventRecoveryRequired, match="lease"):
                    await publish(exporter)
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_partial_dataset_fails_closed_without_touching_messaging():
    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        service = EventBackend(driver)
        await driver.register("human:owner")
        meta = await service.initialize()
        assert (await service.initialize())["epoch"] == meta["epoch"]
        await driver._call("HSET", META, "pending", "broken")
        with pytest.raises(EventProtocolError, match="incomplete dataset"):
            await service.capture()
        await driver.register("human:other")
        assert "human:other" in await driver.participants()
        await driver.close()

    asyncio.run(scenario())


def test_old_epoch_cursor_above_new_tail_replaces_instead_of_rejecting():
    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        await driver.register("human:owner")
        service = EventBackend(driver)
        scope = HubScope()
        sub = HubSubscription(
            service, scope, str(HubCursor(uuid4().hex, (999999999999999999999, 5)))
        )
        await sub.prepare()
        frames = await prefix(sub)
        assert frames[0].event == "stream_prefill" and frames[0].data["replace"] is None
        state = HubStreamState(scope)
        for frame in frames:
            state.feed(frame)
        assert state.cursor == frames[-1].id
        sub.close()
        await driver.close()

    asyncio.run(scenario())


def test_multi_origin_prefix_failure_keeps_committed_state_and_cursor():
    state = HubStreamState(HubScope())
    cursor = str(HubCursor(uuid4().hex, (1, 0)))
    state.feed(StreamFrame("stream_checkpoint", {"cursor": cursor}, cursor))
    state.feed(
        StreamFrame(
            "stream_status",
            dict(agent="agent:alice", online=True, complete=True, reason=None),
        )
    )
    next_cursor = str(HubCursor(HubCursor.parse(cursor).epoch, (2, 0)))
    state.feed(
        StreamFrame(
            "stream_prefill",
            dict(cursor=next_cursor, scope={"kind": "team"}, replace=None),
        )
    )
    state.feed(
        StreamFrame(
            "stream_status",
            dict(agent="agent:bob", online=True, complete=True, reason=None),
        )
    )
    state.feed(
        StreamFrame(
            "run_begin", dict(agent="agent:bob", type="run_begin", run="invalid")
        )
    )
    with pytest.raises(ValueError):
        state.feed(
            StreamFrame("stream_checkpoint", {"cursor": next_cursor}, next_cursor)
        )
    assert state.cursor == cursor and set(state.status) == {"agent:alice"}
    assert not state.agents


@pytest.mark.parametrize("interrupt", ["expiry", "lost-reply"])
def test_staging_retries_and_activation_receipts_survive_switch(
    tmp_path, monkeypatch, interrupt
):
    from toolang.teaming.event_backend import MANIFEST, generation_key

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            await exporter.recover("initial")
            old = exporter.generation
            record = await run(harness)
            original = service.stage if interrupt == "expiry" else service.commit
            intercepted = False

            async def uncertain(op):
                nonlocal intercepted
                result = await original(op)
                if not intercepted and (
                    interrupt == "expiry" or op["kind"] == "recover"
                ):
                    intercepted = True
                    if interrupt == "expiry":
                        await driver._call(
                            "DEL", generation_key("agent:alice", op["generation"])
                        )
                    raise BackendUnavailable("reply lost")
                return result

            monkeypatch.setattr(
                service, "stage" if interrupt == "expiry" else "commit", uncertain
            )
            try:
                await exporter.recover("source_gap")
                projection = await service.projection(
                    "agent:alice", exporter.generation
                )
                assert projection[MANIFEST]["sealed"] is True
                assert any(
                    value.get("end", {}).get("run") == record.id
                    for value in projection.values()
                    if value.get("end")
                )
                assert not await driver._call(
                    "EXISTS", generation_key("agent:alice", old)
                )
                controls = [
                    row
                    for row in await driver._call("XRANGE", STREAM, "-", "+")
                    if row[1]["kind"] == "recovered"
                ]
                assert len(controls) == 2 and intercepted
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_snapshot_retries_selected_mutation_and_generation_switch(
    tmp_path, monkeypatch
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            await exporter.recover("initial")
            original = service.projection
            calls = 0
            record = None

            async def changing(agent, generation):
                nonlocal calls, record
                calls += 1
                value = await original(agent, generation)
                if calls == 1:
                    record = await run(harness)
                    await exporter.recover("source_gap")
                return value

            monkeypatch.setattr(service, "projection", changing)
            try:
                # A different epoch requires retained terminal history too.
                sub = HubSubscription(
                    service, HubScope(), str(HubCursor(uuid4().hex, (100, 0)))
                )
                await sub.prepare()
                frames = await prefix(sub)
                state = HubStreamState(HubScope())
                for frame in frames:
                    state.feed(frame)
                assert calls >= 2 and record is not None
                assert state.agents["agent:alice"].complete(record.id)
                sub.close()
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_idle_backend_reset_is_detected_and_recovered(tmp_path):
    from contextlib import suppress

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            worker = asyncio.create_task(exporter.run())
            try:
                async with asyncio.timeout(3):
                    while not exporter.generation:
                        await asyncio.sleep(0.01)
                old = exporter.epoch
                await driver._call("FLUSHDB")
                # The lifecycle restores its registration independently.
                await driver.register("human:owner", agent="agent:alice", token="lease")
                async with asyncio.timeout(4):
                    while exporter.epoch == old:
                        await asyncio.sleep(0.01)
                    while (await service.capture("agent:alice"))[1].get(
                        "agent:alice", {}
                    ).get("status") != "complete":
                        await asyncio.sleep(0.01)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_hub_filters_and_cursor_errors_have_flat_http_contract(tmp_path):
    import httpx
    from toolang.teaming.api import create_app
    from toolang.teaming.event_backend import AGENTS, MANIFEST, generation_key
    from toolang.teaming.messaging import MessagingClient

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        client = MessagingClient(
            BackendConfig("redis://test"), actor="human:owner", backend=driver
        )
        app = create_app(client, token="secret")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://hub",
                headers={"Authorization": "Bearer secret"},
            ) as http,
        ):
            for params in (
                {"after": "bad"},
                {"thread": "term_x"},
                {"agent": "human:owner"},
                {"agent": "agent:alice", "thread": "term_x", "run": "run_x"},
            ):
                response = await http.get("/events/stream", params=params)
                assert (
                    response.status_code == 400
                    and response.json()["code"] == "invalid_request"
                )
            response = await http.get(
                "/events/stream", params={"agent": "agent:missing"}
            )
            assert (
                response.status_code == 404
                and response.json()["code"] == "scope_unavailable"
            )
            meta = await EventBackend(driver).initialize()
            response = await http.get(
                "/events/stream",
                params={"after": str(HubCursor(meta["epoch"], (1, 0)))},
            )
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_request"
            await driver._call(
                "HSET",
                AGENTS,
                "agent:alice",
                json.dumps({"v": 1, "status": "complete"}),
            )
            for scope in ("run", "thread"):
                response = await http.get(
                    "/events/stream", params={"agent": "agent:alice", scope: "missing"}
                )
                assert response.status_code == 404
                assert response.json()["code"] == "scope_unavailable"
            await driver._call("HSET", META, "pending", "broken")
            response = await http.get("/events/stream")
            assert (
                response.status_code == 503
                and response.json()["code"] == "protocol_error"
            )
            await driver._call("HDEL", META, "pending")
            await driver._call(
                "HSET", AGENTS, "agent:alice", json.dumps({"v": 1, "generation": "g"})
            )
            await driver._call(
                "HSET",
                generation_key("agent:alice", "g"),
                MANIFEST,
                json.dumps({"v": 1, "count": 1, "baseline": "0-0"}),
                '["run","r"]',
                "{",
            )
            response = await http.get("/events/stream")
            assert (
                response.status_code == 503
                and response.json()["code"] == "protocol_error"
            )

    asyncio.run(scenario())


def test_retry_admission_clears_old_result_and_source_begin(tmp_path):
    from toolang.execution.events import RunRetried
    from toolang.execution.stream import CanonicalEvent
    from toolang.execution.types import ControlRef, EventCursor
    from toolang.teaming.events import field

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            await driver.register("human:owner", agent="agent:alice", token="lease")
            service = EventBackend(driver)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="lease",
            )
            record = await run(harness)
            await exporter.recover("initial")
            old = str(exporter.highwater)
            mutation = RunRetried(
                record.id, str(record.thread), ControlRef.for_run(record.id, 1), (), ()
            )
            event = CanonicalEvent(
                EventCursor(exporter.highwater.epoch, exporter.highwater.seq + 1),
                mutation,
                str(record.thread),
                record.id,
                0,
            )
            try:
                await exporter.publish(event)
                entity = exporter.projection.entities[field("run", record.id)]
                assert entity["end"] is None and entity["end_source"] is None
                assert (
                    entity["begin_source"] is None
                    and entity["begin"]["started_at"] == ""
                )
                assert entity["begin"]["control"] == str(mutation.control)
                scope = HubScope("agent:alice", run=record.id)
                sub = HubSubscription(service, scope)
                await sub.prepare()
                state = HubStreamState(scope)
                for frame in await prefix(sub):
                    state.feed(frame)
                assert not state.agents["agent:alice"].complete(record.id)
                assert not sub._terminal()
                sub.close()
                assert old != str(exporter.highwater)
            finally:
                exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_multi_origin_cached_replay_preserves_global_order_and_identity(tmp_path):
    from toolang.execution.events import RunBegin, RunEnd
    from toolang.execution.stream import CanonicalEvent
    from toolang.execution.types import EventCursor, ControlRef

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        async with harness:
            service = EventBackend(driver)
            exporters = []
            for name in ("alice", "bob"):
                await driver.register("human:owner", agent=f"agent:{name}", token=name)
                exporter = EventExporter(
                    harness.executor.stream,
                    harness.store.db_path,
                    service,
                    agent=f"agent:{name}",
                    token=name,
                )
                await exporter.recover("initial")
                exporters.append(exporter)
            initial = HubSubscription(service, HubScope())
            await initial.prepare()
            after = (await prefix(initial))[-1].id
            initial.close()
            try:
                # Deliberately use the same IDs on distinct origins.
                for exporter, event in (
                    (
                        exporters[0],
                        RunBegin(
                            "run_one",
                            ControlRef.for_run("run_one", 0),
                            thread_id="term_one",
                        ),
                    ),
                    (
                        exporters[1],
                        RunBegin(
                            "run_one",
                            ControlRef.for_run("run_one", 0),
                            thread_id="term_one",
                        ),
                    ),
                    (exporters[0], RunEnd("run_one", "succeeded")),
                    (exporters[1], RunEnd("run_one", "failed")),
                ):
                    await exporter.publish(
                        CanonicalEvent(
                            EventCursor(
                                exporter.highwater.epoch, exporter.highwater.seq + 1
                            ),
                            event,
                            "term_one",
                            "run_one",
                            0,
                        )
                    )
                sub = HubSubscription(service, HubScope(), after)
                await sub.prepare()
                frames = await prefix(sub)
                delivered = [
                    frame
                    for frame in frames
                    if frame.id and frame.event != "stream_checkpoint"
                ]
                assert [frame.data["agent"] for frame in delivered] == [
                    "agent:alice",
                    "agent:bob",
                    "agent:alice",
                    "agent:bob",
                ]
                assert [
                    HubCursor.parse(frame.id).position for frame in delivered
                ] == sorted(HubCursor.parse(frame.id).position for frame in delivered)
                state = HubStreamState(HubScope())
                for frame in frames:
                    state.feed(frame)
                assert all(s.complete("run_one") for s in state.agents.values())
                alice_end = state.agents["agent:alice"].snapshot().events[-1]
                bob_end = state.agents["agent:bob"].snapshot().events[-1]
                assert isinstance(alice_end, RunEnd) and alice_end.status == "succeeded"
                assert isinstance(bob_end, RunEnd) and bob_end.status == "failed"
                sub.close()
            finally:
                for exporter in exporters:
                    exporter.close()
                await driver.close()

    asyncio.run(scenario())


def test_teaming_keeps_lease_through_final_export_drain(tmp_path, monkeypatch):
    from toolang.work.messaging import MessagingLoop
    from toolang.work.teaming import TeamingLoop
    from toolang.teaming.messaging import MessagingClient
    from toolang.teaming.backend import online_key

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        server = FakeServer(server_type="valkey")
        driver = backend(server)
        inspector = backend(server)
        async with harness:
            client = MessagingClient(
                BackendConfig("redis://test"),
                actor=f"agent:{harness.setup.layout.name}",
                backend=driver,
                token="lease",
            )
            messaging = MessagingLoop(
                layout=harness.setup.layout,
                owner="human:owner",
                config=client.config,
                executor=harness.executor,
                threads=harness.threads,
                get_agent_setup=lambda: harness.setup,
                get_agent_state=lambda: harness.state,
                client=client,
            )
            lifecycle = TeamingLoop(messaging)
            observed = []
            original = lifecycle.exporter.publish

            async def checked(frame):
                assert (
                    await inspector._call("HGET", online_key(client.actor), "token")
                    == "lease"
                )
                await original(frame)
                observed.append(frame.event.type)

            monkeypatch.setattr(lifecycle.exporter, "publish", checked)
            lifecycle.start()
            try:
                async with asyncio.timeout(3):
                    while not lifecycle.exporter.generation:
                        await asyncio.sleep(0.01)
                record = await run(harness)
                await lifecycle.stop_messages()
                await harness.executor.stop()
            finally:
                await lifecycle.close()
            assert "run_end" in observed
            assert not await inspector.online(client.actor)
            service = EventBackend(inspector)
            origin = (await service.capture(client.actor))[1][client.actor]
            projection = await service.projection(client.actor, origin["generation"])
            assert any(
                value.get("end", {}).get("run") == record.id
                for value in projection.values()
                if value.get("end")
            )
            assert not harness.executor.stream._readers
            await inspector.close()

    asyncio.run(scenario())


def test_projection_omits_optional_history_before_failing_active_budget(
    tmp_path, monkeypatch
):
    import toolang.teaming.events as events
    from toolang.execution.events import RunBegin, RunEnd
    from toolang.execution.stream import CanonicalEvent
    from toolang.execution.types import ControlRef, EventCursor

    projection = events.Projection()
    monkeypatch.setattr(events, "MAX_RECENT", 2)
    for index in range(4):
        name = f"run_{index}"
        begin = RunBegin(name, ControlRef.for_run(name, 0), thread_id="term_one")
        projection.apply(
            CanonicalEvent(
                EventCursor("a" * 32, index * 2 + 1), begin, "term_one", name, 0
            )
        )
        if index < 3:
            projection.apply(
                CanonicalEvent(
                    EventCursor("a" * 32, index * 2 + 2),
                    RunEnd(
                        name, "succeeded", finished_at=f"2026-10-09T00:00:0{index}Z"
                    ),
                    "term_one",
                    name,
                    0,
                )
            )
    assert projection.trim()
    assert events.field("run", "run_0") not in projection.entities
    assert events.field("run", "run_3") in projection.entities
    monkeypatch.setattr(events, "MAX_ENTITIES", 2)
    projection.trim()
    assert {value.get("root") for value in projection.entities.values()} == {"run_3"}
    monkeypatch.setattr(events, "MAX_ENTITIES", 1)
    from toolang.execution.errors import SnapshotLimitError

    with pytest.raises(SnapshotLimitError):
        projection.trim()


@pytest.mark.parametrize(
    "stored",
    [
        "{",
        "[]",
        "null",
        '{"v":1,"entity":[],"delivery":"1-0"}',
        '{"v":1,"entity":{"v":2},"delivery":"1-0"}',
    ],
)
def test_malformed_stored_projection_is_a_protocol_failure(stored):
    from toolang.teaming.event_backend import generation_key

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        try:
            await driver._call(
                "HSET",
                generation_key("agent:alice", "generation"),
                '["run","r"]',
                stored,
            )
            with pytest.raises(EventProtocolError):
                await EventBackend(driver).projection("agent:alice", "generation")
        finally:
            await driver.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", [False, True])
def test_hub_recovery_ignores_inherited_sse_ids(replacement):
    import httpx
    from httpx_sse import connect_sse
    from toolang.execution.events import RunBegin
    from toolang.execution.types import ControlRef, EventCursor
    from toolang.teaming.subscriptions import envelope

    before = str(HubCursor("a" * 32, (1, 0)))
    boundary = str(HubCursor("a" * 32, (2, 0)))
    scope = HubScope()
    frames = [StreamFrame("stream_checkpoint", {"cursor": before}, before)]
    if replacement:
        frames.append(
            StreamFrame(
                "stream_prefill",
                {"cursor": boundary, "scope": scope.data(), "replace": None},
            )
        )
    frames.append(
        envelope(
            "agent:alice",
            StreamFrame.source(
                RunBegin("run_one", ControlRef.for_run("run_one", 0)),
                str(EventCursor("b" * 32, 1)),
                context=True,
            ),
            boundary,
            context=True,
        )
    )
    frames.append(StreamFrame("stream_checkpoint", {"cursor": boundary}, boundary))
    wire = "".join(
        f"event: {frame.event}\ndata: {json.dumps(frame.data)}\n"
        + (f"id: {frame.id}\n" if frame.id else "")
        + "\n"
        for frame in frames
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=wire, headers={"content-type": "text/event-stream"}
        )
    )
    state = HubStreamState(scope)
    with httpx.Client(transport=transport) as http:
        with connect_sse(http, "GET", "http://hub/events/stream") as stream:
            events = list(stream.iter_sse())
    assert events[1].id == before  # SSE carries the previous ID across absent fields.
    for event in events[:-1]:
        state.feed(StreamFrame(event.event, json.loads(event.data), event.id or None))
    assert state.cursor == before
    if replacement:
        assert not state.agents  # The prefix has not committed yet.
    event = events[-1]
    state.feed(StreamFrame(event.event, json.loads(event.data), event.id or None))
    assert state.agents["agent:alice"].has_run("run_one")
    assert state.cursor == boundary


def test_stale_exporter_cannot_remove_the_new_owners_staging(tmp_path, monkeypatch):
    from toolang.teaming.event_backend import generation_key

    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        service = EventBackend(driver)
        async with harness:
            old = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="old",
            )
            new = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                service,
                agent="agent:alice",
                token="new",
            )
            try:
                await driver.register("human:owner", agent="agent:alice", token="old")
                await old.recover("initial")
                await driver.lease("agent:alice", "old", 0)
                await driver.register("human:owner", agent="agent:alice", token="new")
                stage = service.stage

                async def interrupt(op):
                    await stage(op)
                    key = generation_key("agent:alice", op["generation"])
                    assert await driver._call("EXISTS", key) == 1
                    # The old process has not noticed lease loss yet and retries
                    # just after the new owner uploads its replacement.
                    with pytest.raises(EventRecoveryRequired, match="lease"):
                        await old.recover("source_gap")
                    assert await driver._call("EXISTS", key) == 1

                monkeypatch.setattr(service, "stage", interrupt)
                await new.recover("initial")
                _, origins, _ = await service.capture("agent:alice")
                assert origins["agent:alice"]["generation"] == new.generation
                assert origins["agent:alice"]["status"] == "complete"
                # Cleanup must also preserve a generation that already activated.
                await service.abandon("agent:alice", new.generation, token="new")
                assert await driver._call(
                    "EXISTS", generation_key("agent:alice", new.generation)
                )
            finally:
                old.close()
                new.close()
                await driver.close()

    asyncio.run(scenario())


def test_projection_preserves_nested_background_run_structure():
    from toolang.execution.events import (
        RunBegin,
        RunEnd,
        StepBegin,
        StepEnd,
        event_from_data,
    )
    from toolang.execution.stream import CanonicalEvent
    from toolang.execution.subscriptions import StreamScope
    from toolang.execution.types import ControlRef, EventCursor, StepRef
    from toolang.lang.ast import RunStmt, Span
    from toolang.teaming.events import Projection

    root = RunBegin("run_root", ControlRef.for_run("run_root", 0))
    step = StepBegin(
        StepRef.parse("run_root.0"),
        "run",
        RunStmt(span=Span(line=1), runnable="child", asynchronous=True),
    )
    child = RunBegin("run_child", ControlRef.for_run("run_child", 0), parent=step.step)
    step_end = StepEnd(step.step, "run", "succeeded")
    root_end = RunEnd(root.run, "succeeded")
    child_end = RunEnd(child.run, "succeeded")
    projection = Projection()
    for seq, event in enumerate((root, step, child, step_end, root_end, child_end), 1):
        projection.apply(
            CanonicalEvent(EventCursor("a" * 32, seq), event, "term_one", root.run, 0)
        )
    snapshot = projection.snapshot(StreamScope(root=root.run))
    assert [event_from_data(frame.data) for frame in snapshot.structural()] == [
        root,
        step,
        child,
        child_end,
        step_end,
        root_end,
    ]


@pytest.mark.parametrize("damage", ["orphan", "cycle", "duplicate", "wrong_end"])
def test_projection_rejects_invalid_tree_structure(damage):
    from toolang.execution.events import RunBegin, RunEnd, event_to_data
    from toolang.execution.stream import CanonicalEvent
    from toolang.execution.subscriptions import StreamScope
    from toolang.execution.types import ControlRef, EventCursor, StepRef
    from toolang.teaming.events import Projection, field

    projection = Projection()
    name = "run_one"
    projection.apply(
        CanonicalEvent(
            EventCursor("a" * 32, 1),
            RunBegin(name, ControlRef.for_run(name, 0), thread_id="term_one"),
            "term_one",
            name,
            0,
        )
    )
    entity = projection.entities[field("run", name)]
    if damage in {"orphan", "cycle"}:
        parent = StepRef.parse(f"{name if damage == 'cycle' else 'run_missing'}.0")
        entity["begin"]["parent"] = str(parent)
    elif damage == "duplicate":
        projection.entities[field("run", "run_duplicate")] = dict(entity)
    else:
        entity["end"] = event_to_data(RunEnd("run_other", "succeeded"))
        entity["end_source"] = str(EventCursor("a" * 32, 2))
    with pytest.raises(EventProtocolError):
        projection.snapshot(StreamScope())


@pytest.mark.parametrize("damage", ["root", "control", "manifest", "floor"])
def test_invalid_recovery_metadata_is_a_backend_error(damage):
    import httpx
    from toolang.teaming.api import create_app
    from toolang.teaming.event_backend import AGENTS, MANIFEST, generation_key
    from toolang.teaming.events import field
    from toolang.teaming.messaging import MessagingClient

    async def scenario():
        driver = backend(FakeServer(server_type="valkey"))
        client = MessagingClient(
            BackendConfig("redis://test"), actor="human:owner", backend=driver
        )
        app = create_app(client, token="secret")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                base_url="http://hub",
                headers={"Authorization": "Bearer secret"},
            ) as http,
        ):
            await EventBackend(driver).initialize()
            origin = {"v": 1, "generation": "g"}
            manifest = {"v": 1, "count": 0, "baseline": "0-0"}
            entities = {}
            if damage == "floor":
                origin["floor"] = "bad"
            elif damage == "manifest":
                del manifest["baseline"]
            elif damage == "root":
                entities[field("root", "run_one")] = {
                    "v": 1,
                    "root": "run_one",
                    "thread": "term_one",
                }
            else:
                entities[field("control", "term_one#0")] = {
                    "v": 1,
                    "thread": "term_one",
                    "root": None,
                    "source": None,
                    "event": {"type": "invalid"},
                }
            manifest["count"] = len(entities)
            await driver._call("HSET", AGENTS, "agent:alice", json.dumps(origin))
            await driver._call(
                "HSET",
                generation_key("agent:alice", "g"),
                MANIFEST,
                json.dumps(manifest),
            )
            for key, entity in entities.items():
                await driver._call(
                    "HSET", generation_key("agent:alice", "g"), key, json.dumps(entity)
                )
            response = await http.get(
                "/events/stream",
                params={
                    "after": str(
                        HubCursor(
                            (await EventBackend(driver).initialize())["epoch"], (0, 0)
                        )
                    )
                }
                if damage == "floor"
                else {},
            )
            assert response.status_code == 503
            assert response.json()["code"] == "protocol_error"

    asyncio.run(scenario())
