from datetime import datetime, timezone
from pathlib import Path

from toolang.work.state import Chore
from toolang.work.store import ChoreStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_chore_claim_persists_preallocated_run_id(tmp_path: Path) -> None:
    definition = Chore(
        name="TSK-1",
        title=None,
        body="",
        schedule=None,
        path=Path("agent.too"),
    )
    chores = {definition.id: definition}
    store = ChoreStore(tmp_path / "chores.db")
    try:
        store.reconcile(chores=chores, now=NOW)

        claimed = store.claim(
            chore=definition,
            trigger="source",
            run_id="run_executor",
            now=NOW,
        )

        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.active_run_id == "run_executor"
        assert (
            store.finish_run(
                chores=chores,
                run_id="run_executor",
                run_status="succeeded",
                now=NOW,
            )
            is not None
        )
    finally:
        store.close()
