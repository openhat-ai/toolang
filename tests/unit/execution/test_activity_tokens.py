"""Token facts share spend settlement, ownership and resumable migration."""

from contextlib import closing

from toolang.execution import statistics
from toolang.execution.events import StepEnd
from toolang.execution.store import RunStore
from toolang.execution.types import ModelUsageMeter, ModelAccounting, ModelCost
from toolang.execution.records import ModelStepNoted
from tests.support.execution_fixtures import persist_event
from tests.unit.execution.test_activity import root, model, snapshot, at


def test_tokens_settle_once_with_cache_reads_included_in_input(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        step = model(store, status="running")
        accounting = ModelAccounting(
            input_tokens=5,
            output_tokens=5,
            estimate=ModelCost(0.5, "USD", True),
            selected="estimated",
            meters=(
                ModelUsageMeter("input.cache_read", 3),
                ModelUsageMeter("input.cache_write", 1),
            ),
        )
        event = StepEnd(
            step=step.ref,
            kind="model",
            status="succeeded",
            noted=ModelStepNoted(accounting=accounting),
            output=step.output,
            finished_at=at(70),
        )
        persist_event(store, event)
        persist_event(store, event)
        for page in (snapshot(store), snapshot(store, since=at(60))):
            assert (
                page.stats.input_tokens,
                page.stats.cached_tokens,
                page.stats.output_tokens,
            ) == (5, 3, 5)
            assert page.stats.tokens_complete
            assert page.roots[0].stats.input_tokens == page.stats.input_tokens
        assert snapshot(store, since=at(71)).stats.input_tokens == 0


def test_unknown_cache_and_inflight_usage_are_not_zero(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store, status="running")
        assert snapshot(store).stats.input_tokens is None
        model(store)
        page = snapshot(store)
        assert page.stats.input_tokens == 5 and page.stats.output_tokens == 5
        assert page.stats.cached_tokens is None and not page.stats.tokens_complete


def test_token_migration_preserves_consumption_and_is_idempotent(tmp_path):
    path = tmp_path / "runs.db"
    with closing(RunStore(path)) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store)
        conn = store._conn
        conn.execute("DROP INDEX activity_token_migration")
        for field in (*statistics.TOKEN_FIELDS, "tokens_migrated"):
            conn.execute(f"ALTER TABLE activity_attempts DROP COLUMN {field}")
        for field in statistics.BUCKET_FIELDS[7:]:
            conn.execute(f"ALTER TABLE activity_buckets DROP COLUMN {field}")
        conn.execute("PRAGMA user_version=54")
        conn.commit()
    for _ in range(2):
        with closing(RunStore(path)) as migrated:
            page = snapshot(migrated)
            assert page.stats.model == 1 and page.stats.cost == 0.5
            assert page.stats.input_tokens == 5 and page.stats.output_tokens == 5
            assert page.stats.cached_tokens is None
            assert (
                migrated._conn.execute(
                    "SELECT COUNT(*) FROM activity_attempts WHERE tokens_migrated=0"
                ).fetchone()[0]
                == 0
            )
