"""One projection producer, bounded viewers and source-owned clock updates."""

import asyncio
from contextlib import aclosing, closing

from toolang.execution import statistics
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.store import RunStore
from tests.unit.execution.test_activity import at, root, model


def test_slow_subscriber_shares_clock_and_resumes_at_latest_boundary(tmp_path):
    async def scenario():
        with closing(RunStore(tmp_path / "runs.db")) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            reader = ActivityReader(store.db_path, "agent:alice")
            query = ActivityQuery()
            async with (
                aclosing(reader.updates(query)) as first,
                aclosing(reader.updates(query)) as slow,
            ):
                initial = (await anext(first))[0]
                await anext(slow)
                producer = reader._publishers[query]
                assert len(reader._listeners[query]) == 2
                tick = (await asyncio.wait_for(anext(first), 2))[0]
                assert tick.revision == initial.revision
                assert tick.stats.time is not None and initial.stats.time is not None
                assert tick.stats.time > initial.stats.time
                model(store)
                update = (await asyncio.wait_for(anext(first), 2))[0]
                assert update.stats.model == 1 and update.revision > tick.revision
                latest = (await anext(slow))[0]
                assert latest.revision == update.revision
                assert reader._publishers[query] is producer
            assert not reader._publishers and not reader._listeners
            assert producer.done()

    asyncio.run(scenario())


def test_full_result_is_loaded_only_for_details(tmp_path):
    from toolang.execution.types import Output
    from tests.support.execution_fixtures import project_run_end

    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        result = "# Report\n" + "Full result text.\n" * 100
        project_run_end(
            store, run_id="run_root", output=Output(result, "_"), finished_at=at(80)
        )
        reader = ActivityReader(store.db_path, "agent:alice")
        assert reader.result("run_root") == result
        page = reader.read(ActivityQuery(recent=None))
        assert result not in page.model_dump_json()
        assert len(page.roots[0].summary) <= 240
