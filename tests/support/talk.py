"""Terminal admission doubles for tests of the Talk cursor transport only."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from toolang.execution.talk import TalkBatch, TalkReceipt
from toolang.execution.types import ConversationScope


def admitted_handler(loop, effect=None):
    """Model admission and terminal outcome; durable recovery has separate tests."""
    receipts, runs, notices = {}, {}, set()
    store = loop.executor.store
    store.talk.get.side_effect = lambda scope, after: receipts.get((scope, after))
    store.get_run.side_effect = lambda *, run_id: runs.get(run_id)

    def attempt_notice(run):
        if run in notices:
            return False
        notices.add(run)
        return True

    store.talk.attempt_notice.side_effect = attempt_notice

    async def handle(batch):
        scope = ConversationScope(
            loop.identity, batch["conversations"][0]["conversation"]
        )
        saved = TalkBatch(
            scope,
            after=batch["cursor"]["after"],
            through=batch["cursor"]["through"],
            payload=json.dumps(batch),
        )
        run = f"run_{len(runs)}"
        receipts[scope, saved.after] = TalkReceipt(saved, run, False)
        record = runs[run] = SimpleNamespace(status="running")
        try:
            if isinstance(effect, BaseException) or (
                isinstance(effect, type) and issubclass(effect, BaseException)
            ):
                raise effect
            if effect is not None:
                await effect(batch)
        except BaseException:
            record.status = "failed"
            raise
        else:
            record.status = "succeeded"

    loop.handle = AsyncMock(side_effect=handle)
    return loop.handle
